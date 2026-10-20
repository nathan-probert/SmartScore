# Pre-2017 backtrack ingestion runbook (2013-14 .. 2016-17)

Purpose: add four historical seasons to the raw store and backtrack tables so
they can be used as **pointwise training rows**. They have no Tims challenge
groups (challenge starts 2021-12-09), so they must never enter pick evaluation.

Tested entry points (flags from `--help` on 2026-10-20):

- `local_store.py --build/--derive/--lineups/--feeds/--publish/
  --publish-team/--publish-goalies SEASON`
- `dump_store.py --export --season SEASON`
- `reconstruct.py --season SEASON --write` (alternative player-snapshot path)

A single 2013-14 API probe succeeded: game log (78 games), `schedule/2013-10-01`
(7-day window), and shiftcharts for game `2013021223` (730 rows). No long scrape
was run for this runbook.

## 1. Ingest commands (copy-paste, one season at a time)

Run from the repo root. `config.py` loads `.env`; ensure
`SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` are set and `ENV=dev` (the backtrack
publish target).

```bash
cd /home/nathan/code/SmartScore-backtrack
export ENV=dev

for SEASON in 20132014 20142015 20152016 20162017; do
  uv run python smartscore/scripts/backtrack/local_store.py --build "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --derive "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --lineups "$SEASON"
  # --feeds re-ingests tims_daily/goalies_daily raw payloads; pre-2017 has no
  # Tims payload, so this is a no-op. Keep it for pipeline parity or skip it.
  uv run python smartscore/scripts/backtrack/local_store.py --feeds "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --publish "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --publish-team "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --publish-goalies "$SEASON"
  uv run python smartscore/scripts/backtrack/dump_store.py --export --season "$SEASON"
done
```

If `--feeds` errors on missing tims payloads, skip it; it cannot add challenge
groups for these seasons.

`reconstruct.py --season <SEASON> --write` is an alternative for the player
snapshot table only; the `local_store` sequence above also builds team/goalie
tables and the raw store, so prefer it.

## 2. Expected size / runtime / outputs

Per season (current archive: ~55k player-game rows, ~1,350-1,400 games):

| item | estimate |
|---|---|
| API requests | ~3-4k (schedule + boxscores + game logs + shiftcharts) |
| cache | ~0.18 GB (`data/cache/`) |
| raw SQLite growth | ~0.18 GB (`data/raw_nhl.sqlite`) |
| dump | ~13 MB (`data/dumps/<season>.sql.gz`) |
| wall time | **30-45 min/season**, **2-3 h for all four** at the 0.34s throttle + parsing |

Four seasons add roughly **220k player-game rows (+52%** over 427,240); the raw
DB grows from ~1.6 GB to ~2.3 GB and the cache from ~1.6 GB to ~2.3 GB.

Outputs:
- `data/cache/` — API payloads
- `data/raw_nhl.sqlite` — `player_games`, `shifts`, `derived_features`,
  `derived_team_stats`, `goalie_features`, `player_lineups`, raw payload tables
- Supabase `Player-Snapshots-backtrack-dev`, `Team-Stats-backtrack-dev`,
  `Goalie-Snapshots-backtrack-dev`
- `data/dumps/20132014.sql.gz` etc.

## 3. Verification after each season

```bash
sqlite3 data/raw_nhl.sqlite <<'SQL'
-- row counts, date range, label sanity
SELECT season, COUNT(*) AS player_games, MIN(game_date), MAX(game_date),
       SUM(scored) AS goals
FROM player_games WHERE season='20132014' GROUP BY season;

SELECT COUNT(*) AS bad_labels
FROM player_games
WHERE season='20132014' AND (scored IS NULL OR scored NOT IN (0,1));

-- derived feature coverage and null rates
SELECT COUNT(*) AS derived_rows,
       AVG(gpg IS NULL) AS null_gpg,
       AVG(five_gpg IS NULL) AS null_five_gpg,
       AVG(ppg IS NULL) AS null_ppg,
       AVG(hgpg IS NULL) AS null_hgpg
FROM derived_features WHERE season='20132014';

-- join integrity: no orphan derived rows; lineups join player_games
SELECT COUNT(*) AS orphan_derived
FROM derived_features f
LEFT JOIN player_games g
  ON f.season=g.season AND f.player_id=g.player_id AND f.game_date=g.game_date
WHERE f.season='20132014' AND g.player_id IS NULL;

SELECT COUNT(*) AS lineup_rows
FROM player_lineups pl
JOIN player_games pg ON pl.game_id=pg.game_id AND pl.player_id=pg.player_id
WHERE pg.season='20132014';
SQL
```

Expect ~55k `player_games`, dates from Oct 2013 to Jun 2014, ~1,350-1,400 games,
`bad_labels=0`, derived rows equal to `player_games`, no orphan joins, and
lineup coverage near the other seasons. `hgpg` nulls are expected in 2013-14 and
2014-15 because the historic window has no prior seasons.

Quick summary: `uv run python smartscore/scripts/backtrack/local_store.py --stats`
and `uv run python smartscore/scripts/backtrack/dump_store.py --list`.

## 4. Model data load and re-evaluation

After all seasons are published, refresh the model CSVs once:

```bash
cd /home/nathan/code/SmartScore-backtrack
OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
  smartscore/scripts/backtrack/model.py --env dev --refresh \
  --model ensemble --seed 0 --split kfold --splits 5 --train-subset all
```

`--refresh` re-downloads `Player-Snapshots-backtrack-dev`,
`Team-Stats-backtrack-dev`, `Goalie-Snapshots-backtrack-dev` into
`data/ml/*.csv`; subsequent runs use the cache. `season` arrives as TEXT
(`20132014`); the model casts CSV round-trips through `Int64` to string before
merging.

The corrected headline command (repeat for seeds 1 and 3):

```bash
for s in 0 1 3; do
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/model.py --env dev --model ensemble \
    --seed "$s" --split kfold --splits 5 --train-subset all
done
```

**Pre-Tims exclusion check** — before evaluating, confirm the new rows have no
challenge groups:

```bash
uv run --no-sync python - <<'PY'
import pandas as pd
p = pd.read_csv("data/ml/player_snapshots_backtrack_dev.csv",
                usecols=["date", "season", "tims"])
old = p[p.season.astype(str).str[:4].astype(int) < 2021]
print("pre-2021 rows with tims group:", int(old.tims.isin([1, 2, 3]).sum()))
PY
```

Must print **0**. New rows stay `tims=0`; `run_folds` builds pick folds only from
`tims in {1,2,3}` dates, so they never become test picks. With
`--train-subset all` they are included in pointwise training; with
`--train-subset challenge` they are excluded. After the run, the coverage line
`rows in tims groups` must still be **92,440**.
