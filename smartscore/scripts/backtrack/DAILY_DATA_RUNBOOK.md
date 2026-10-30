# DAILY_DATA_RUNBOOK — the daily day-of data capture, in operational order

Owner decision needed: the schedule in §6 is **proposed, not installed**
(this runbook documents it; installing is the owner's call — see §6).

Purpose: make the incremental prior-day crawl → derived features → Supabase
publish chain a monitored, first-class daily routine so the deployed ML Lambda
always trains on complete prior-day data. No new modeling.

Current season constant: **20262027** (bump `SEASON` at each season boundary;
`nhl_client._season_from_date` derives it purely from the month, ≥ July belongs
to the *new* season id).

Last end-to-end verification: **2026-10-24** (evidence inline below).

---

## 1. The steps in order (dependency chain)

```
schema migration (one-time, per spec PR #138 / migration PR from r-migration)
        │  unblocks: publish sending the 16 new shot/TOI columns
        ▼
(A) incremental crawl  ──────────────►  data/raw_nhl.sqlite  (local store)
        │        run_background.py --cmd build --season $SEASON
        │        (= box scores + shift charts on cache-miss, game-log merge,
        │         lineup reconstruction, tims/goalie feeds, then derived
        │         features + derived team features — see below)
        ▼
(B) (already inside build: derive | lineups | feeds)  — no separate daily step
        ▼
(C) publish ─────────────────────────►  Player-/Team-/Goalie-Snapshots-backtrack-{dev|prod}
        │        make backtrack-publish SEASON=$SEASON ENV=prod
        │        (upserts; reads the local store at run time → picks up every
        │         row the latest build wrote, no manual flag, idempotent)
        ▼
(D) AWS Lambda trains/predicts off Player-Snapshots-backtrack-prod
        (per-game EventBridge rules via schedule_run() —
        smartscore/utility.py + fixed runs in templates/template.yaml)

independent side-chains (not in the (A)→(D) line):
(P) projected-lineup collector  10:30 / 13:00 / 16:00 ET daily
        snapshot_projected.py --env prod   (PROJECTED_LINEUP_RUNBOOK.md)
        its PROJECTED_PROXY tier reads local ACTUALs, so it benefits from a
        nightly (A), but it never blocks on it.
(F) nightly feed-fidelity tracker  09:45 ET (FEED_FIDELITY_RUNBOOK.md)
        depends on: (A) having the prior day's ACTUAL shift-chart units,
                    and (P) prod snapshots existing for the date.
(S) daily sanity check (read-only): daily_sanity.py — at any time, anytime
        after (A) is expected to have caught up.
```

## 2. CRITICAL incremental gotcha — the season schedule cache

`nhl_client.season_games()` keeps the season's discovered game list in
`data/cache/schedule-<season>.json` and **returns that file verbatim while it
exists** — it never re-walks a warm schedule (the file is rewritten at the end
of a full walk). The per-game box scores and shift charts ARE incremental
(cache-miss only, warm cache ≈ free), but the game list itself is not.

Consequence observed on 2026-10-24: a bare re-run of
`run_background.py --cmd build --season 20262027` re-merged everything for
games it already knew (6,600 player-games, latest `2026-10-22`) and
**added zero rows** while reporting a normal "derived features computed" —
a silently stale success. Deleting the schedule cache first re-walks the
schedule (a few dozen light requests), discovers the newly FINAL games, and
the per-game caches then serve everything already known:

| run on 2026-10-24 (ET) | schedule cache? | runtime | rows | latest game date |
|---|---|---|---|---|
| `bt-202627d` 14:30 re-run | kept (stale, Oct 23) | 8.2 min | 6,600 (+0) | 2026-10-22 |
| `bt-202627e` 14:43 re-run, schedule cache deleted first | re-walked | 7.5 min | 6,720 (+120) | **2026-10-24** |

The second run picked up 2 FINAL games on 2026-10-23 (80 player-games) plus
2 matinees already final on 2026-10-24 (40), 3 new shift-chart payloads
(1,866 shift rows), tims_daily 1,879→1,920 rows (latest `2026-10-23`),
lineups 4,896→5,004. New games after the walk (evening puck-drops there) are
picked up by the NEXT day's refresh — that is the day-of/overnight cadence the
schedule below encodes.

**So the daily step is: delete the schedule cache, then build.** That is the
only machine-level "incremental refresh" trigger; there is no `--refresh`
flag. (Deleting the file is safe: it is a rebuilt-from-scratch cache, and the
walk rewrites it.)

## 3. What `--build` already covers, and what must stay separate

`local_store.py --build <SEASON>` (main() at local_store.py~2179) runs, in
order: `build()` [box scores, shift charts, `merge_gamelogs`, `compute_lineups`,
challenge feeds] then `compute_derived` + `compute_derived_team`. So step (B)
of the prompt's ordering is *inside* step (A) — no separate `--derive` /
`--lineups` / `--feeds` call on the daily path. The pre-2017 runbook's
explicit `--derive/--lineups/--feeds` lines are for per-piecemail rebuilds of
older seasons, not the daily flow.

`--publish` is **not** part of it. The publish chain is
`make backtrack-publish SEASON=... ENV=...` (Makefile~102), which re-runs
`--derive` (seconds, keeps shipped SQL authoritative) and then the three upserts:

- `local_store.py --publish $SEASON` → `Player-Snapshots-backtrack-{ENV}`
  (one row per (date, player_id); skaters only — goalie rows go elsewhere)
- `local_store.py --publish-team $SEASON` → `Team-Stats-backtrack-{ENV}`
  (one row per (season, team_abbrev, game_id))
- `local_store.py --publish-goalies $SEASON` → `Goalie-Snapshots-backtrack-{ENV}`

Does publish pick the new rows up automatically? **Yes, once it runs.** It
SELECTs out of the local SQLite store at run time (join player_games <
derived_features < player_lineups / tims_daily / box scores); it has no
notion of "what was there last time". There is no manual refresh step
between build and publish. It upserts with `on_conflict=date,player_id`
(team: `(season, team_abbrev, game_id)`; goalies: `(season, player_id, date)`)
so re-runs are safe. Publish sends *only* columns the table declares — which
is exactly why the 16-column migration must land **before** the daily publish
is expected to carry those columns (PostgREST would reject whole batches on
unknown columns, so the code drops undeclared ones silently).

Note the honest current state (2026-10-24 read-only check):
`Player-Snapshots-backtrack-prod` is **empty** (0 rows) and
`Player-Snapshots-backtrack-dev` tops out at `2026-06-14`. Before any prod
publish image channel is expected to be live, everyone must remember the prod
publish chain simply hasn't run yet — not a crawl fault.

## 4. Order sensitivities — "what breaks if you swap two steps"

| swap | consequence |
|---|---|
| build (A) after publish (C) | publish ships yesterday's store → the Lambda trains on T−2 data until morning, and tomorrow's publish overwrites nothing (idempotent upserts) but the training rows for the *skipped* date only appear after the next publish. No error anywhere: it looks healthy. |
| build without deleting the schedule cache | "successful" no-op through the last re-walked date (see §2). Silent — this is the one that eats you. |
| publish-columns (16 cols) before the migration PR lands | publish silently omits the columns (it self-truncates to declared columns), so prod rows land column-less and a later featured Lambda silently trains without them. |
| derive (in build) rolled back in front of crawl | derived_features recomputed on the OLD player_games while new rows arrive ⇒ today's features published tomorrow at the same rate; worst case a half-written derive with the crawder writing concurrently (SQLite serialises writers, so you get a queue, not corruption — but ordering confusion is how stale rows get published). |
| snapshot_projected (P) before the nightly crawl | PROJECTED_PROXY falls back to an older_actuals date — projections degrade qualitatively but never write ACTUAL labels; safe-but-stale, self-heals at the next slot. |
| fidelity tracker (F) before (A) catches the prior day | it skips (exit 0, one log line, no metrics): actuals missing ⇒ no data for that night. Re-run manually after the crawl catches up; nothing to cleanse. |
| `--ingest-cache` style mass re-ingests during a live crawl | two writers on the same tables: SQLite blocks, run_background tasks pile up, and the log stops advancing. Never run two builds concurrently; kill one first (`--name <task> --stop`). |

## 5. Proposed daily schedule (owner to install)

Times ET; host runs Debian cron (no cronie ⇒ no `CRON_TZ`, so express in
host-local time as noted, or use the systemd timer where America/Toronto is
expressed in the unit itself).

```cron
# ── daily data capture: refresh to the prior day's finals, then publish ─────
# 1) 03:30 ET: re-walk season schedule + incremental crawl/derive/lineups/feeds
#    (late-night games finish after midnight; 03:30 clears their shift charts)
30 3 * * *  rm -f /home/nathan/code/SmartScore-backtrack/data/cache/schedule-20262027.json && cd /home/nathan/code/SmartScore-backtrack && .venv/bin/python smartscore/scripts/backtrack/run_background.py --name bt-202627d --cmd build --season 20262027 >> /home/nathan/code/SmartScore-backtrack/data/bg/daily-build-cron.log 2>&1
# 2) 04:20 ET: publish everything the store now holds (derive included), @prod
20 4 * * *  cd /home/nathan/code/SmartScore-backtrack && ENV=prod .venv/bin/python smartscore/scripts/backtrack/local_store.py --publish 20262027 && ENV=prod .venv/bin/python smartscore/scripts/backtrack/local_store.py --publish-team 20262027 && ENV=prod .venv/bin/python smartscore/scripts/backtrack/local_store.py --publish-goalies 20262027 >> /home/nathan/code/SmartScore-backtrack/data/bg/daily-publish-cron.log 2>&1
# 3) 04:40 ET (after the publish): sanity so the morning starts with evidence,
#    not hope; add the dev tables too if you want both turtles raced
40 4 * * *  cd /home/nathan/code/SmartScore-backtrack && .venv/bin/python smartscore/scripts/backtrack/daily_sanity.py --env prod >> /home/nathan/code/SmartScore-backtrack/data/bg/daily-sanity-cron.log 2>&1
# 4) 09:45 / 09:55 ET: nightly fidelity vs yesterday's actuals (already
#    designed in FEED_FIDELITY_RUNBOOK.md §Nightly schedule - keep those lines)
```

Why these times: run (1) needs the night's shift charts to be fetchable (late
games finish after midnight ET; 03:30 clears them); run (2) needs run (1)'s
output; run (3) needs run (2); the 10:30/13:00/16:00 projection collector needs
*nothing* on this list (independent chain P), and the 09:45 fidelity tracker
needs (1) and (P) — so it sits behind both in the day.

