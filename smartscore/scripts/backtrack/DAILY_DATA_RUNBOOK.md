# DAILY_DATA_RUNBOOK — daily day-of data capture (crawl → derive → publish → projected scrapes)

Working directory is always `/home/nathan/code/SmartScore-backtrack`
(branch `feat/feed-fidelity` @ c82b2e7 at the time of writing; the companion
`PROJECTED_LINEUP_RUNBOOK.md` lives beside this file). All times are
America/Toronto (ET).

This runbook is verified against the 2026-10-24 day-of run; every claim below
cites the command, state file, log line, or script that proves it.

## Source of truth at a glance

| Layer | Where | Written by |
|---|---|---|
| HTTP cache (raw NHL payloads) | `data/nhl_cache_*` (SQLite), committed dumps | `reconstruct.py --write` (crawl) |
| Local raw/derived store | `data/raw_nhl.sqlite` | `local_store.py --build / --derive` |
| Supabase training tables | `Player-Snapshots-backtrack-{ENV}`, `Team-Stats-backtrack-{ENV}`, `Goalie-Snapshots-backtrack-{ENV}` | `local_store.py --publish / --publish-team / --publish-goalies` |
| Projected pre-game snapshots | `Player-Snapshots-{ENV}` | `snapshot_projected.py --env {ENV}` (10:30 / 13:00 / 16:00 ET) |

ENV handling (verified from `smartscore/config.py` + repo `.env`): `.env`
contains `ENV=prod`, and `local_store.py` resolves the target tables via
`config()` → `f"Player-Snapshots-backtrack-{env_name}"` (local_store.py:1955).
A shell run without an explicit `ENV` therefore publishes to **prod** — always
pass `ENV=` explicitly.

## Daily operational order

### Step 1 — Incremental crawl + local build (morning)

```
.venv/bin/python smartscore/scripts/backtrack/run_background.py \
    --name bt-202627e --cmd build --season 20262027
```

`--cmd build` launches `local_store.py --build 20262027` detached
(run_background.py:91-94), which crawls only what the HTTP cache misses
(schedule, box scores, player game logs), then reconstructs lineups, ingests
challenge feeds, and derives — all inside one process. State and log land in
`data/bg/<name>.json` / `.log`; poll with `--name <name> --status` / `--tail`.

Dependency: needs nothing newer than yesterday's crawl (the cache makes it
incremental). Yesterday's games are complete by morning; today's slate rows
appear only as evening box scores land (the 2026-10-24 rows/day block below
shows the natural taper: 480 → 80 → 40).

### Step 2 — Derived features (usually already inside Step 1)

`--build` ends with the derive pass — quote from `data/bg/bt-202627e.log`:

```
20262027: hgpg/hppg computed for 6720 row(s) over window ['20252026', '20242025', '20262027']
20262027: goalie_features computed for 348 appearance row(s)
derived features computed for 20262027
```

If the raw layer was filled by other means, re-derive standalone:
`make backtrack-derive SEASON=20262027` (Makefile:80-82) — the Makefile's own
comment: "--derive runs first so the SQL, not a stale table, is what ships".

Dependency: needs Step 1's raw tables (`player_games`, `player_lineups`,
`shifts`, `tims_daily`) in `data/raw_nhl.sqlite`.

### Step 3 — Publish to Supabase backtrack tables (this is the step that did NOT run today)

```
make backtrack-publish SEASON=20262027 ENV=prod     # or ENV=dev while iterating
```

Makefile:99-107 expands to (in order):

```
make backtrack-derive SEASON=20262027
ENV=prod uv run python smartscore/scripts/backtrack/local_store.py --publish 20262027
ENV=prod uv run python smartscore/scripts/backtrack/local_store.py --publish-team 20262027
ENV=prod uv run python smartscore/scripts/backtrack/local_store.py --publish-goalies 20262027
```

