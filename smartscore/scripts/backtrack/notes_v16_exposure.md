### V16-exposure — multiplicative prior xG/60 x projected TOI: REJECTED

Hypothesis (never-tried combination): the two components that each failed
testing **alone** — (1) prior-games expected-goals rate from the first-party
per-shot xG proxy (V8-own-xG: rejected -0.16/-0.35pp), and (2) a prior-games
projected TOI (V4 TOI rates: neutral/diluted in the block) — may matter
**multiplicatively**. Exposure = prior xG/60 minutes x projected TOI minutes:
a player who shoots from good locations *and* projects to heavy ice time is
the archetypal Tims challenge group leader.

**Feature construction (single new numeric feature `exposure_xg60_toi`; 43
features total).**

- *Prior xG/60*: per-shot out-of-fold xG from the V8 first-party model (raw
  MoneyPuck events: distance, angle, type, rebound, rush, skater diff,
  timeSinceLastEvent, period, empty net; own LGBM, "fit nothing on test
  dates" — shot fold i never trains on fold-i dates). Summed per player-game
  (zero-attempt games = 0 xG, within shot-data coverage 2017-10-04..2026-06-14),
  then season-to-date prior sums per player: `xg_before = cumsum - current row`
  (model.shot_features pattern), `xg60_prior = 60 * xg_before / toi_before`
  (TOI from `player_games`, strictly earlier same-season games only).
  Cached artefacts `data/moneypuck/shots/shot_xg_seed{1,3}.csv` reused after
  verifying their `fold` column matches `model.fold_blocks` for seeds 1/3
  exactly (0 mismatches on 564,522 challenge-date shot rows per seed).
- *Projected TOI*: `r10_toi_pg` — prior-10-games TOI mean per player
  (V4 block in `shot_features()`, strictly pre-game). Not the same game's TOI,
  not oracle.
- `exposure_xg60_toi = xg60_prior * r10_toi_pg`. No shrinkage, no raw
  components added (the product is the candidate). Challenge-row coverage
  98.92% (91,441 / 92,440); NaN = first-NHL-game or out-of-coverage rows.
  Spot-checks: three sampled challenge player-dates recomputed independently,
  matched to <1e-9.

**Protocol (unchanged).** Date-blocked k=5 (`fold_blocks`), train on ALL
scored rows, `--model ensemble` arm (embeds hgb/logreg/lgbm per fold),
`OMP_NUM_THREADS=2 LGBM_N_JOBS=2`, lightgbm==4.7.0. **Same-frame baseline arm
("base", vanilla 42-feature set) run with the identical folds in the same
process** for exact paired picks. Note the archive cache is the V15 copy
(617,310 rows incl. 2013-17 pointwise-only rows); the 2013-17 rows have no
xG coverage so `exposure_xg60_toi` is NaN there — training rows only.

Exact commands:

```bash
cd SmartScore-backtrack
for s in 1 3; do
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/exp_exposure.py --env dev --seed "$s"
done
```

Seed 1 was run first (1330s base + 526s exposure); seed 3 was run separately
under `nohup` with the same command (525s base + 492s exposure). Both runs
completed end to end; nothing was rerun or cherry-picked. gpg/hgpg reference
picks reproduce exactly on both seeds: gpg **837/3159 = 26.496%**, hgpg
**886/3159 = 28.047%**. Mean AUC moved only 0.6100 → 0.6108 (seed 1) and
0.6103 → 0.6111 (seed 3).

## Results

### Pooled accuracy, seeds 1 and 3

Challenge set is 3,159 scored player-games per seed. Deltas are versus the
paired same-seed base arm; McNemar is exact on the pooled paired picks.

| seed | arm | method | hits / 3159 | acc | Δ vs paired base | McNemar vs base |
|---:|---|---|---:|---:|---:|---|
| 1 | base | ensemble | 926 | 29.313% | — | — |
| 1 | exposure | ensemble | 921 | 29.155% | **−0.158pp** | 125-130, p = 0.8023 |
| 1 | base | LGBM | 931 | 29.471% | — | — |
| 1 | exposure | LGBM | 922 | 29.186% | **−0.285pp** | 119-128, p = 0.6108 |
| 3 | base | ensemble | 935 | 29.598% | — | — |
| 3 | exposure | ensemble | 933 | 29.535% | **−0.063pp** | 130-132, p = 0.9508 |
| 3 | base | LGBM | 929 | 29.408% | — | — |
| 3 | exposure | LGBM | 924 | 29.250% | **−0.158pp** | 131-136, p = 0.8067 |

Two-seed totals (6,318 evaluations per arm): ensemble base 1,861 = 29.456% vs
exposure 1,854 = 29.345% (**−0.111pp**); LGBM base 1,860 = 29.440% vs
exposure 1,846 = 29.218% (**−0.222pp**).

Per-fold mean ± std (population over the five date blocks, %):

| seed | arm | ensemble | LGBM |
|---:|---|---|---|
| 1 | base | 29.315 ± 2.863 | 29.474 ± 3.308 |
| 1 | exposure | 29.156 ± 3.157 | 29.188 ± 2.286 |
| 3 | base | 29.598 ± 0.459 | 29.408 ± 0.970 |
| 3 | exposure | 29.536 ± 1.067 | 29.251 ± 0.578 |

Per-fold accuracy (%), ensemble / LGBM:

| seed | arm | fold 1 | fold 2 | fold 3 | fold 4 | fold 5 |
|---:|---|---:|---:|---:|---:|---:|
| 1 | base | 33.02 / 33.65 | 27.01 / 26.38 | 26.07 / 26.07 | 31.11 / 31.75 | 29.37 / 29.52 |
| 1 | exposure | 33.49 / 31.12 | 27.65 / 28.44 | 25.43 / 25.91 | 31.11 / 31.59 | 28.10 / 28.89 |
| 3 | base | 29.23 / 30.65 | 29.70 / 27.96 | 30.17 / 29.70 | 29.84 / 29.21 | 29.05 / 29.52 |
| 3 | exposure | 30.02 / 29.23 | 27.80 / 28.28 | 29.38 / 29.38 | 29.84 / 29.68 | 30.63 / 29.68 |

The exposure column moves accuracy by well under one fold's standard
deviation in every block; seed 1's ensemble gain in fold 1 (+0.47pp) is
cancelled by fold 3 (−0.64pp), and seed 3's fold-1 gain (+0.79pp) by fold 2
(−1.90pp).

### Per-group and era subsets

Group hit rates in % (g1/g2/g3 are the challenge TOI groups); era columns are
hits within 2023-24+ (n = 2,001) and 2025-26 (n = 633).

| seed | arm | method | g1 | g2 | g3 | 2023-24+ | 2025-26 |
|---:|---|---|---:|---:|---:|---:|---:|
| 1 | base | ensemble | 39.601 | 27.540 | 20.798 | 29.485 | 30.016 |
| 1 | exposure | ensemble | 38.936 | 27.445 | 21.083 | 29.285 | 29.858 |
| 1 | base | LGBM | 39.886 | 28.680 | 19.848 | 30.185 | 30.332 |
| 1 | exposure | LGBM | 39.411 | 27.540 | 20.608 | 29.585 | 31.122 |
| 3 | base | ensemble | 39.031 | 28.015 | 21.747 | 29.935 | 30.806 |
| 3 | exposure | ensemble | 39.411 | 27.635 | 21.557 | 29.335 | 31.438 |
| 3 | base | LGBM | 38.746 | 28.395 | 21.083 | 29.585 | 30.332 |
| 3 | exposure | LGBM | 38.557 | 28.965 | 20.228 | 29.085 | 31.438 |

No group moves more than ±0.9pp, and the direction flips by seed (seed 1
loses g1/g2 and gains g3; seed 3 gains g1 and loses g2/g3 for the ensemble,
the reverse for LGBM). The 2025-26 subset is the only consistently positive
cell, but it is 633 games and the 2023-24+ subset (2,001 games) moves the
wrong way in every exposure arm.

### Exact McNemar vs reference arms

Each cell is wins-losses for the row arm versus the reference (exact
two-sided binomial). "vs base" compares the exposure arm to the paired base
arm of the same method and seed.

| seed | arm | method | vs gpg | vs hgpg | vs paired base |
|---:|---|---|---|---|---|
| 1 | base | ensemble | 417-328, p = 0.0012 | 304-264, p = 0.1017 | — |
| 1 | base | LGBM | 419-325, p = 0.0006 | 323-278, p = 0.0726 | — |
| 1 | exposure | ensemble | 411-327, p = 0.0022 | 314-279, p = 0.1626 | 125-130, p = 0.8023 |
| 1 | exposure | LGBM | 414-329, p = 0.0020 | 315-279, p = 0.1509 | 119-128, p = 0.6108 |
| 3 | base | ensemble | 425-327, p = 0.0004 | 316-267, p = 0.0467 | — |
| 3 | base | LGBM | 419-327, p = 0.0009 | 338-295, p = 0.0950 | — |
| 3 | exposure | ensemble | 420-324, p = 0.0005 | 329-282, p = 0.0627 | 130-132, p = 0.9508 |
| 3 | exposure | LGBM | 416-329, p = 0.0016 | 334-296, p = 0.1404 | 131-136, p = 0.8067 |

Both arms remain clearly better than the gpg reference on both seeds, and
both remain statistically indistinguishable from hgpg. Critically, the
exposure arm never separates from its paired base arm (all four p ≥ 0.61).

## Why the multiplicative hypothesis failed

The product is not a genuine interaction: on the 92,440 challenge rows
(91,441 with a non-null exposure) `exposure_xg60_toi` correlates **0.940
Pearson / 0.953 Spearman with its own `xg60_prior` component** but only
**0.135 / 0.080 with `r10_toi_pg`**, because the two components are weakly
*negatively* correlated with each other (r = −0.169). Multiplying by
projected TOI therefore barely rescales the xG/60 signal — it does not
identify the intended "good shooter who also plays a lot" subgroup, since
high prior xG/60 is if anything associated with lower TOI (small-sample
fourth-liners and defencemen score at higher per-60 rates). Worse, the
product is largely redundant with columns the frame already carries:
r = 0.854 with `hgpg` (ρ = 0.885), 0.778 with `gpg` (ρ = 0.814), 0.799 with
`sd_shots_pg` and 0.730 with `r10_shots_pg`. The GBM already reads the
season-to-date goal/shot/TOI rates separately and can approximate any useful
product itself, so the explicit feature only adds estimation noise — especially
for low-TOI players, where `xg60_prior` is built on a handful of prior shots.
The −0.16pp/−0.29pp (seed 1) and −0.06pp/−0.16pp (seed 3) pooled moves are
consistent with that noise, not with a small real effect.

## Verdict: REJECTED

The pre-declared adoption gate required **≥ +0.5pp on BOTH seeds** (and on
both the ensemble and LGBM arms). All four seed-by-seed deltas are negative:

- seed 1: ensemble **−0.158pp** (921 vs 926, McNemar 125-130, p = 0.8023);
  LGBM **−0.285pp** (922 vs 931, 119-128, p = 0.6108).
- seed 3: ensemble **−0.063pp** (933 vs 935, 130-132, p = 0.9508);
  LGBM **−0.158pp** (924 vs 929, 131-136, p = 0.8067).

There is no seed, arm or subset that clears the bar; the feature is a
statistical tie at best and is dominated by its xG/60 component. Reject.
`model.py` and the default 42-feature model are unchanged; the V15 numeric
default remains in place. The candidate and its exact experiment stay on
disk (`exp_exposure.py`, seed-1/seed-3 results CSVs and logs) so the
negative result is reproducible; the 18MB OOF dumps remain gitignored under
`data/ml/`.