### systemd timers (preferred on systemd hosts)

`/etc/systemd/system/daily-data-build.service`:

```ini
[Unit]
Description=Daily incremental season build (crawl + derive + lineups + feeds)

[Service]
Type=oneshot
WorkingDirectory=/home/nathan/code/SmartScore-backtrack
Environment=PATH=/home/nathan/.local/bin:/usr/local/bin:/usr/bin:/bin
# delete the season schedule cache so season_games() re-walks and discovers
# newly FINAL games (see §2 - without this the build is a silent no-op)
ExecStart=/bin/sh -c 'rm -f data/cache/schedule-20262027.json && .venv/bin/python smartscore/scripts/backtrack/run_background.py --name bt-202627d --cmd build --season 20262027'
```

`/etc/systemd/system/daily-data-build.timer`:

```ini
[Unit]
Description=Daily incremental build at 03:30 America/Toronto

[Timer]
OnCalendar=*-*-* 03:30 America/Toronto
Persistent=true

[Install]
WantedBy=timers.target
```

`daily-data-publish.service` / `.timer` mirror the build pair with
`OnCalendar=*-*-* 04:20 America/Toronto` and
`ExecStart=/home/nathan/code/SmartScore-backtrack/.venv/bin/python
smartscore/scripts/backtrack/local_store.py --publish 20262027`, plus
`--publish-team` and `--publish-goalies` lines (all with `Environment=ENV=prod`),
and the same coupling: a `Publish` unit `After=` + `Requires=` the build unit's
`daily-data-build.service`.

