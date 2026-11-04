# PROJECTED_LINEUP_RUNBOOK - pre-game projected lineups with units

Owner: `smartscore/scripts/backtrack/snapshot_projected.py`
Branch: `backtrack-recon`. **Working directory is always
`/home/nathan/code/SmartScore-backtrack`** - not `/home/nathan/code/SmartScore`
(the script lives only in the backtrack repo, and an earlier runbook pointed
at the wrong checkout, which is why nothing ever landed in prod).

## What it persists

One row per `(date, player_id)` into `Player-Snapshots-{dev|prod}` with the
heading columns only (date, player_id, name, team_name, home,
lineup_unit, lineup_position_group, pp_unit, lineup_status). It never sends
stats columns (`_assert_append_safe` refuses any payload carrying one).

Two tiers of unit signal, flagged through `lineup_status`:

* `PROJECTED` - every unit value on the row is from a published pre-game
  source: the NHL.com lineup-projections article (F1-F4 / D1-D3) and, when
  the dated article confirms the slate, RotoWire's PP1/PP2. If the article's
  JSON-LD `datePublished` is not the game date, the article's units are
  refused (it sometimes still carries the previous slate - e.g. at 11:30 ET
  on 2026-10-22 it did) and only the RotoWire slate is collected; every
  matchup is date-anchored by matching onto that day's NHL schedule.
* `PROJECTED_PROXY` - the row contains at least one value that is a
  *prior-game proxy*: the player's strongest strictly-prior deployment from
  the snapshot archive (earlier date rows only) or the local shift-chart
  reconstructions (`data/raw_nhl.sqlite` `player_lineups`, ACTUAL units,
  complete through the last crawled season, incl. D-pairs). A stored row
  already labelled `PROJECTED` never receives proxy values, so proxy
  fidelity can never launder into the official label. `PROJECTED_PROXY`
  rows may contain official values (e.g. RotoWire PP) alongside the proxy
  ones - the label errs toward caution; read it as "at least one value is
  proxy". Proxy PP labels can be PP1/PP2/PP3/PP4 (reconstruction ranks
  beyond two units are real deployment, kept verbatim under the proxy flag).

Write paths (both idempotent over the `(date, player_id)` primary key):

* **append** - `upsert` with `ignore_duplicates=True`: insert-only by DB
  semantics; already-stored rows are never touched.
* **refresh** - fills only *blank* lineup fields on stored rows
  (UNKNOWN-stamp source failures, bare rows), normalises verbatim
  "POWER PLAY #N" labels to PP1/PP2 when a row is patched anyway, and never
  overwrites a non-null stored value. Guards:
  - `CONFIRMED`/`ACTUAL` rows are excluded in the group build *and* by a
    PostgREST `not.in` filter inside the UPDATE itself;
  - no stats column is ever in a refresh payload;
  - pre-game timestamp guard: rows whose scheduled game start has passed,
    and rows whose team cannot be placed on the date's schedule, are
    skipped (a late run never touches a game already underway).

## Why three times a day

The article's F/D units publish through the morning and the slate coverage
varies, so each run fills rows still blank. Because refresh only fills
blanks (and never rewrites non-null stored values or the two keep-statuses),
re-runs are strictly additive, cheap, and idempotent - the third 16:00 run
is the last safe write before most puck drops. This corrects the earlier
"keeps PP units fresh" rationale: nothing expires; later runs only add what
earlier runs could not collect.

## Schedule (all three are ET)

10:30, 13:00, 16:00 America/Toronto, every day of the season.

## uv environment

If the venv has to be rebuilt (it is committed-prebuilt at
`.venv/bin/python` for python3.12), ensure `uv` is on PATH first - it hats
at `$HOME/.local/bin` and background/nohup runs have been observed failing
without it:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Cron