Equivalent detached runner: `run_background.py --cmd publish --season 20262027
--env prod` (local_store.py `--publish`, run_background.py:100-103). All three
writes upsert (player rows on `(date, player_id)`, team rows on
`(season, team_abbrev, game_id)`, goalie rows on `(season, player_id, date)`),
so re-running is safe (Makefile comment, lines 95-99).

Dependencies, each evidenced:

1. Step 1/2 done — publish reads only `data/raw_nhl.sqlite` (local_store.py
   :1896, "Reads from the raw store rather than re-crawling: that is the point
   of the raw layer").
2. **Migration must land BEFORE the first publish carrying the 16 new
   shot/TOI columns.** They land on `Player-Snapshots-backtrack-{ENV}` via the
   in-flight sibling migration
   `supabase/migrations/20261024_add_shot_columns_player_snapshots_backtrack.sql`
   on branch `feat/supabase-shot-cols` (verified via `git ls-tree`), per the
   PR #138 spec ("migration-before-publish ordering" is the documented
   gotcha). Reason: publish sends only columns the table declares —
   "PostgREST rejects an entire batch on an unknown column" (local_store.py
   publish() docstring, :1906-1908). The columns ride the existing
   `(date, player_id)` PK through the upsert (migration header), and the
   feature-aggregation semantics are per PR #138: strictly-prior-games
   aggregates over `player_games` (exact ports of `shot_features()`,
   shift(1) on `(player_id, date, season)`); NaN→None before upsert; season
   TEXT; TOI "mm:ss"→decimal minutes.
3. Concurrency note for monitoring day-of: while a sibling worktree
   (`/home/nathan/code/SmartScore-shotcols`, `feat/supabase-shot-cols`) runs
   its one-shot 16-column backfill (`publish_shot_cols.py --env dev`, observed
   live 2026-10-24 15:03+ via `ps`), transient PostgREST gateway 4xx can show
   up in read-side checks (`daily_sanity.py` produced a transient
   `FAIL: GET ... -> 404` / `-> 400` pair at ~15:10; the identical queries
   returned 200 on retry minutes later — probes recorded via raw
   `requests.get` against `/rest/v1/`). Prefer re-running to re-diagnosing.

### Step 4 — Projected-lineup scrapes, 10:30 / 13:00 / 16:00 ET

Per `PROJECTED_LINEUP_RUNBOOK.md` (its §Schedule): every day of the season,
`.venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env
prod` (and `--env dev` while iterating) at 10:30, 13:00, 16:00 ET. Writes one
row per `(date, player_id)` to `Player-Snapshots-{ENV}`; refresh only fills
blanks, never overwrites non-null values or `CONFIRMED`/`ACTUAL` rows, so
re-runs are strictly additive and idempotent.

Dependency: the `PROJECTED_PROXY` tier reads prior deployments from
`data/raw_nhl.sqlite` `player_lineups` — `PROJECTED_LINEUP_RUNBOOK.md`
§Known limits: "Proxy lineups stay at last season's final deployments until
the current season's shift-chart crawl is rebuilt in `data/raw_nhl.sqlite`".
So a fresh Step 1 build strictly improves the proxy tier; as of 2026-10-24 the
current season is in the store (5004 lineup rows quoted below), which is why
today's snapshot rows carry real D-pair units.

### Step 5 — Morning sanity (read-only monitor)

```
.venv/bin/python smartscore/scripts/backtrack/daily_sanity.py --env both
```

See the Monitoring section for today's actual outputs.

## What breaks if you swap / reorder steps

