# Spec: Supabase columns the SmartScore ML ensemble needs to train/predict purely in-cloud

Written against `smartscore/scripts/backtrack/model.py` and
`smartscore/scripts/backtrack/local_store.py` at **c82b2e7** (branch
`feat/feed-fidelity`). Goal: option 1 of the AWS free-tier plan — publish
the columns that are currently computed locally against
`data/raw_nhl.sqlite` so `model.py` (`shot_features()`, `club_map()`,
`add_derived()`) never needs the SQLite file. This doc changes **no code
and runs no SQL**; it is the decision table the owner reviews before
approving the migration.

## 0. Today's state (verified)

**Table naming.** `model.py` builds names as `f"Player-Snapshots-backtrack-{ENV}"`
with `ENV ∈ {dev, prod}` (`smartscore/config.py`), so the dev tables are
`Player-Snapshots-backtrack-dev`, `Team-Stats-backtrack-dev`,
`Goalie-Snapshots-backtrack-dev`. Every migration in `supabase/migrations/`
uses the `__ENV__` placeholder and is re-piped through psql on every deploy
(no migration history); all statements must stay idempotent.

**Row counts (from the local model caches of the dev tables, Oct 2026):**

| table | rows | date range |
|---|---|---|
| `Player-Snapshots-backtrack-dev` | **617,310** | 2013-10-01 .. 2026-06-14 |
| `Team-Stats-backtrack-dev` | 34,300 | full published window |
| `Goalie-Snapshots-backtrack-dev` | 36,696 | full published window |

**Current player-snapshot columns** (order from the fetched dev CSV):
`date, player_id, name, team_name, home, gpg, hgpg, five_gpg, hppg, tgpg,
otga, otshga, injury_status, injury_desc, tims, lineup_unit,
lineup_position_group, pp_unit, lineup_status, scored, ppg`
— `tgpg/otga/otshga` are declared but **left null by design** (they live at
their honest grain in Team-Stats-backtrack). Key: `PRIMARY KEY (date, player_id)`,
upserted by `local_store.publish()` in 500-row batches with
`on_conflict="date,player_id"`. One row **per player per game date**, i.e.
the row for date D already reads "entering game D" (strictly-before
cutoff). Team rows key `(season, team_abbrev, game_id)`; goalie rows key
`(season, player_id, date)`.

**What publish() sends today** (local_store.py publish(), ~line 1913):
`date, player_id, name, team_name, home, gpg, five_gpg, ppg, hgpg, hppg,
tims, scored, lineup_unit, lineup_position_group, pp_unit, lineup_status`,
sourced from local `derived_features` + `player_games` + `player_lineups`
+ `tims_daily`. `publish_team()` / `publish_goalies()` fill the other two
tables. PostgREST **rejects an entire batch that names an unknown column**
— the migration must land before any publish gains new keys.

**The 42-feature count.** `DATASET_FEATURES` at c82b2e7 = 40 numeric +
2 categorical = **42** entries (the "43" in the deployment-plan report
appears to have counted the `scored` label). The ledger in §4 sums to 42
every line accounted; the optional `--lineup projected` mode adds 8 more
(`proj_lineup_unit_b, proj_pp_unit_b, proj_line_conf, proj_pp_conf,
linemate_prior_gpg_mean, linemate_prior_ppg_mean, pp_unit_prior_ppg_mean,
line_intact`) which are **out of scope** for this spec but share the same
derivation paths and would also publish cleanly on the same PK.

---

## 1. Decision table — Block A: per-player-game shot/TOI columns (14 new)

All on **`Player-Snapshots-backtrack-{ENV}`**, riding the existing
`(date, player_id)` PK — no key change. Every column is an aggregate over
the player's **strictly earlier games** (exact port of `shot_features()`,
model.py:379-444), so the row's values are identical whether computed
locally pre-publish or consumed post-publish.

