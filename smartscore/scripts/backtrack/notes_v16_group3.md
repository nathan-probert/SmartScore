# V16-group3 — group-conditional weighting + dedicated group-3 model (rejected)

## Motivation

Baseline per-group pooled accuracy (corrected ensemble, reference): **g1
39.9% / g2 27.2% / g3 20.9%**. Group 3 (the weakest tier of each date's
slate, typically 2nd/3rd-line talent) is half as accurate as group 1 and
drags pooled accuracy (ensemble mean **29.71%**; seeds 0/1/3 =
29.915/29.408/29.820). Three untried levers were declared, all on the SAME
k=5 challenge-date k-fold (`fold_blocks`, `--train-subset all` semantics,
model.py machinery unedited), 3,159 pooled picks per seed:

- **V1 group-weight (`w2`, `w4`)** — group-conditional sample weights:
  every training row weight 1 except `tims==3` rows, weighted W=2 and W=4
  (declared before running). All three ensemble components (HGB / LGBM /
  logreg) re-fit with `sample_weight`, scoring/picking unchanged.
- **V2 dedicated g3 model (`g3blend`, `g3pure`)** — an LGBM classifier
  (config a, binary objective) trained ONLY on the fold's `tims==3`
  training rows (~19-20k rows after the 2017+ filter). The g3 pick becomes
  `0.5 * pct-rank(base ensemble) + 0.5 * pct-rank(g3 model)` within
  `(date, tims)`; g1/g2 keep the base ensemble scores exactly.
  `g3pure` (g3 pick from the dedicated model alone) is printed from the
  same fit, no extra training.
- **V3 per-group score normalisation (`znop`)** — z-score within
  `(date, tims)`, evaluated post-hoc on the saved OOF instead of a
  training pass. **Within a group a z-score is an affine (strictly
  monotone) transform, so the group top-1 is provably unchanged**; it can
  only matter when comparisons cross groups, and pick-time comparisons
  never do (argmax is inside one `(date, tims)` group). Verified
  empirically: 3,159/3,159 groups had identical top-1 picks on both seeds.

All features remain the standard pre-game `DATASET_FEATURES` (post-V6
leak correction; no same-game `lineup_unit`/`pp_unit`). The only
group-aware inputs are `tims` membership itself (pre-game) and the
`rank_*` features, which are same-date pre-game percentiles.

## Why each variant could / could not change picks

- `w2`/`w4`: reweighting changes the fitted function, so within-group
  orderings can change for ALL groups (observed 662-875 changed pooled
  picks per seed vs base). It is a loss-mass shift, not a recalibration.
- `g3blend`/`g3pure`: only g3 picks can change by construction
  (g1/g2 scores are the base arm's identical numbers; the 357-576 changed
  picks all sit in the g3 top-1).
- `znop`: cannot change any pick (monotone invariance); verified True.

## One protocol repair made before running

The `data/ml` cache now carries the V15 pre-2017 rows (617,310 rows), but
the quoted reference ensemble (seeds 1/3 = 29.408/29.820) never trained on
them (V15 verdict: keep 2017+ training). The script therefore filters
training rows to `date >= 2017-10-01` by default
(`--train-min-date 2017-10-01`; `none` restores full-cache training). Even
so the current cache's derived features include pre-2017 history inside
the 2017-18..2020-21 rolling/career shot features (the raw store gained
2013-17 games in V15), so the in-run `base` arm lands at **926/925** hits
for seeds 1/3 instead of 929/941 — a −3/−16-pick re-derivation difference,
well inside fold noise. gpg (837) and hgpg (886) reproduce exactly on both
seeds, validating the fold wiring. All variant comparisons below are vs
the in-run `base` arm, which is perfectly paired (same folds, same rows,
unmodified scoring).

## Commands

```bash
cd SmartScore-backtrack
for s in 1 3; do
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/exp_group3.py --env dev --seed "$s" \
    --oof-file data/ml/oof_v16_group3_seed"$s".csv
done
# per-seed totals ~34 min / ~22 min under concurrent-worker load;
# a pickled frame cache data/ml/v16_group3_frame.pkl (617,310 rows) avoids
# the ~6 min add_derived rebuild on every run.
```