## 6. Owner decision needed

- ☐ Install the §5 cron or systemd timer (nothing is installed today:
  `crontab -l` is empty, no user/system units relate to SmartScore, and
  `.github/workflows/` has only deploy.yml triggered on push/PR — no `schedule:`
  key anywhere in the repo). The AWS-side schedules (template.yaml
  `cron(0 8/14 * * ? *)` + per-game EventBridge rules from `schedule_run`)
  are the deployed pipeline's own triggers, unrelated to this local crawl.
- ☐ Choose tool: plain cron (no CRON_TZ available on this Debian) vs the
  systemd timer (explicit timezone, Persistent=true hence catch-up races).
- ☐ Season bump: change every `20262027` literal when the season turns (4+1
  plus the §2 schedule-cache filename).
- ☐ Whether the daily publish to **prod** runs from this host before or only
  after the 16-column migration PR is merged (§3's note).

## 7. Health / monitoring checklist (copy-paste)

Local store freshness and per-day counts — or just run the script:

```bash
cd /home/nathan/code/SmartScore-backtrack
.venv/bin/python smartscore/scripts/backtrack/local_store.py --stats
.venv/bin/python smartscore/scripts/backtrack/daily_sanity.py --env prod   # § example output below

sqlite3 via python (no sqlite3 CLI on this host):
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('file:data/raw_nhl.sqlite?mode=ro', uri=True); \
 print(c.execute(\"SELECT game_date, count(*) FROM player_games WHERE season='20262027' GROUP BY game_date ORDER BY game_date DESC LIMIT 7\").fetchall())"

# latest build state / alive-pid / log tail:
.venv/bin/python smartscore/scripts/backtrack/run_background.py --name bt-202627d --status
.venv/bin/python smartscore/scripts/backtrack/run_background.py --name bt-202627d --tail 40
tail -50 data/bg/daily-build-cron.log          # installed-cron tail (once installed)

# log presence per expectation (for batch evidence keeping):
ls -lat data/bg/*.log | head            # last touched build logs
stat -c '%y %n' data/bg/bt-202627d.log data/cache/schedule-20262027.json data/raw_nhl.sqlite | sed 's/\.[0-9]* / /'
```

Supabase projected-table quick check (GET-only):

```bash
cd /home/nathan/code/SmartScore-backtrack
.venv/bin/python - <<'EOF'
import os, time
from collections import Counter
from dotenv import load_dotenv
import requests
load_dotenv('.env')
key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
h = {'apikey': key, 'Authorization': 'Bearer ' + key}
date = time.strftime('%Y-%m-%d')
r = requests.get(os.environ['SUPABASE_URL'] + '/rest/v1/Player-Snapshots-prod',
                 headers=h, params={'select': 'lineup_status', 'date': f'eq.{date}'})
print(date, Counter(x['lineup_status'] for x in r.json()))
EOF
```

(For the full breakdown — by_status rows, D-unit rows, PP1 rows, and both
published tables' latest dates — just use `daily_sanity.py`, which runs the
same pattern plus the local-store checks.)

### Real example output — `daily_sanity.py --env both`, 2026-10-24 14:55 ET

```text
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

Reads: the 10:30/13:00 collectors ran (548 PROJECTED PROD rows are present for
today, waiting for the third slot to fill the UNKNOWN bucket); the local store
leads the published dev table by every current-season game (publish pending —
see §3 note); D/PP1 shares look normal for a morning slot.

## 8. Recovery cheatsheet

- A task claims to be running forever → `--name <task> --status` then `--stop`:
  it kills the pid recorded in data/bg/<name>.json and delays another start.
- A build died mid-crawl → re-run the same §5/§2 command; per-game caches make
  replays free; pressed `--gamelogs`/`--lineups`/`--feeds` handle piecewise.
- Suspicious feeds 0 rows → check the 403 with Origin headers in
  `nhl_client._get_json`, try `--feeds` alone for the same season.
- Duplicate name → run_background refuses a duplicate alive pid; use another
  `--name` for a manual run during testing.