- **Publish before build/derive:** publish is a pure reader of
  `data/raw_nhl.sqlite`; with a stale store it re-upserts yesterday's rows
  (harmless, but the day's new rows never ship — exactly today's state: the
  published dev table's latest date is still 2026-06-14). With a half-built
  store it can ship NULLed feature columns; the Makefile runs `backtrack-derive`
  inside `backtrack-publish` specifically so "the SQL, not a stale table, is
  what ships" (Makefile:95-96).
- **Publish (16-col payload) before the migration:** a feature that exists
  locally but has no column in Supabase "would fail the whole publish rather
  than just be skipped" (local_store.py:1906-1908) — PostgREST rejects the
  entire batch, so the day's upsert lands nowhere.
- **Scrapes before build:** not a hard failure (scrapes are additive), but the
  proxy tier would fall back to stale deployments (Known limits quote above)
  until the shift-chart rebuild lands.
- **Build twice without stopping the first:** `run_background.py --cmd` refuses
  to start over a live pid ("is already running… Use --stop first",
  run_background.py:87-89); the guard reads the pid from the state file, which
  does not carry a done/running marker — liveness is pid-only
  (`_pid_alive`, run_background.py:60-80). A finished run's state file simply
  remains; verify completion with `--status` (exit 1 when not running), which
  today shows `pid : 3830155 (not running)` after "derived features computed".
- **Sanity before the build/publish finishes:** the 60 s read-connection
  timeout (`daily_sanity.py:49-57`) exists because "the daily build can hold
  the write lock for a long derive pass"; running it mid-build risks
  `database is locked` and reading a pre-derive store.

## Crontab / systemd — PROPOSAL for the owner (nothing was installed)

The daily increment is currently run by hand (today's state file `bt-202627e.json`
records a manual launch, 2026-10-24T14:43, `env: dev`). Example to install
(owner decision; uses the already-verified command chain; a finished Step 1 is
pollable via `--status`, but running `local_store.py --build` directly keeps
the cron job synchronous and lets `&&` sequence the publish):

```cron
# daily day-of data increment: build the local store, derive, publish all three tables
CRON_TZ=America/Toronto
0 9 * * *  cd /home/nathan/code/SmartScore-backtrack && ENV=prod .venv/bin/python smartscore/scripts/backtrack/local_store.py --build 20262027 >> data/bg/daily_increment.log 2>&1 && ENV=prod make backtrack-publish SEASON=20262027 ENV=prod >> data/bg/daily_increment.log 2>&1 && .venv/bin/python smartscore/scripts/backtrack/daily_sanity.py --env both >> data/bg/daily_increment.log 2>&1
```

systemd equivalent (preferred on systemd hosts, matches the
PROJECTED_LINEUP_RUNBOOK's timer pattern):

```ini
# /etc/systemd/system/daily-data-increment.service
[Unit]
Description=Daily backtrack increment: local build + derive + Supabase publish

[Service]
Type=oneshot
WorkingDirectory=/home/nathan/code/SmartScore-backtrack
Environment=PATH=/home/nathan/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=ENV=prod
ExecStart=/home/nathan/code/SmartScore-backtrack/.venv/bin/python smartscore/scripts/backtrack/local_store.py --build 20262027
ExecStart=/bin/make backtrack-publish SEASON=20262027 ENV=prod
ExecStart=/home/nathan/code/SmartScore-backtrack/.venv/bin/python smartscore/scripts/backtrack/daily_sanity.py --env both
StandardOutput=append:/home/nathan/code/SmartScore-backtrack/data/bg/daily_increment.log
StandardError=inherit
```

```ini
# /etc/systemd/system/daily-data-increment.timer
[Unit]
Description=Daily backtrack increment at 09:00 ET

[Timer]
OnCalendar=*-*-* 09:00 America/Toronto
Persistent=true

[Install]
WantedBy=timers.target
```

(The projected-lineup scrapes keep their own schedule from
`PROJECTED_LINEUP_RUNBOOK.md`: 10:30 / 13:00 / 16:00 ET.)

## Monitoring — actual daily_sanity.py output as the worked example

`daily_sanity.py` is read-only (48-57, 120-148: read-only SQLite connection +
GET-only REST calls; "note: this script only SELECTs; it never writes
Supabase"). Captured verbatim on 2026-10-24 (`--env both`, exit 0):

```
note: this script only SELECTs; it never writes Supabase
== local store: raw_nhl.sqlite ==
20262027 player_games latest game date : 2026-10-24 (26 dates, 6720 rows)
20262027 player_lineups (reconstructed)  : 5004 rows
20262027 shifts games covered            : 139
20262027 tims_daily latest                : 2026-10-23 (1920 rows)
20262027 derived_features rows            : 6720
20262027 derived_team_stats rows          : 336
last three game dates in the store (rows/day):
    2026-10-24: 40 rows
    2026-10-23: 80 rows
    2026-10-22: 480 rows

== Supabase --env dev ==
Player-Snapshots-dev @2026-10-24 rows: 549 by_status={'PROJECTED': 184, 'UNKNOWN': 365}
    rows with D units (D1-D3 or group=D): 87
    rows with PP1: 118
Player-Snapshots-backtrack-dev latest date: 2026-06-14 (36 rows on that date)
note: published table lags the local store - publish is a separate step; run make backtrack-publish after the build
== Supabase --env prod ==
Player-Snapshots-prod @2026-10-24 rows: 548 by_status={'PROJECTED': 183, 'UNKNOWN': 365}
    rows with D units (D1-D3 or group=D): 87
    rows with PP1: 118
Player-Snapshots-backtrack-prod: no rows (empty table: publish has never run for this env)
```

Reading it on 2026-10-24:

- **Local store is current** (latest game date = today; today's 40 rows are
  the morning-of taper, not an error — the script's own denominator caveat).
  This matches the 14:43–14:48 build run (`bt-202627e.log`: "lineups -> 5004
  row(s)", "feeds -> 2278 row(s) ingested", "derived features computed").
- **Projected snapshots landed for today in both envs** (~549 rows with 87
  D-unit rows and 118 PP1 rows; UNKNOWN still exceeds PROJECTED, so a later
  16:00-ET pass has room to fill blanks — no cron has been installed for the
  scrapes (no `snapshot_projected_cron.log` exists), so today's rows came
  from a manual run, exact pass unknown).
- **The published training tables are the gap**: dev's latest published date
  is 2026-06-14 (the 617,310-row season backfill through the end of the
  2025-26 season per PR #138's verification basis), and prod's backtrack
  table is empty. The `note:` line is the runbook's own pointer: "publish is
  a separate step; run make backtrack-publish after the build".

## Verification trace (2026-10-24)

- `git status` @ feat/feed-fidelity c82b2e7: `daily_sanity.py` untracked.
- `data/bg/bt-202627e.json`: command
  `[.venv/bin/python …/local_store.py, --build, 20262027]`, `cmd: build`,
  `env: dev`, started `2026-10-24T14:43:04`; `ps -p 3830155` → exited.
- `data/bg/bt-202627e.log`: 15 lines, quoted finishes above; **no publish/
  upsert line — `--cmd build` never touches Supabase** (run_background.py:
  91-94 launches only `local_store.py --build`; publish is `--cmd publish`,
  :100-103, or `make backtrack-publish`).
- No `data/bg/publish-*` file newer than 2026-10-18 (`stat` mtimes), and
  `Player-Snapshots-backtrack-dev` latest date 2026-06-14 → **no daily publish
  ran today**; `Player-Snapshots-backtrack-prod` has 0 rows, ever.
- Makefile:99-107 (`backtrack-publish` expansion quoted), config.py (`ENV`
  default chain), local_store.py:1955-1957 (table naming), PROJECTED_LINEUP_
  RUNBOOK.md (10:30/13:00/16:00 ET), git ls-tree of
  `feat/supabase-shot-cols` (migration file exists on that branch only),
  `gh pr view 138` (16-column spec + acceptance semantics).

Known-limits note for this file: today's transient Supabase gateway 4xx pair
is recorded above; nothing in the pipeline logic was diagnosed as faulty, and
the sibling backfill finished its own verification — its hands-off status is
the owner's call, outside this runbook's scope.