## Results

Pooled out-of-fold picks (hits/3159, per-group hit-%, era subsets):

| seed | arm | hits | acc | folds mean±std | g1/g2/g3 | 2023-24+ | 2025-26 |
|---|---|---:|---:|---:|---|---:|---:|
| 1 | base | 926 | 29.313% | 29.315 ± 2.273 | 39.2/27.1/21.7 | 606/2001 = 30.28% | 188/633 = 29.70% |
| 1 | w2 | 914 | 28.933% | 28.933 ± 2.881 | 39.4/26.7/20.7 | 604/2001 = 30.18% | 192/633 = 30.33% |
| 1 | w4 | 917 | 29.028% | 29.029 ± 2.225 | 40.0/26.9/20.2 | 601/2001 = 30.03% | 191/633 = 30.17% |
| 1 | g3blend | 914 | 28.933% | 28.935 ± 2.175 | 39.2/27.1/20.5 | 598/2001 = 29.89% | 191/633 = 30.17% |
| 1 | g3pure | 898 | 28.427% | 28.428 ± 1.455 | 39.2/27.1/19.0 | 587/2001 = 29.34% | 189/633 = 29.86% |
| 3 | base | 925 | 29.281% | 29.282 ± 0.511 | 38.8/27.9/21.1 | 598/2001 = 29.89% | 204/633 = 32.23% |
| 3 | w2 | 911 | 28.838% | 28.840 ± 1.978 | 37.8/27.8/20.9 | 592/2001 = 29.59% | 196/633 = 30.96% |
| 3 | w4 | 904 | 28.617% | 28.616 ± 1.059 | 38.1/27.2/20.6 | 582/2001 = 29.09% | 202/633 = 31.91% |
| 3 | g3blend | 919 | 29.091% | 29.092 ± 0.439 | 38.8/27.9/20.5 | 592/2001 = 29.59% | 199/633 = 31.44% |
| 3 | g3pure | 910 | 28.807% | 28.806 ± 0.794 | 38.8/27.9/19.7 | 582/2001 = 29.09% | 198/633 = 31.28% |

Delta vs in-run base (pp): w2 −0.380/−0.443, w4 −0.285/−0.664, g3blend
−0.380/−0.190, g3pure −0.886/−0.476 for seeds 1/3. **No arm beat base on
either seed**, so the ≥ +0.5pp-on-both-seeds bar fails everywhere → seed 0
was not run (protocol stop rule).

Exact McNemar (pooled paired picks; x-y = variant-hits-basemisses ×
base-hits-variant-misses; p vs BOTH gpg and hgpg, plus vs base):

| arm | seed | vs gpg | vs hgpg | vs base (changed picks) |
|---|---|---|---|---|
| base | 1 | 408-319, p=0.0011 | 314-274, p=0.1077 | — |
| w2 | 1 | 402-325, p=0.0048 | 302-274, p=0.2606 | 112-124, p=0.4740 (667) |
| w4 | 1 | 402-322, p=0.0033 | 302-271, p=0.2101 | 161-170, p=0.6602 (869) |
| g3blend | 1 | 411-334, p=0.0053 | 322-294, p=0.2766 | 47-59, p=0.2853 (357) |
| g3pure | 1 | 410-349, p=0.0293 | 326-314, p=0.6637 | 67-95, p=0.0336 worse (570) |
| base | 3 | 422-334, p=0.0015 | 315-276, p=0.1180 | — |
| w2 | 3 | 407-333, p=0.0072 | 304-279, p=0.3202 | 118-132, p=0.4110 (662) |
| w4 | 3 | 405-338, p=0.0154 | 291-273, p=0.4741 | 152-173, p=0.2672 (875) |
| g3blend | 3 | 429-347, p=0.0036 | 324-291, p=0.1969 | 45-51, p=0.6101 (362) |
| g3pure | 3 | 436-363, p=0.0108 | 338-314, p=0.3677 | 74-89, p=0.2728 worse (576) |

