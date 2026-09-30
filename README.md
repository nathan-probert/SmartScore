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
- Goalie + skater stats: official NHL API `api-web.nhle.com` (`goalie-stats-leaders/current`, `player/{id}/landing`, `player/{id}/game-log/now`, `club-stats/{team}/now`, `gamecenter/{gameId}/boxscore` with `starter=true` for backfill)
- Recorded per skater as `opp_goalie_*` fields (name, team, status, GAA, save %, record) via `handle_get_goalies` between `GetInjuries` and `GetTims`
- DB: apply `supabase/migrations/20260930_add_starting_goalie_columns.sql` in the Supabase SQL editor (Picks + Historic tables, dev + prod) before deploying, or upserts will fail on the new columns

> If RotoWire tables endpoints ever change/break (undocumented, embedded via `loadTableRW` in `starting-goalies.php` / `injury-report.php`), use [DailyFaceoff](https://www.dailyfaceoff.com/starting-goalies/) as fallback for both starting goalies (`Confirmed / Likely` + timestamp + source, server-rendered HTML, scrapable with BeautifulSoup) and injuries ([line combos / injury list](https://www.dailyfaceoff.com/teams/)). Note RotoWire team codes differ from NHL API (`MON` vs `MTL`, `LAS` vs `VGK`) so keep the map in sync.

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