| column | table | semantics / formula (exactly as `shot_features()` computes it) | source (local store) | grain | dtype | null policy | backfill est. |
|---|---|---|---|---|---|---|---|
| `sd_shots_pg` | Player-Snapshots | season-to-date shots/game = Σ shots over the player's games **in the same season, game_date < D** ÷ count of those games | `player_games.shots` (grid: player_id, game_date, season) | per player-game | `DOUBLE PRECISION` | NULL when 0 prior games in the season (season-first game; code emits NaN) | 617,310 rows touched (upsert; ~447k non-null) |
| `sd_goals_pg` | Player-Snapshots | season-to-date goals/game, same strictly-before frame | `player_games.goals` | per player-game | `DOUBLE PRECISION` | NULL on season-first game | 617,310 |
| `sd_toi_pg` | Player-Snapshots | season-to-date TOI/game in **decimal minutes**; raw `toi` is TEXT `"mm:ss"` → `mm + ss/60` (model.py:401-407) | `player_games.toi` | per player-game | `DOUBLE PRECISION` | NULL on season-first game, or unparseable/absent toi | 617,310 |
| `sd_sh_pct` | Player-Snapshots | season-to-date shooting talent = `goals_before / shots_before` (Σ goals ÷ Σ shots, NOT a mean of per-game pct) | `player_games.goals, .shots` | per player-game | `DOUBLE PRECISION` | NULL when `shots_before = 0` or 0 prior games | 617,310 |
| `r5_shots_pg` | Player-Snapshots | mean shots over the last ≤5 prior games (spans season boundaries; `shift(1).rolling(5, min_periods=1).mean()` per player) | `player_games.shots` | per player-game | `DOUBLE PRECISION` | NULL before the player's ≥2nd game overall | 617,310 |
| `r5_goals_pg` | Player-Snapshots | mean goals over last ≤5 prior games | `player_games.goals` | per player-game | `DOUBLE PRECISION` | NULL before 2nd game | 617,310 |
| `r10_shots_pg` | Player-Snapshots | mean shots over last ≤10 prior games | `player_games.shots` | per player-game | `DOUBLE PRECISION` | NULL before 2nd game | 617,310 |
| `r10_goals_pg` | Player-Snapshots | mean goals over last ≤10 prior games | `player_games.goals` | per player-game | `DOUBLE PRECISION` | NULL before 2nd game | 617,310 |
| `r10_toi_pg` | Player-Snapshots | mean decimal-minute TOI over last ≤10 prior games | `player_games.toi` | per player-game | `DOUBLE PRECISION` | NULL before 2nd game | 617,310 |
| `ewm_goals` | Player-Snapshots | exponentially-weighted mean of prior goals, halflife **5 games**, over `shift(1)` per-player series | `player_games.goals` | per player-game | `DOUBLE PRECISION` | NULL before 2nd game | 617,310 |
| `ewm_shots` | Player-Snapshots | EWM of prior shots, halflife 5 | `player_games.shots` | per player-game | `DOUBLE PRECISION` | NULL before 2nd game | 617,310 |
| `career_games` | Player-Snapshots | count of the player's games strictly before D, all seasons (`cumcount()` after sort by player_id, date, season) | `player_games` rows | per player-game | `INTEGER` (fits int32; int is exact) | never null (0 on debut) | 617,310 |
| `career_goals_pg` | Player-Snapshots | expanding mean of prior games' goals (all seasons): Σ prior goals ÷ prior games | `player_games.goals` | per player-game | `DOUBLE PRECISION` | NULL before 2nd game | 617,310 |
| `career_shots_pg` | Player-Snapshots | expanding mean of prior shots | `player_games.shots` | per player-game | `DOUBLE PRECISION` | NULL before 2nd game | 617,310 |

**Ordering rule the backfill must reproduce to the decimal:** rows sorted
by `(player_id, game_date, season)`, then the per-player/season window
functions applied with a one-row lag (`shift(1)`), so the current game's
own line never enters its feature row — the pre-game/fold-safety invariant
the code comments lean on. Publishing precomputed rather than recomputing
preserves this *only if* done from the same sort; the safest backfill is
"run `shot_features()` verbatim and upsert its output" (sketch in §5).

