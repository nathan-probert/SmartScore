# Feed-Fidelity Runbook — nightly lineup-feed fidelity vs backtracking actuals

Owner: `smartscore/scripts/backtrack/fidelity_tracker.py`
Context: `MODEL_NOTES.md` → FINAL RESULT + `V16- collect` sections, and the V11
sensitivity table (81% fidelity ≈ +0.1–0.3pp, 100% ≈ +1.2pp).

## What it does

Each run takes one game date and answers a single question: **how close is the
daily build's projected lineup to what actually deployed?**

- **Projected side (Supabase, read-only):** `Player-Snapshots-prod` (with
  `--env prod`) or `Player-Snapshots-backtrack-dev` (with `--env dev`); rows
  with `lineup_status` in {`PROJECTED`, `PROJECTED_PROXY`}. `--env both` runs
  both tables. NO Supabase write is ever issued — this module only SELECTs;
  prod is reached with the service-role key for read reach, nothing more.
- **Actual side (local store, read-only):** `player_lineups` in
  `data/raw_nhl.sqlite` (shift-derived ACTUAL lines via
  `local_store.compute_lineups` / `lineup_reconstruct.reconstruct_game`),
  joined to `player_games` on `(game_id, player_id)` for the game date,
  team abbrev, and home flag.
- **Join:** on `(date, player_id)`. The daily build's `team_name` is a place
  name, and "New York" is NYR/NYI-ambiguous, so identity is never taken from a
  place-name string join: the (place, home/away split of the actuals)
  resolution is used only to disambiguate, and a still-ambiguous row is
  counted (`n_team_ambiguous`) and EXCLUDED, never guessed.
- **Metrics per compared pair:**
  - `unit_match` — exact `lineup_unit` equality (F1–F4 / D1–D3).
  - `pp_match` — exact `pp_unit` equality after PP normalization
    ("POWER PLAY #1" → PP1; PP3/PP4 stay themselves).
  - `presence_match` — the projected row says the player sits in a D/F slot
    that also exists in the actual deployment (group equality).
- **Per-snapshot fidelity:** the collector runs 10:30 / 13:00 / 16:00 ET, but
  the daily-build tables key on `(date, player_id)` with no snapshot column,
  so the stored row is the accumulated view of all three runs. The tracker
  detects a slot column generically (`source_slot`/`snapshot_slot`/`slot`, or
  `*,_at`/`snapshot_time` bucketed in ET) and writes one metric row per
  source-slot; today that resolves to the single slot `single`, and the
  run-level number is carried under slot `all`. **When a per-which-slot
  column is added to the daily build, per-snapshot fidelity starts flowing
  with no code change** — and per-snapshot is the number that would say WHICH
  collection slot to trust.

## How to run

```bash
cd /home/nathan/code/SmartScore-backtrack
.venv/bin/python smartscore/scripts/backtrack/fidelity_tracker.py --env prod --date 2026-10-22
.venv/bin/python smartscore/scripts/backtrack/fidelity_tracker.py --env both          # prod + dev
```

Skip behaviour (exit 0, no log rows, clear message): the date's ACTUAL rows
are not in the local store yet (the shift crawler backfills the current season
concurrently → run nightly *after* the crawl), no projected rows exist for the
date, or the local store file is missing.

## Nightly schedule (cron)

After the 16:00 ET collection slot and once shift charts for the prior day
have had their chance to land (games that night finish late ET; the crawler
ingests them overnight):

```cron
# nightly feed-fidelity tracking of YESTERDAY's slate, prod first then dev
CRON_TZ=America/Toronto
45 9 * * *  cd /home/nathan/code/SmartScore-backtrack && PATH=$HOME/.local/bin:$PATH .venv/bin/python smartscore/scripts/backtrack/fidelity_tracker.py --env prod --date "$(TZ=America/Toronto date -d yesterday +%F)" >> /home/nathan/code/SmartScore-backtrack/smartscore/scripts/backtrack/fidelity_tracker_cron.log 2>&1
55 9 * * *  cd /home/nathan/code/SmartScore-backtrack && PATH=$HOME/.local/bin:$PATH .venv/bin/python smartscore/scripts/backtrack/fidelity_tracker.py --env dev  --date "$(TZ=America/Toronto date -d yesterday +%F)" >> /home/nathan/code/SmartScore-backtrack/smartscore/scripts/backtrack/fidelity_tracker_cron.log 2>&1
```

Absolute paths and `PATH=$HOME/.local/bin:$PATH` are load-bearing: cron's
default PATH has no `uv`, and the module runs `uv run`-free because the
project venv is invoked directly. `--date` defaults to *today* ET; the cron
line passes yesterday explicitly so a 09:45 run measures yesterday's slate
after its actuals have accumulated.

Skips log nothing, so a message-only cron log on a non-game day is normal.

## Where the log lands

`data/ml/fidelity_log.csv` (untracked cache file, like the rest of `data/ml`;
never committed). Schema — one row per (date, source-slot, metric):

