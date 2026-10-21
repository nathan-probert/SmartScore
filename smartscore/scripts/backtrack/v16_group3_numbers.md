# V16-group3 — numbers log (raw run results)

Source: `OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python
smartscore/scripts/backtrack/exp_group3.py --env dev --seed {1,3}
--oof-file data/ml/oof_v16_group3_seed{s}.csv`
(2017+ training filter on by default via `--train-min-date 2017-10-01` —
the quoted reference ensemble never trained on the V15 pre-2017 rows.)

Reference reproduction check (from `data/ml/oof_v6c_ensemble_seed{1,3}.csv`,
independent of this run): seed 1 = 929/3159 = 29.408%; seed 3 = 941/3159
(V-log printed 942 / 29.820%; the OOF re-pick differs by one tie).
The in-run `base` arm is 3 picks off that reference on seed 1 (926) because the
feature derivations were re-run on the current 617k cache — the derived
rolling/career shot features of 2017-22 rows now include pre-2017 history that
did not exist when the reference was computed. All McNemar/delta comparisons
below use the in-run `base` arm, which is exactly paired (same folds, same
rows, unmodified scoring); gpg 837/886 reproduce exactly.

Arms: `gpg` / `hgpg` (baselines), `base` (unmodified rank ensemble hgb+logreg+lgbm),
`w2` (sample weight 2.0 on tims==3 training rows for all three components),
`w4` (weight 4.0), `g3blend` (base ensemble everywhere except: g3 pick uses
0.5*rank(base)+0.5*rank(dedicated g3 LGBM) within (date,tims)), `g3pure`
(g3 pick from the dedicated LGBM alone), `znop` (z-score within group —
no-op check, expected 0 changed picks).

## Seed 1 (run total 2046s, frame cache build 364s)

| arm | hits/3159 | acc | folds mean±std | g1 / g2 / g3 | 2023-24+ | 2025-26 |
|---|---:|---:|---:|---|---:|---:|
| gpg | 837 | 26.496% | 26.494 ± 1.963 | 37.8 / 23.7 / 17.9 | 542/2001=27.09% | 179/633=28.28% |
| hgpg | 886 | 28.047% | 28.045 ± 2.559 | 38.4 / 26.0 / 19.8 | 563/2001=28.14% | 192/633=30.33% |
| base | 926 | 29.313% | 29.315 ± 2.273 | 39.2 / 27.1 / 21.7 | 606/2001=30.28% | 188/633=29.70% |
| w2 | 914 | 28.933% | 28.933 ± 2.881 | 39.4 / 26.7 / 20.7 | 604/2001=30.18% | 192/633=30.33% |
| w4 | 917 | 29.028% | 29.029 ± 2.225 | 40.0 / 26.9 / 20.2 | 601/2001=30.03% | 191/633=30.17% |
| g3blend | 914 | 28.933% | 28.935 ± 2.175 | 39.2 / 27.1 / 20.5 | 598/2001=29.89% | 191/633=30.17% |
| g3pure | 898 | 28.427% | 28.428 ± 1.455 | 39.2 / 27.1 / 19.0 | 587/2001=29.34% | 189/633=29.86% |

## Seed 1 — exact McNemar (pooled paired picks; a-b = variant-hit×base-miss × base-hit×variant-miss)

| arm | vs gpg | vs hgpg | vs base |
|---|---|---|---|
| base | 408-319, p=0.0011 | 314-274, p=0.1077 | — |
| w2 | 402-325, p=0.0048 | 302-274, p=0.2606 | 112-124, p=0.4740 (667 picks changed) |
| w4 | 402-322, p=0.0033 | 302-271, p=0.2101 | 161-170, p=0.6602 (869 picks changed) |
| g3blend | 411-334, p=0.0053 | 322-294, p=0.2766 | 47-59, p=0.2853 (357 picks changed) |
| g3pure | 410-349, p=0.0293 | 326-314, p=0.6637 | 67-95, p=0.0336, worse (570 picks changed) |

znop check: 3159 groups compared, top-1 picks identical (True) — per-group
z-scoring is provably pick-invariant, as expected.

## Seed 3 (run total 1343s, frame cache reused)

| arm | hits/3159 | acc | folds mean±std | g1 / g2 / g3 | 2023-24+ | 2025-26 |
|---|---:|---:|---:|---|---:|---:|
| gpg | 837 | 26.496% | 26.497 ± 2.009 | 37.8 / 23.7 / 17.9 | 542/2001=27.09% | 179/633=28.28% |
| hgpg | 886 | 28.047% | 28.049 ± 1.080 | 38.4 / 26.0 / 19.8 | 563/2001=28.14% | 192/633=30.33% |
| base | 925 | 29.281% | 29.282 ± 0.511 | 38.8 / 27.9 / 21.1 | 598/2001=29.89% | 204/633=32.23% |
| w2 | 911 | 28.838% | 28.840 ± 1.978 | 37.8 / 27.8 / 20.9 | 592/2001=29.59% | 196/633=30.96% |
| w4 | 904 | 28.617% | 28.616 ± 1.059 | 38.1 / 27.2 / 20.6 | 582/2001=29.09% | 202/633=31.91% |
| g3blend | 919 | 29.091% | 29.092 ± 0.439 | 38.8 / 27.9 / 20.5 | 592/2001=29.59% | 199/633=31.44% |
| g3pure | 910 | 28.807% | 28.806 ± 0.794 | 38.8 / 27.9 / 19.7 | 582/2001=29.09% | 198/633=31.28% |

## Seed 3 — exact McNemar

| arm | vs gpg | vs hgpg | vs base |
|---|---|---|---|
| base | 422-334, p=0.0015 | 315-276, p=0.1180 | — |
| w2 | 407-333, p=0.0072 | 304-279, p=0.3202 | 118-132, p=0.4110 (662 picks changed) |
| w4 | 405-338, p=0.0154 | 291-273, p=0.4741 | 152-173, p=0.2672 (875 picks changed) |
| g3blend | 429-347, p=0.0036 | 324-291, p=0.1969 | 45-51, p=0.6101 (362 picks changed, all in g3 by construction) |
| g3pure | 436-363, p=0.0108 | 338-314, p=0.3677 | 74-89, p=0.2728, worse (576 picks changed, all in g3) |

znop check (seed 3): 3159 groups, identical top-1 picks (True).

## Two-seed summary (deltas in pp vs the in-run base arm)

| arm | seed1 | seed3 | mean | verdict bar (+0.5pp both seeds) |
|---|---:|---:|---:|---|
| w2 (g3 weight 2) | −0.380 | −0.443 | −0.412 | fail seed 1 |
| w4 (g3 weight 4) | −0.285 | −0.664 | −0.475 | fail both |
| g3blend (0.5 base + 0.5 dedicated-g3 rank, g3 pick only) | −0.380 | −0.190 | −0.285 | fail both |
| g3pure (dedicated g3 model alone as the g3 pick) | −0.886 | −0.476 | −0.681 | fail both |

No arm beat base on either seed → the +0.5pp-on-both-seeds test cannot be
passed → seed 0 / adjunct verification not run (protocol stop rule).