Nothing new is needed on `Team-Stats-backtrack-{ENV}` or
`Goalie-Snapshots-backtrack-{ENV}` — their rates (tgpg/otga/otshga,
gaa/save_pct/record/shutouts/games_played) already cover the team and
opposing-goalie features the model joins at read time.

---

## 2. Decision table — Block B: team-abbreviation columns (2 new)

`club_map()` (model.py:270-287) joins `player_games.team_abbrev /
opponent_abbrev` per `(date, player_id)` to build the `club` / `opp`
join keys. The Supabase snapshot's only team identity is `team_name` =
the NHL **place name**, which renders both New York clubs as
`"New York"` (NYR and NYI share dates constantly) — a name merge cannot
resolve which club a player skated for. (`New Jersey` is unambiguous,
`New York` is not; also `Los Angeles Kings`-style full names never
appear, this column is place names only.)

Recommendation: **per-row adjective-free abbreviation columns on the
snapshot**, not a separate date→club map table. The snapshot rows are
already one-per-player-game with their own PK and publish path, `club_map`
is keyed `(date, player_id)` anyway, and a side lookup table would add a
third join plus its own publish pipeline for zero columns saved.

| column | table | semantics / formula | source (local store) | grain | dtype | null policy | backfill est. |
|---|---|---|---|---|---|---|---|
| `team_abbrev` | Player-Snapshots | the club the player skated for in the row's own game (NHL 3-letter: TOR, NYR, NYI, ...) | `player_games.team_abbrev` keyed `(player_id, game_date)` | per player-game | `TEXT` | NULL only if the `player_games` row is missing (should not occur; publish fails closed today) | 617,310 |
| `opponent_abbrev` | Player-Snapshots | the opponent of that game | `player_games.opponent_abbrev` | per player-game | `TEXT` | NULL same as above | 617,310 |

With these two columns everything `club_map()` supplies lives in Supabase:
`club` := `team_abbrev`, `opp` := `opponent_abbrev`, and the Team-Stats /
Goalie-Snapshots joins in `build_frame()` (model.py:314-334) resolve by
abbreviation instead of place name. **Minimal set = these 2 columns.**
Do not join by `team_name`/placeName under any circumstances (the
"New York" trap), and do not re-introduce `team_abbr` as a name on the
live `Player-Snapshots-{ENV}` table — that migration explicitly forbid it
(see 20261002 header note); this spec only touches the `backtrack` tables.

---

## 3. Feature ledger — the full DATASET_FEATURES cross-check (c82b2e7 = 42)

Categories:
**A** = `ALREADY-IN-SUPABASE` (direct column, or joined from another backtrack table at read time),
**S** = `ADDED-BY-THIS-SPEC` (§1/§2),
**D** = `DERIVABLE-AT-RUNTIME` (computed in `add_derived()`/`build_frame()`, all inputs are published columns — a cloud pipeline replicates it in pandas/SQL with no SQLite).

