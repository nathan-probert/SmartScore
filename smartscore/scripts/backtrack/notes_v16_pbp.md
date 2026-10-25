# V16-pbp — pass-precedence / possession-context per-shot xG (fetch + eval)

Verdict up front: **rejected.** The possession-context enrichment moves the
per-shot proxy by ~0.001 AUC and the pick metric by −0.06 / +0.28pp (LGBM
seeds 1/3) and +0.51 / −0.76pp (ensemble seeds 1/3) — the same mixed
noise pattern as V7-shots, V9-projected-lineups and V15. The pre-declared
gate (≥ +0.5pp pooled on BOTH seeds 1 and 3, McNemar-consistent) failed, so
no seed 0 was run and the default stays unchanged.

## Stage 1 — PBP fetch (fetch_pbp.py)

- Source: `api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play`, one GET per
  game, `nhl_client._get_json` retry envelope (4 attempts, 30s timeout,
  1/2/4s backoff), game list driven by `data/raw_nhl.sqlite` `player_games`
  (seasons 20212022..202526), progress logged every 100 games.
- Per-game trimmed payloads cached at `data/ml/pbp/pbp-{game_id}.json`
  (atomic tmp+rename); the fetcher also adopts full payloads previously
  cached at `data/cache/pbp-{game_id}.json`. Fully resumable + idempotent:
  re-runs skip cached games and rebuild the CSVs from cache.
- **Games 6,993 / 6,993 cached, 0 fetch failures, 0 parse failures.**
- Tables (per raw-attribute aggregation, no external model numbers):
  - `data/ml/pbp_shots.csv` — **608,813 unblocked attempts** (sog 395,525 /
    miss 131,145 / goals 44,175... see counts below), one row per attempt
    with coordinates, dist/angle, shot type, rebound flag, PP/empty-net,
    and the possession chain: `prior_own_events`, `one_touch_raw`
    (first own event of the chain = one-touch vs carry-in proxy),
    `prior_event_last_dx/dy` (pass/carry origin; cross-ice flag |dy| ≥ 25).
  - `data/ml/pbp_player_game.csv` — **279,659 player-game rows**
    (65,460 zero-attempt roster rows kept, V8-style denominators).
- Coverage: 2021-10-12 .. 2026-06-14 (all five seasons in scope; the raw
  store also carries 2025-26, so the whole challenge era is covered).
- Sanity: 87.1 attempts/game; rebound share 0.0813; one-touch share 0.3675;
  prior-event share 0.6325; cross-ice 0.2575; pp share 0.0953; empty-net
  0.0074. Hand-checked game 2023020100: 90 rows = 55 SOG + 28 misses +
  7 goals, exact.
- Ops gotcha worth keeping: three fan-out fetch processes against the same
  endpoint trip HTTP 429s (which the retry envelope does NOT retry, by
  design); a boundary rule slot a "failure rate > 50% -> stop the pass" breaker.
  One serialized process at delay 0.75 sustains 1.3 games/s with zero failures.
- Runtime: ~33 min per 2,800-game range (serialized), plus ~1 min aggregate.

## Stage 2 — feature + eval (exp_pbp.py, model.py machinery untouched)

**Per-shot xG (first-party).** Per seed, the challenge dates are cut into
five shuffled date blocks (`model.fold_blocks`); every other dated block
(dates carrying shots) gets its own five blocks. For each block a per-shot
LGBMClassifier (400 it, lr 0.05, 31 leaves, min_child 100) trains on all
shots OUTSIDE the block's dates (assertion: date sets disjoint) and predicts
the block's shots; everything is strict out-of-fold, first-party, no
external model or odds input.

| per-shot OOF AUC (608,813 shots, 44,175 goals) | seed 1 | seed 3 |
|---|---|---|
| geo-only (dist/angle/HD/slot/type/rebound/PP/empty net) | 0.7645 ± 0.0044 | 0.7653 ± 0.0117 |
| enriched (+ possession block) | **0.7652 ± 0.0044** | **0.7662 ± 0.0118** |

The pass-precedence block adds **+0.0007 / +0.0009 AUC** — essentially
nothing (the V8 geometry reference of 0.769 used the MoneyPuck raw columns
which include time-since-last-event features we cannot reconstruct).

**Player features.** Per player-game xG sums from the enriched OOF model,
then strictly prior-game rolling means with shift(1) and coverage guards
(≥10/≥20 of 15/30):
`rs_pbxg_pg15/30`, `rs_pbxg_pa15`, `rs_pbprior_share15/30`,
`rs_pbone_touch_share15`, `rs_pbhd_share15`, `rs_pbrebound_share15`,
`rs_pbcross_share15`, `rs_pbslot_share15`, `rs_pbpp_share15` (+ a `_miss`
missing-indicator companion per rolling column; out-of-coverage rows get
0.0 + indicator instead of NaN). 238,393 of 617,310 frame rows carry
genuine rolling values; 92,282 / 92,440 challenge rows are in coverage
(genuine or imputed). Leak assertions: fold date-disjointness plus a
perturbation check (setting a current game's own xG to 99.0 does not move
its rolling features) — passed on both seeds.

