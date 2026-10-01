# SmartScore

## About this Program
You can find more information about this program on the [website](https://smartscore.nathanprobert.ca/help)!

## Calculating the probability
The current method for calculating the probability takes into account a variety of individual statistics:
 - Player's goals per game (GPG)
 - Player's goals per game in the last 5 games (5GPG)
 - Player's goals per game over the last 3 NHL seasons (HGPG)
 - Team's goals per game (TGPG)
 - Other team's goals against per game (OTGA)
 - Home or away (Home)
 - Player's power play goals per game over the last 3 NHL seasons (HPPG)
 - Other team's short handed goals per game (OTSHGA)

## Data Sources

- Injuries: `https://www.rotowire.com/hockey/tables/injury-report.php?team=ALL&pos=ALL` (JSON, no auth)
- Starting goalies: `https://www.rotowire.com/hockey/tables/projected-goalies.php?date=YYYY-MM-DD` (JSON, no auth, `Confirmed / Expected / Unknown`)
- Goalie + skater stats: official NHL API `api-web.nhle.com` (`goalie-stats-leaders/current`, `player/{id}/landing`, `player/{id}/game-log/now`, `club-stats/{team}/now`, `gamecenter/{gameId}/boxscore` with `starter=true` for backfill). `club-stats/{team}/now` is **current season only** and returns no goalies until the team plays a regular-season game, so `opp_goalie_*` stats are legitimately null in preseason — a goalie with no games this season has no current-season stats, and prior-season numbers are deliberately not backfilled
- Recorded per skater as `opp_goalie_*` fields (name, team, status, GAA, save %, record) via `handle_get_goalies` between `GetInjuries` and `GetTims`
- DB: migrations in `supabase/migrations/*.sql` are applied automatically by CI before the Lambda deploy. Each file is scoped with an `__ENV__` table-name placeholder that CI substitutes — PRs with the `deploy` label apply to the `dev` tables, merges to `main` apply to the `prod` tables. psql keeps no migration history, so every file is re-applied on each run and must be idempotent.

> If RotoWire tables endpoints ever change/break (undocumented, embedded via `loadTableRW` in `starting-goalies.php` / `injury-report.php`), use [DailyFaceoff](https://www.dailyfaceoff.com/starting-goalies/) as fallback for both starting goalies (`Confirmed / Likely` + timestamp + source, server-rendered HTML, scrapable with BeautifulSoup) and injuries ([line combos / injury list](https://www.dailyfaceoff.com/teams/)). Note RotoWire team codes differ from NHL API (`MON` vs `MTL`, `LAS` vs `VGK`) so keep the map in sync. Its `/_next/data/.../line-combinations.json` endpoint referenced in older notes now returns 404.

## Starting lineups

`smartscore/nhl_lineups.py` retrieves line combinations from two sources, and
`handle_get_lineups` merges them into the player list. Both sources are parsed
deterministically — no LLM extraction.

- **Forward lines, defence pairs, goalies, scratches, injuries:** the NHL.com daily
  projections article (`https://www.nhl.com/news/nhl-lineup-projections-2026-27-season`).
  The lineups ship inside a JSON-LD `NewsArticle` block, so this reads the
  `articleBody` JSON field rather than scraping HTML. Units are identified by group
  size and labelled positionally: `F1`-`F4` (trios), `D1`-`D3` (pairs), `G1`/`G2`.
  `articleBody` is one flat markdown blob where each game header is appended to the
  end of the preceding paragraph, so it must be segmented by regex match position —
  splitting on newlines silently corrupts the result.
- **Power play units, goalie designation, injuries:** the lineup section of
  `https://www.rotowire.com/hockey/nhl-lineups.php` (BeautifulSoup over the stable
  `lineup__*` class names). RotoWire does not publish forward lines publicly, so this
  is complementary to the NHL.com article, which omits PP units.

### Persisted fields

`handle_get_lineups` writes four columns, added by
`supabase/migrations/20261001_add_lineup_columns.sql`:

| Field | Meaning |
|---|---|
| `lineup_unit` | Forward line only (`F1`-`F4`) |
| `lineup_position_group` | `F` / `D` / `G`, whichever unit the player sits in |
| `pp_unit` | `POWER PLAY #1` / `POWER PLAY #2`, or null |
| `lineup_status` | `PROJECTED` when a forward line matched, else `UNKNOWN` |

A forward can carry both `lineup_unit` and `pp_unit` (typically ~117 players per slate).
`lineup_status` is deliberately `UNKNOWN` rather than absent on a miss, so an empty or
failed fetch is distinguishable from a genuine non-match.

Defence pairings and goalie designations are parsed and available on the source payload
but not stored per skater, since the picks table is skater-scoped.

### Using these for model training

Verified end-to-end: all four fields reach `Picks-{ENV}` and `Historic-Picks-{ENV}`
(`save_to_db` / `update_historical_data` pass whole dicts through, and the Mongo
`POST_BATCH` path copies unknown fields via `[key: string]: unknown` + `filterPlayerFields`).

Constraints worth knowing before training on this:

- **Only 3 picks/date are recorded.** `choose_picks` reduces the pool to
  `NUM_EXPECTED_PLAYERS` (3) before `write_historic_db`, so `Historic-Picks` holds
  ~3 rows/day, not the full roster. `Picks-{ENV}` is the only table with every player.
- **Only 8 days are retained** (`DAYS_TO_KEEP_HISTORIC_DATA`), and rows only earn a
  `Scored` value once that date is finalised. Treat the historic table as a short
  rolling window, not a training corpus — pull from `Picks-{ENV}` if you need volume.
- **The Rust predictor does not read these fields.** `make_predictions_teams` builds
  `make_predictions_rust.PlayerInfo` from an explicit field list (gpg/hgpg/tgpg/otga/
  otshga/hppg/home), so lineup data is recorded but not yet a model input.
  `MakePredictions` also runs *before* `GetLineups` in the state machine.
- **`lineup_status` distinguishes outcomes.** `PROJECTED` vs `UNKNOWN` lets a training
  job separate a real negative (not on a forward line) from a failed/empty fetch.
  Filter on `PROJECTED` rather than treating null as "not a top-9 forward".

### Notes

- `_log_structure` logs how many teams matched the expected 4F/6D/2G shape on every
  fetch and warns per-team on deviations, so a partial parse is visible instead of silent.
  Expect occasional "0 goalies" warnings: the article genuinely omits goalies for some teams.
- `merge_lineup_data` logs the match rate and warns below full coverage. It also warns
  when the article lists one player on two lines at once (seen live with Elias Pettersson
  on both F1 and F2) — the name-keyed lookup can only keep one, so this says so rather
  than looking like a clean parse.
- **The article double-encodes some characters.** `Ryan O’Reilly` arrives as UTF-8 bytes
  read back as latin-1, which transliterates to `OaReilly` and can never match. `_repair_mojibake`
  undoes this before transliteration; keep it ahead of `unidecode` in any name handling.
- These are **projected** lineups, published in the morning; they are not confirmed.
  `opp_goalie_*` continues to come from the RotoWire goalies table, which carries
  `Confirmed / Expected`.
- Joins use `normalize_player_name` (folds hyphens, apostrophes, accents and mojibake)
  because the existing joins match on name, not id.
- Neither source publishes an archive; only the current slate is available.



## Running this Program

First install all necessary packages:<br/>
```make local-setup```<br/>

Deploy to AWS:
*   **On Linux/macOS:**
    ```bash
    bash build_scripts/deploy.sh
    ```
    Or make it executable (`chmod +x build_scripts/deploy.sh`) and run:
    ```bash
    ./build_scripts/deploy.sh
    ```
*   **On Windows (using Git Bash or similar):**
    ```bash
    sh build_scripts/deploy.sh
    ```
*If you are on windows, ensure Docker is running with the image: "public.ecr.aws/amazonlinux/amazonlinux:2".*
<br/><br/>

## GitHub CD Configuration

The deployment pipeline expects the following GitHub repository secrets to be configured:

- `AWS_ACCESS_KEY_ID`: AWS IAM access key ID used by CI/CD for authenticated AWS API calls.
- `AWS_ACCOUNT_ID`: Target AWS account ID used for deployment targeting and resource naming.
- `AWS_SECRET_ACCESS_KEY`: AWS IAM secret key paired with the access key for CI/CD authentication.
- `BREVO_FROM_EMAIL`: Sender address used for outbound SmartScore notification emails.
- `BREVO_SMTP_KEY`: Brevo SMTP API key/password used to authenticate with the SMTP relay.
- `BREVO_SMTP_LOGIN`: Brevo SMTP login/username used with the SMTP key.
- `FEATURE_SEND_EMAILS`: Feature flag that enables or disables sending emails at runtime.
- `SUPABASE_API_KEY`: Supabase anon/public API key used by the default client.
- `SUPABASE_DB_URL`: Postgres connection string for the Supabase project, used by CI to apply `supabase/migrations/*.sql`.
- `SUPABASE_SERVICE_ROLE_KEY`: Supabase service-role key used for privileged server-side operations.
- `SUPABASE_URL`: Base URL for the Supabase project used by application clients.

## Feature Flags

Feature flags are stored as environment variables and read at runtime.

- `FEATURE_SEND_EMAILS`: Controls whether SmartScore sends user notification emails.
    - `true`, `1`, `yes`, `on` => enabled
    - `false`, `0`, `no`, `off` (or unset if changed in code defaults) => disabled

Example:

```bash
FEATURE_SEND_EMAILS=false
```

*Note: This program is intended for informational purposes only and does not facilitate actual betting. Users should exercise their own judgment and discretion when using the provided suggestions for betting purposes.*

<br/>

# ALL CONSIDERED STATISTICS  

- **Goals Per Game (GPG)**: The most telling stat when determining if a player will score a goal. It implicitly considers factors like time on ice, shots per game, shot-to-goal ratio, etc.  
- **Goals Per Game in Last 5 Games (5GPG)**: Captures hot streaks (and cold streaks).  
- **Historic Goals Per Game (HGPG)**: A player's GPG over the last three seasons. Especially useful at the beginning of a new season when current GPG may be skewed.  
- **Team's Goals Per Game (TGPG)**: Useful if a player is traded to a new team.  
- **Other Team's Goals Against (OTGA)**: Captures the opposing team's defensive strength, factoring in defense quality, goalie performance, etc. This is independent of a player's GPG, which reflects their scoring average, whereas OTGA varies with each game.  
- **Home or Away**: Helps identify patterns in a player's goal-scoring performance based on location.  
- **Historic Power Play Goals (HPPG)**: A player's power play goals over the last three seasons (since these are relatively rare, one season alone might be misleading). Can be combined with **Other Team's Shorthanded Goals Against Per Game** for a more comprehensive stat.  
- **Other Team's Shorthanded Goals Against Per Game (OTSHGA)**: Measures how many shorthanded goals a team allows per game. Can be combined with **HPPG** for a more meaningful composite stat. Also implicitly considers other team's penalty minutes, penalty kill percentage, etc.  

### Stats Already Covered by Others:  
- **Time on Ice** → Covered by GPG.  
- **Shots Per Game** → Covered by GPG.  
- **Shot Percentage** → Covered by GPG.  
- **Other Team's Penalty Minutes** → Covered by OTSHGA.

