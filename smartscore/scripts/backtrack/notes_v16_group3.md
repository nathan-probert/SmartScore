# V16-group3 — group-conditional weighting + dedicated g3 model

## Motivation

Baseline per-group pooled accuracy (corrected all-era ensemble, seeds
0/1/3): **g1 39.9% / g2 27.2% / g3 20.9%**. Group 3 (the weakest tier of
each date's slate) is half as accurate as group 1 and drags pooled
accuracy to 29.71%. Two declared variants and nothing else, both on the
SAME k=5 challenge-date k-fold structure as the default (`fold_blocks`
with `--seed`, `--train-subset all`, `model.py` machinery unedited,
3,159 pooled picks per seed):

- **V1 group-weight** (`w2`, `w4`) — group-conditional sample weights:
  every training row weight 1 except `tims==3` rows, weighted W ∈ {2, 4}.
  All three ensemble components are re-fit with the weights (HGB and
  logreg accept `sample_weight` natively via sklearn; LGBM via its own
  `sample_weight` arg, so no member runs unweighted), then the pick is
  the usual percentile-rank mean within `(date, tims)`. Rationale: a
  re-fit under shifted loss mass is not a monotone transform, so
  within-group orderings can move for all groups (verified: 667-875
  picks changed per seed vs base).
- **V2 g3blend** — a dedicated LGBM (config a, binary objective) trained
  ONLY on `tims==3` training rows (~24.6k rows/fold, 2017+), scored on
  the fold's test rows; the g3 pick then uses
  `0.5*rank_pct(ensemble) + 0.5*rank_pct(g3 model)` within `(date,
  tims)`; g1/g2 picks keep the raw default-ensemble scores exactly.
  `g3pure` (g3 pick from the dedicated model alone) is printed from the
  same fitted model as a diagnostics arm — no extra fits.

All features are the standard pre-game `DATASET_FEATURES` (V2+V4 set,
post-V6 leak correction: no same-game `lineup_unit`/`pp_unit`); g3
inputs are the prior-game form/role/slot proxies already in the frame
(`gpg/hgpg/prev2/3_goals/games_since_goal/lineup_position_group/
shot-TOI block/...`). Baselines (`gpg`, `hgpg`) and the unweighted
`base` ensemble are computed in the same pass on the same folds, so
every McNemar comparison is perfectly paired within-run.

Reference-universe note: the refreshed `data/ml` CSV cache now also holds
the V15 2013-17 rows (frame 617,310 rows), which the published reference
numbers never trained on, and V15 already showed pre-2017 rows hurt the
ensemble. `exp_group3.py` therefore defaults to `--train-min-date
2017-10-01`, keeping the reference training universe (~380k train rows/fold; re-deriving the frame needs `--rebuild-frame`). The
in-run `base` arm reproduces the published ensemble references to within
cache-refresh drift: seed 1 **926/3159 = 29.313%** (published 929 =
29.408%, −0.095pp) and seed 3 **925/3159 = 29.281%** (published 942 =
29.820%, −0.539pp) — the drift is from the re-paged cache, not the
protocol; all variant-vs-base deltas and McNemars below are computed
within-run (paired on identical folds), so they are unaffected.

Diagnostics arm `znop`: per-group z-score normalisation of the base
ensemble scores changed **0 of 3,159** picks on both seeds (confirmed
`groups compared: 3159; identical top-1 picks: True`), quantifying that
with-in-group monotone rescaling can never change a pick — it was not
spent as a training variant.

## Commands

```bash
cd SmartScore-backtrack
for s in 1 3; do
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/exp_group3.py --env dev --seed "$s" \
    --oof-file data/ml/oof_v16_group3_seed"$s".csv
done
```

(Runs log to /tmp/opencode/v16_group3_seed{1,3}.log; copies in
data/ml/. Frame cached once as data/ml/v16_group3_frame.pkl.)

## Results

### Stage 1 — V1 group-weight, seeds 1 and 3 (recorded in the same pass as V2; the folds are identical, all arms paired)

| arm | seed | pooled hits / 3159 | acc | folds mean ± std | Δ vs base | McNemar vs base | McNemar vs gpg | McNemar vs hgpg |
|---|---:|---:|---:|---:|---:|---|---|---|
| base | 1 | 926 | 29.313% | 29.315 ± 2.273 | — | — | 408-319, p=0.0011 | 314-274, p=0.108 |
| w2 | 1 | 914 | 28.933% | 28.933 ± 2.881 | **−0.380pp** | 112-124, p=0.474 | 402-325, p=0.0048 | 302-274, p=0.261 |
| w4 | 1 | 917 | 29.028% | 29.029 ± 2.225 | **−0.285pp** | 161-170, p=0.660 | 402-322, p=0.0033 | 302-271, p=0.210 |
| base | 3 | 925 | 29.281% | 29.282 ± 0.511 | — | — | 422-334, p=0.0015 | 315-276, p=0.118 |
| w2 | 3 | 911 | 28.838% | 28.840 ± 1.978 | **−0.443pp** | 118-132, p=0.411 | 407-333, p=0.0072 | 304-279, p=0.320 |
| w4 | 3 | 904 | 28.617% | 28.616 ± 1.059 | **−0.664pp** | 152-173, p=0.267 | 405-338, p=0.0154 | 291-273, p=0.474 |

Pooled over both seeds (6,318 picks): w2 1,825 = **28.885%** (−0.397pp),
w4 1,821 = **28.822%** (−0.460pp).

Per-group V1 (seed1 / seed3): w2 **g1=39.4/37.8 g2=26.7/27.8 g3=20.7/20.9**;
w4 **g1=40.0/38.1 g2=26.9/27.2 g3=20.2/20.6**. Base g3 = 21.7/21.1.
Upweighting g3 rows did not even lift g3 accuracy — it fell (−1.0/−0.5pp
g3) while w4 also hurt g1. The fits see most g3 rows in the
`tims==0`-heavy all-rows universe far less than a ×2 weight implies
relative to per-group difficulty, and the extra weight mostly reshuffles
picks between all groups at random cost.

### Stage 2 — V2 dedicated g3 model, seeds 1 and 3

| arm | seed | pooled hits / 3159 | acc | folds mean ± std | Δ vs base | McNemar vs base | McNemar vs gpg | McNemar vs hgpg |
|---|---:|---:|---:|---:|---:|---|---|---|
| g3blend | 1 | 914 | 28.933% | 28.935 ± 2.175 | **−0.380pp** | 47-59, p=0.285 | 411-334, p=0.0053 | 322-294, p=0.277 |
| g3pure | 1 | 898 | 28.427% | 28.428 ± 1.455 | −0.886pp | 67-95, p=0.034 (worse) | 410-349, p=0.0293 | 326-314, p=0.664 |
| g3blend | 3 | 919 | 29.091% | 29.092 ± 0.439 | **−0.190pp** | 45-51, p=0.610 | 429-347, p=0.0036 | 324-291, p=0.197 |
| g3pure | 3 | 910 | 28.807% | 28.806 ± 0.794 | −0.474pp | 74-89, p=0.273 | 436-363, p=0.0108 | 338-314, p=0.368 |

Pooled over both seeds: g3blend 1,833 = **29.009%** (−0.273pp), g3pure
1,808 = **28.618%** (−0.664pp).

Per-group hit rates (g1/g2/g3, seed1 / seed3): g3blend
**39.2/27.1/20.5** and **38.8/27.9/20.5**; g3pure g3 **19.0/19.7**; base
g3 **21.7/21.1**. The dedicated model, trained per-fold on ~24.6k g3
rows, is a strictly WORSE ranker inside g3 itself — the group's signal
is already better served by the full-universe fit (380k rows share the
player-quality structure g3 needs; isolating g3 costs 15x the data for
no specialisation gain).

### Era subsets (pooled picks)

Seed 1: base 30.28/29.70% (2023-24+/2025-26); w2 30.18/30.33; w4
30.03/30.17; g3blend 29.89/30.17; g3pure 29.34/29.86.
Seed 3: base 29.89/32.23; w2 29.59/30.96; w4 29.09/31.91; g3blend
29.59/31.44; g3pure 29.09/31.28. No variant controls an era advantage;
the drift is pick noise.

## Verdict

**Both variants REJECTED** — neither reaches +0.5pp pooled on even one
seed (both are negative on both), so the "+0.5pp on both seeds" bar
fails outright and no seed-0 winner run was made:

| variant | seed 1 Δ | seed 3 Δ | verdict |
|---|---:|---:|---|
| V1 w2 (tims=3 weight ×2, all three members weighted) | −0.380pp | −0.443pp | rejected |
| V1 w4 (tims=3 weight ×4) | −0.285pp | −0.664pp | rejected |
| V2 g3blend (dedicated g3 LGBM, 0.5 blend for g3 pick) | −0.380pp | −0.190pp | rejected |
| V2 g3pure (dedicated model alone for g3 pick, diagnostic) | −0.886pp | −0.474pp | rejected |

McNemar-consistent: every variant-vs-base p ≥ 0.27 except g3pure seed 1
(p=0.034, in the WORSE direction). Every variant still clears gpg
comfortably (p ≈ 0.003-0.02) and stays a wash vs hgpg (p ≥ 0.20) — i.e.
the variants simply inherit the base ensemble's edge and add noise.

Mechanistic read for the record: (a) group-3 difficulty is not an
under-representation problem — doubling/quadrupling its loss weight
redistributes within-group orderings at a net cost; (b) isolating g3
training data starves the model (24.6k rows vs 380k) for no
specialisation gain, and halving its vote with the ensemble only dilutes
the ensemble. Fair-severity: g3's TRUE base rate is simply lower
(fewer/tier-worse scorers per slate); improving it needs new signal, not
reweighting, and the two cheapest such levers were already rejected
(V12-identity, opponent history). OOF evidence:
`data/ml/oof_v16_group3_seed{1,3}.csv` (gitignored).

Default remains the unchanged `--model ensemble` (29.71% mean all-era).