| # | feature | kind | source of truth / proof |
|---|---|---|---|
| 1 | `gpg` | A | columns exist: `Player-Snapshots.gpg` (base migration 20261008) |
| 2 | `five_gpg` | A | `Player-Snapshots.five_gpg` exists |
| 3 | `ppg` | A | `Player-Snapshots.ppg` added by 20261009 migration |
| 4 | `hgpg` | A | `Player-Snapshots.hgpg` exists (3-yr window via compute_historic) |
| 5 | `hppg` | A | `Player-Snapshots.hppg` exists |
| 6 | `home` | A | `Player-Snapshots.home` BOOLEAN; model renders 1.0/0.0 (`build_frame`:351) — cast at read |
| 7 | `tims` | A | `Player-Snapshots.tims` (categorical feed from tims_daily) |
| 8 | `lineup_position_group` | A | `Player-Snapshots.lineup_position_group` ("F"/"D" from shift charts) |
| 9 | `tgpg` | A | `Team-Stats-backtrack.tgpg` joined on (date, club) |
| 10 | `otga` | A | `Team-Stats-backtrack.otga` (opponent-specific rate) |
| 11 | `otshga` | A | `Team-Stats-backtrack.otshga` |
| 12 | `opp_gaa` | A | `Goalie-Snapshots-backtrack.gaa` joined on (date, opp); most-experienced row wins ties |
| 13 | `opp_save_pct` | A | `Goalie-Snapshots.save_pct` |
| 14 | `opp_shutouts` | A | `Goalie-Snapshots.shutouts` |
| 15 | `opp_games_played` | A | `Goalie-Snapshots.games_played` |
| 16 | `opp_wins` | A | parse of `Goalie-Snapshots.record` "W-L-OTL" (`build_frame`:345-348) |
| 17 | `opp_losses` | A | parse of `record` |
| 18 | `opp_otl` | A | parse of `record` |
| 19 | `rank_gpg` | D | `rank(pct=True)` within (date, tims) over published `gpg`+`tims` (`add_derived`:643-645) |
| 20 | `rank_hgpg` | D | same over published `hgpg` |
| 21 | `rank_five_gpg` | D | same over published `five_gpg` |
| 22 | `rank_ppg` | D | same over published `ppg` |
| 23 | `prev2_goals` | D | rolling(2) sum of published `scored`, shift(1) per player (`add_derived`:588-592) |
| 24 | `prev3_goals` | D | rolling(3) sum of published `scored` |
| 25 | `games_since_goal` | D | streak over published `scored` history, cap 15 (`_since_last_goal`) |
| 26 | `days_since_game` | D | date gap to the player's previous published row (`add_derived`:594) |
| 27 | `club_rest_days` | D | date gap per club in published `Team-Stats (date, team_abbrev)` (`add_derived`:598-615) |
| 28 | `opp_rest_days` | D | same on the opponent's club rows |
| 29 | `sd_shots_pg` | S | needs `player_games.shots` sum — NOT published anywhere in Supabase today |
| 30 | `sd_goals_pg` | S | needs `player_games.goals` |
| 31 | `sd_toi_pg` | S | needs `player_games.toi` (TEXT mm:ss → decimal minutes) |
| 32 | `sd_sh_pct` | S | needs `player_games.goals ÷ shots` sums |
| 33 | `r5_shots_pg` | S | last-5 window over `player_games.shots` |
| 34 | `r5_goals_pg` | S | last-5 over `player_games.goals` |
| 35 | `r10_shots_pg` | S | last-10 over `player_games.shots` |
| 36 | `r10_goals_pg` | S | last-10 over `player_games.goals` |
| 37 | `r10_toi_pg` | S | last-10 over `player_games.toi` |
| 38 | `ewm_goals` | S | EWM(halflife=5) over `player_games.goals` |
| 39 | `ewm_shots` | S | EWM(halflife=5) over `player_games.shots` |
| 40 | `career_games` | S | count over `player_games` rows |
| 41 | `career_goals_pg` | S | expanding mean over `player_games.goals` |
| 42 | `career_shots_pg` | S | expanding mean over `player_games.shots` |

**Sum: 42 = 18 ALREADY-IN-SUPABASE (rows 1-18) + 10 DERIVABLE-AT-RUNTIME
(rows 19-28) + 14 ADDED-BY-THIS-SPEC (rows 29-42). No feature silently
missing; after Block A+B, every number the model consumes lives in
Supabase.** The label `scored` (not a feature) is already column 20 of the
snapshot table; `club`/`opp` join keys come back as internal columns via
Block B.

Why S and not D for the shot block: `D` requires the per-game **raw**
counters (each game's shots/toi) to be in Supabase. They are not, and
publishing them raw instead would (a) violate the store-design decision
"in this table a rate cannot be recomputed from Supabase alone requires
the SQLite file" being *the intentional trade* (20261008 migration
header), or (b) mean swapping the source of truth to Supabase — a much
bigger decision than an additive column set. Precomputing the same
strictly-before features publish-side keeps SQLite as source of truth and
is the smallest cloud-neutral change.