| column | meaning |
|---|---|
| `ts` | UTC append timestamp |
| `env` | prod / dev |
| `season` | season id as TEXT ("20262027") — a string everywhere in this module, never an int |
| `scope` | `date` (this run) or `season` (exposure-weighted aggregate recomputed each run) |
| `date` | game date (empty on `season` rows) |
| `source_slot` | collection slot (or `single` / `all`) |
| `metric` | `unit_match` / `pp_match` / `presence_match` |
| `value` | the rate (empty when no comparable pairs) |
| `matches` / `n` | matched bodies and the denominator — reweight with these, never average `value`s |

Read back the trend:

```bash
python3 - <<'EOF'
import csv
rows = [r for r in csv.DictReader(open("data/ml/fidelity_log.csv"))
        if r["scope"] == "date" and r["metric"] == "unit_match"]
rows.sort(key=lambda r: r["ts"])
for r in rows: print(r["date"], r["env"], r["source_slot"], f"{float(r['value'])*100:.1f}%", f"{r['matches']}/{r['n']}")
EOF
```

## Interpreting the numbers — the adoption gate

- **unit_match is the gate metric.** The pre-declared bar: **≥ 90% on a
  comparable-pair denominator, sustained over the validation window
  (~2–3 weeks of dated rows).**
- **presence_match** says the projected player exists in the actual
  deployment at all (scratch/late-scratch capture). A gap between
  presence (high) and unit (low) means the names are right but the slot
  labels are wrong — that is a source problem, not a coverage problem.
- **pp_match** is a secondary read on the PP-unit source (RotoWire) vs the
  shift-chart reconstruction; verbatim "POWER PLAY #N" vs PP1/PP2 noise on
  historical rows is normalized before comparison.
- Use the season-scope rows for the trend, and respect the `n`s: a 3/3 day is
  not 100% fidelity.
- The numbers feed the model decision separately: fidelity ≥ 90% is the
  necessary condition; the *McNemar* half (paired vs the no-lineup ensemble
  on both seeds — see the V16 experiment scripts' McNemar plumbing) is what
  converts FIDELITY measurements to adoption.

### Explicitly: what number → adopt, what → stay at the 29.7% default

**Adopt** only when the logged `unit_match` (season-scope, exposure-weighted)
holds ≥ **90%** across the validation window AND the paired McNemar tests vs
the no-lineup ensemble come back significant winners on **BOTH** seeds
(1 and 3). **Stay at the 29.7% default** (the no-lineup ensemble, 29.71%
pooled OOF) while fidelity sits under 90% — the V11 sensitivity says ~81%
fidelity is worth only +0.1–0.3pp (within noise) and only ~100% fidelity buys
~+1.2pp, so an under-gate feed buys nothing worth a default switch.

## ⚠️ READ-ONLY against prod — explicit caveat

The tracker never issues a Supabase write on ANY environment. Prod reads use
the existing service-role credential for read access; there is no insert,
patch, upsert, or delete anywhere in `fidelity_tracker.py`. The only file it
changes is the untracked `data/ml/fidelity_log.csv`. Nobody has authorized a
prod write through this feature; if a prod write ever becomes necessary it
needs its own approval, its own diff, and `-env prod` explicit gates like
`snapshot_projected.py` already has.

## Known state (first prod check, 2026-10-23)

- `--env prod --date 2026-10-22` (read-only sanity run): the current season's
  shift backfill landed for 2026-10-22 during validation, so the run produced
  real numbers instead of the expected skip. Prod table had 540 rows (525
  unit-carrying per MODEL_NOTES as of 2026-10-22; at run time 276 were
  tracked rows — 275 live-pipeline `PROJECTED` F-only rows from the pre-fix
  archive + 1 `PROJECTED_PROXY` — and 264 `UNKNOWN` were excluded by the
  tracked-status filter). Result: **268 compared pairs, unit_match 164/268 =
  61.2%, pp_match 130/150 = 86.7%, presence_match 267/268 = 99.6%.**
  Read: presence is near-perfect (projected players appear in the actual
  deployment), but exact-unit fidelity sits far under the 90% gate — the
  pre-fix live-pipeline archive's F-only labels do not meet the bar, which is
  exactly what the gate is there to catch. Keep watching the nightly rows as
  the collector's full-vocabulary rows (F + D + PP) accrue and the
  crawler-current actuals cover them; the comparable denominator also grows
  as D/PP labels reach the projected side.
- Earlier in the same session (pre-backfill) the skip path fired verbatim:
  "no ACTUAL lineups in the local store for 2026-10-22 (... shift charts are
  not crawled yet ...)" with exit 0 and no log rows — recorded as the
  designed graceful behaviour. Skip-path rehearsed; first tracked numbers:
  the 61.2% row above.
- Per-snapshot fidelity is pending a slot column on the daily build (see the
  SOURCE SLOTS note above) — the "single"/"all" slots are placeholders until
  then.