**Protocol.** Same k=5 challenge-date k-fold, --train-subset all, LGBM
config a gate + full ensemble arm; exact McNemar (binomtest) on pooled
paired picks. Commands (seeds 1 and 3 identical but for --seed):

```bash
cd SmartScore-backtrack
OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
  smartscore/scripts/backtrack/exp_pbp.py --env dev --seed 1 --with-pbp \
  --oof-file data/ml/oof_v16_pbp_seed1.csv
# base/reference arm: same command WITHOUT --with-pbp
```

**Pooled OOF picks (with-pbp arms):**

| model | seed | hits/3159 | pooled acc | folds mean ± std | ref (per protocol) | Δ |
|---|---|---|---|---|---|---|
| lgbm | 1 | 926 | 29.313% | 29.314 ± 2.351 | 928 = 29.376% | **−0.063pp** |
| lgbm | 3 | 925 | 29.281% | 29.282 ± 0.948 | 916 = 28.997% | **+0.284pp** |
| ensemble | 1 | 945 | 29.915% | 29.914 ± 2.514 | 929 = 29.408% | +0.507pp |
| ensemble | 3 | 918 | 29.060% | 29.060 ± 0.639 | 942 = 29.820% | **−0.760pp** |

(gpg 837 = 26.496% and hgpg 886 = 28.047% reproduce exactly in-run on both
seeds. The archival LGBM refs (928/916) drifted to **931/929** under the
current frame — the V15 pre-2017 rows changed the base slightly — so the
decisive comparison is the matched-data pair below.)

**Matched-data pbp-vs-base (same code, same data, same folds — decisive):**

| model | seed | base hits | pbp hits | Δ | McNemar a-b | p | changed picks |
|---|---|---|---|---|---|---|---|
| lgbm | 1 | 931 = 29.471% | 926 = 29.313% | **−0.158pp** | 150-155 | 0.8189 | 795 |
| lgbm | 3 | 929 = 29.408% | 925 = 29.281% | **−0.127pp** | 136-140 | 0.8567 | 798 |

**Exact McNemar (pooled paired picks, vs gpg/hgpg and archival refs):**

| arm | vs | a-b | p |
|---|---|---|---|
| lgbm+pbp s1 | gpg | 395-306 | 0.0009 |
| lgbm+pbp s1 | hgpg | 313-273 | 0.1071 |
| lgbm+pbp s3 | gpg | 400-312 | 0.0011 |
| lgbm+pbp s3 | hgpg | 329-290 | 0.1266 |
| ensemble+pbp s1 | gpg | 416-308 | 0.0001 |
| ensemble+pbp s1 | hgpg | 324-265 | 0.0168 |
| ensemble+pbp s1 | lgbm (file ref 928) | 197-180 | 0.4099 |
| ensemble+pbp s3 | gpg | 409-328 | 0.0032 |
| ensemble+pbp s3 | hgpg | 310-278 | 0.2011 |
| ensemble+pbp s3 | lgbm (file ref 916) | 194-192 | 0.9594 |

Per-group (with-pbp ensemble, seeds 1/3): s1 g1 39.2 / g2 28.8 / g3 21.7;
s3 g1 38.5 / g2 27.8 / g3 20.9 (baselines in-run g1 38.4 / g2 26.0 / g3 19.8).
Era subsets: s1 2023-24+ 610/2001 = 30.48%, 2025-26 200/633 = 31.60%;
s3 29.34% / 28.59% — with the gpg/hgpg in-run references 27.09/30.33.

## Why rejected

1. The per-shot proxy barely moves: +≤0.001 AUC from the whole possession
   block (det. consistent across seeds/blocks). Pass precedence, one-touch vs
   carry and pass-origin separation carry almost no per-shot goal signal
   beyond geometry on this feed.
2. **Matched-data pairs are negative on BOTH seeds** (−0.16pp p=0.82; −0.13pp
   p=0.86): the earlier mixed signs (−0.06/+0.28) vs the archival references
   were reference drift, not a hint of signal. The gate (≥ +0.5pp on both
   seeds) fails decisively; the ensemble pattern (+0.51/−0.76 vs archival
   refs) matches the same noise band as V7-shots/V9/V15.
3. Consistent with the standing finding: per-shot shot-quality blocks
   (V7-shots, V8-own-xG, V7 MoneyPuck xG) do not move daily picks; the pick
   metric is dominated by volume/rate priors already in the frame.

## Files

- `smartscore/scripts/backtrack/fetch_pbp.py` (fetcher, resumable/idempotent)
- `smartscore/scripts/backtrack/exp_pbp.py` (per-shot xG + rolling features + k-fold eval)
- `data/ml/pbp_shots.csv`, `data/ml/pbp_player_game.csv` (608,813 / 279,659 rows)
- `data/ml/pbp/pbp-{game_id}.json` trimmed per-game cache (6,993 files, not committed — regenerable by re-run)
- `data/ml/oof_v16_pbp_seed{1,3}.csv`, `data/ml/oof_v16_base_seed{1,3}.csv` (OOF tables)