Every variant is still significantly better than gpg (McNemar-consistent)
and still a statistical tie vs hgpg — i.e. the variants move the same
distance the base ensemble does, just lower.

## Why the group-3 attack failed (diagnostics from the seed-1 OOF)

1. **The dedicated g3 model is weaker inside g3, not stronger.** Within-g3
   held-out AUC of the dedicated model was **0.6205/0.6254/0.6346/0.6456/
   0.6179** per fold vs the base ensemble's **0.6344/0.6396/0.6497/0.6690/
   0.6411** — lower on all five folds. ~19k g3-only training rows with
   config-a LGBM is a strictly smaller, noisier model; the shared model's
   capacity is better spent even for g3 rows.
2. **It picks lower-talent players.** Dedicated-model g3 picks average
   `gpg` 0.199 vs base 0.233, and agree with the base g3 pick only in 46%
   of groups — it favours weaker linemate/role profiles that score less.
3. **Upweighting does not buy g3 ranking accuracy either.** Group-conditional
   weights (w2/w4) change 662-875 pooled picks (they do alter within-group
   orderings — this is a re-fit, not a transform), yet g3 accuracy went
   **down** in every seed/weight combination (g3 20.7/20.2 vs base 21.7 on
   seed 1; 20.9/20.6 vs 21.1 on seed 3): the g3-relevant signal is already
   exploited by the shared model, and upweighting mostly displaces general
   capacity that also serves g1/g2 (w4 seed 1 even lifted g1 to 40.0 while
   sinking g3 further).
4. **Per-group z-scoring cannot move any pick** (0/3159 groups changed, both
   seeds) — confirming that group-conditioned normalisation has no
   top-1 effect; any group-aware work must enter training (weights),
   which case 1-3 show is negative here.

## Verdict: **all variants rejected**

- `w2`/`w4` (group-conditional weights): mean −0.41/−0.48pp vs the paired
  base; fail both seeds; the g3 group itself got worse.
- `g3blend`: mean −0.29pp; the g3 pick itself got worse (20.5 vs 21.7/21.1)
  although the blend changes only 357-362 of 1,053 g3 picks.
- `g3pure`: mean −0.68pp, McNemar-consistent worse (570-576 changed picks).
- `znop` per-group normalisation: pick-invariant no-op, verified True.

Keep the default `--model ensemble` unchanged. Group 3 hardness is not a
capacity-allocation problem: base accuracy g3 21.1-21.7 in these runs
(equivalent to the historical 20.9 diagnostic under a different
partition) is what the shared features support, and both a dedicated
g3-only model and g3-upweighted shared models rank g3 rows worse than the
default fit. If anything, the correct conclusion is that **g3 rows are
generated by the same process** — no group-conditional training signal
exists in the current features. New *features* specific to weak-tier
players (e.g. reliable same-date pre-game line-slot confirmation, market
ATGS odds) remain the only plausible lever, and both require data that is
not archived (V10-era PROJECTED lineups, forward-collected odds).

## Notes for reproducibility

- Script: `smartscore/scripts/backtrack/exp_group3.py` (`--seed`, `--splits`,
  `--w2`, `--w4`, `--train-min-date`, `--oof-file`, `--rebuild-frame`,
  `--smoke`). OOF rows per seed in `data/ml/oof_v16_group3_seed{1,3}.csv`
  (untracked cache); frame pickle `data/ml/v16_group3_frame.pkl` (untracked).
- Raw run logs: `/tmp/opencode/v16_group3_seed1.log`,
  `/tmp/opencode/v16_group3_seed3.log`.
- Full numeric tables: `smartscore/scripts/backtrack/v16_group3_numbers.md`.
- The code uses `fit_logreg_weighted` (numeric features only, matching
  `fit_logreg`) and `fit_hgb_w`/`fit_lgbm_w` with identical model configs;
  the dedicated g3 model reuses `categorise` on the g3-only training
  subset. Nothing in model.py was edited.