```
# projected lineups, three passes across the league's morning slate
CRON_TZ=America/Toronto
30 10 * * *  cd /home/nathan/code/SmartScore-backtrack && /home/nathan/code/SmartScore-backtrack/.venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env prod >> /home/nathan/code/SmartScore-backtrack/smartscore/scripts/backtrack/snapshot_projected_cron.log 2>&1
0  13 * * *  cd /home/nathan/code/SmartScore-backtrack && /home/nathan/code/SmartScore-backtrack/.venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env prod >> /home/nathan/code/SmartScore-backtrack/smartscore/scripts/backtrack/snapshot_projected_cron.log 2>&1
0  16 * * *  cd /home/nathan/code/SmartScore-backtrack && /home/nathan/code/SmartScore-backtrack/.venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env prod >> /home/nathan/code/SmartScore-backtrack/smartscore/scripts/backtrack/snapshot_projected_cron.log 2>&1
```

`--env prod` must be explicit; add a second block with `--env dev` while
iterating. `CRON_TZ` needs cronie (systemd hosts do it differently, below);
on Debian's plain Vixie cron run the times with the host timezone instead
or wire the timer through systemd, where the timezone is explicit.

## systemd timers (preferred on systemd hosts)

`/etc/systemd/system/snapshot-projected.service`:

```ini
[Unit]
Description=Snapshot pre-game projected lineups

[Service]
Type=oneshot
WorkingDirectory=/home/nathan/code/SmartScore-backtrack
Environment=PATH=/home/nathan/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/nathan/code/SmartScore-backtrack/.venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env prod
```

`/etc/systemd/system/snapshot-projected.timer`:

```ini
[Unit]
Description=Projected lineups at 10:30 / 13:00 / 16:00 ET

[Timer]
# Oct-Jun hockey season ET times
OnCalendar=*-*-* 10:30,13:00,16:00 America/Toronto
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now snapshot-projected.timer
systemctl list-timers snapshot-projected.timer
```

## Verification query (read-only)

```bash
cd /home/nathan/code/SmartScore-backtrack && .venv/bin/python - <<'EOF'
import os, sys
sys.path.insert(0, "smartscore")
from collections import Counter
os.environ["ENV"] = "prod"
from config import SUPABASE_ADMIN_CLIENT
r = SUPABASE_ADMIN_CLIENT.table("Player-Snapshots-prod").select(
    "date,lineup_status,lineup_unit,lineup_position_group,pp_unit").gte("date", "2026-10-20").execute()
rows = r.data or []
print("statuses:", Counter(x["lineup_status"] for x in rows))
print("D units:", sum(1 for x in rows if (x.get("lineup_unit") or "").startswith("D")))
print("PP1:", sum(1 for x in rows if x.get("pp_unit") == "PP1"))
EOF
```

Healthy run shows, for the current date: `UNKNOWN` count shrinking toward
players with no prior record, `PROJECTED_PROXY`/`PROJECTED` rows with
non-null units, `CONFIRMED`/`ACTUAL` counts untouched by this collector.

Ad-hoc pre-check without writing:

```bash
cd /home/nathan/code/SmartScore-backtrack && .venv/bin/python smartscore/scripts/backtrack/snapshot_projected.py --env prod --dry-run
```

(`--dry-run` performs no table reads and no writes; a real run re-reads the
date's stored rows itself, so re-running is always safe.)

## Known limits (evidence, not speculation)

* The NHL.com article does not cover every game on a busy slate (on
  2026-10-22 it carried 2 games of 12 when fresh) and sometimes lags by a
  day; on such days F/D units come only from the proxy tier and rows are
  labelled `PROJECTED_PROXY`.
* RotoWire publishes PP units only (~20 skaters of a ~40-skater slate) and
  no forward/defence lines.
* No official NHL endpoint publishes full pre-game line units; the
  official lookups here (`schedule`, `roster/{abbrev}/current`) exist for
  date anchoring and player-id joining only.
* Proxy lineups stay at last season's final deployments until the current
  season's shift-chart crawl is rebuilt in `data/raw_nhl.sqlite`; label
  `PROJECTED_PROXY` is the contract that this is approximate.
* Runs never fabricate: what the article, RotoWire and the dated proxy
  history cannot support stays blank (or UNKNOWN), which is why an early
  run may add nothing and why later runs exist.