---

## 4. SQL migration sketch (NOT executed by this PR)

> **STAGE-2 (2026-10-27) — per-game RAW columns.** Supabase is the LIVE data
> plane. The rolling/career block above stays the training-side history; the
> picks pass now RECOMPUTES it exactly from the published per-game record.
> `supabase/migrations/20261027_add_per_game_raw_columns_player_snapshots_
> backtrack.sql` adds the per-player-game columns:
>
> ```sql
> ALTER TABLE "Player-Snapshots-backtrack-__ENV__"
>     ADD COLUMN IF NOT EXISTS "game_goals"   INTEGER,
>     ADD COLUMN IF NOT EXISTS "game_shots"   INTEGER,
>     ADD COLUMN IF NOT EXISTS "game_points"  INTEGER,
>     ADD COLUMN IF NOT EXISTS "game_toi_min" DOUBLE PRECISION;
> ```
>
> The published per-game column list is therefore the FOUR raw columns
> (`game_shots` / `game_goals` / `game_points` / `game_toi_min`) beside the
> 14 precomputed aggregates + 2 club columns of Blocks A/B: goals/shots/toi
> are exactly what `model.shot_features_from_games` consumes (the picks
> pass's verbatim recompute core); `game_points` rides along as the
> per-game provenance the trending rates quote (no feature consumes it);
> no `game_played` — row existence/non-null `game_shots` IS the played
> flag (a boxscore lists exactly the players who appeared). Values ride
> the SAME upsert paths: `publish_shot_cols.attach_shot_columns` attaches
> them nightly, the one-shot `publish_shot_cols.py` top-up
> (`topup_raw_game_cols.py`) fills history, and `backfill_missing_days.py`
> creates/only-top-ups missed recent days (2-5 bounded NHL API requests,
> FINAL-state games only, per-date markers for idempotency).

One migration file, `supabase/migrations/2026MMDD_add_shot_columns_player_snapshots_backtrack.sql`,
`__ENV__` placeholder, idempotent because deploy.yml re-pipes it through psql:

```sql
-- Shot/TOI rolling block + club identity per player-game (see
-- docs/supabase_shot_columns.md). Row values are prior-games-only
-- aggregates computed by local_store from data/raw_nhl.sqlite; publish()
-- overwrites them on the (date, player_id) upsert.
ALTER TABLE "Player-Snapshots-backtrack-__ENV__"
    ADD COLUMN IF NOT EXISTS "sd_shots_pg"      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "sd_goals_pg"      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "sd_toi_pg"        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "sd_sh_pct"        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r5_shots_pg"      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r5_goals_pg"      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r10_shots_pg"     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r10_goals_pg"     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "r10_toi_pg"       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "ewm_goals"        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "ewm_shots"        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "career_games"     INTEGER,
    ADD COLUMN IF NOT EXISTS "career_goals_pg"  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "career_shots_pg"  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS "team_abbrev"      TEXT,
    ADD COLUMN IF NOT EXISTS "opponent_abbrev"  TEXT;

-- Optional: serves the (date, club) join Team-Stats lookups prefer.
-- The existing PK (date, player_id) already leads with date, so this
-- index is only worth adding if the planner churns on it in practice.
-- CREATE INDEX IF NOT EXISTS "Player-Snapshots-backtrack-__ENV___club_idx"
--     ON "Player-Snapshots-backtrack-__ENV__" ("team_abbrev", "date");
```

No PK change (still `date, player_id`), no key breakage, no Team-Stats or
Goalie-Snapshots columns.

**One-shot backfill pattern** (python side; a `--publish-shots/--rebuild`
flag or a one-off `local_store.py` mode):

1. Load `player_games` from `data/raw_nhl.sqlite` and run the **verbatim**
   `model.shot_features()` (its columns are exactly these 14) — do not
   re-derive with SQL window functions; the naive-SQL reproduction of the
   pandas EWM/rolling sequences is where silent decimal drift lives.
2. Pull `(player_id, game_date) → team_abbrev, opponent_abbrev` from
   `player_games` (that is `club_map()` minus the unused columns).
3. Restrict to rows whose `(date, player_id)` exists in the snapshot table
   (dev shows 617,310 such rows; the remaining ~1% of skater rows in the
   store belong to dates/players publish never wrote).
4. Upsert in 500-row batches, `on_conflict="date,player_id"`, `returning="minimal"`
   — identical to `publish()`. Convert `NaN → None` first (see §5).

Incremental afterwards: `publish()` gains the 16 keys in its `SELECT` +
payload dict (one SELECT per derived join); **no extra rows are written** —
the columns ride the ~247 skater-row + ~24 team-row + goalie daily upserts
that a normal game day costs. The only daily increment is post-2026-10
rows for new dates (~220-260 player rows on a full slate); the shot
columns are already folded into those.

---

## 5. Gotchas the migration/publish must handle

1. **season dtype TEXT ↔ int64.** The Supabase columns store `season` as
   TEXT `"20232024"`; the CSV caches round-trip it as numpy int64 and the
   raw store carries INTEGER. Every join/merge must normalise both sides
   via `astype("Int64").astype(str)` exactly as `build_frame()`/`shot_features()`
   do (model.py:286-299, 397). A bare int64 season with a null (nullable
   `Int64` → "<NA>") will not text-match anything. The new columns
   sidestep this by never shipping season-derived strings, but the
   backfill job still merges on `season` internally and must normalise.
2. **NaN is not JSON.** The rolling block is NaN for the first game of a
   career/season by design. supabase-py + PostgREST require `None`, not
   float `nan`, in the upsert payload (bare NaN serialises as non-JSON `NaN`
   and the batch dies). Apply `df.where(pd.notna(df), None)` (or `None`
   in the dict comprehension) before `upsert`.
3. **"New York" ambiguity.** Joining any snapshot or team data by
   `team_name` on a co-slated date risks mixing NYR/NYI everywhere.
   This is precisely why Block B ships `team_abbrev`/`opponent_abbrev`
   and why `club_map()` exists locally; the cloud pipeline must join
   Team-Stats on `(date, team_abbrev)` and Goalie-Snapshots on
   `(date, opponent_abbrev)`, never the place name.
4. **TOI is `"mm:ss"` TEXT.** Store minutes as decimal (mm + ss/60) in the
   DOUBLE PRECISION column — the model consumes decimal minutes
   (model.py:401-407); storing the string would trade one epoch of the
   same dtype problem downstream.
5. **Publish order.** Migration first, then any publish that carries the
   new keys — PostgREST 400s (and thereby *whole-batch-drop*) on unnamed
   columns; the idempotent deploy re-run makes the safe order simply
   "always merge the migration before the code change".
6. **upsert batch size 500** (PostgREST 1000-row default cap; backfill of
   617,310 rows = ~1,235 requests — several minutes, one-time only).
7. **Backfill idempotency.** The upsert keys on the PK, so re-running the
   backfill is a safe overwrite, not an append.

---

## 6. Owner sign-off summary

* **Total new columns: 16** — 14 shot/TOI rolling features + 2 club
  abbreviation columns, all on `Player-Snapshots-backtrack-{ENV}`.
* **Total backfill rows: 617,310 one-time upserts** on the dev table
  (same row count for prod at promotion; no new rows created), plus barn
  rows on new dates after (~250/day, existing pipeline).
* **Zero schema changes** to Team-Stats-backtrack / Goalie-Snapshots-backtrack.
* **Before/after cloud parity check:** the acceptance test for the
  migration is that `model.py --env dev` produces identical DATASET rows
  when `shot_features()/club_map()` read the published columns instead of
  `data/raw_nhl.sqlite` (same strictly-before cutoff, same sort).
