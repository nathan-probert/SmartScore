# V16-interact — home × player-form, rest × lineup-role, opponent-strength-relative

**Verdict: REJECTED.** The ensemble arm clears the +0.5pp bar on seed 1 by
0.006pp but only reaches +0.25pp on seed 3; the LGBM arm fails on both seeds.
A mixed LGBM/ensemble result (or an ensemble-only pass on one seed) does not
meet the pre-declared gate, so seed 0 was not run.

## What was tested

Twenty pairwise interaction columns added on top of the corrected V16 frame
(60 numeric + 2 categorical, `--train-subset all`, in-run gpg/hgpg baselines on
identical folds). `model.py` was not edited; the script imports its frame and
fold machinery and extends `NUMERIC_FEATURES` at runtime. All inputs are
strictly pre-game, no new joins, no same-game shift-derived `lineup_unit` /
`pp_unit`.

Group A — home × player form (8):

```
home_x_gpg  home_x_hgpg  home_x_five_gpg  home_x_ppg
home_x_rank_gpg  home_x_rank_hgpg  home_x_ewm_goals  home_x_prev2_goals
```

Group B — rest × lineup role (4): `club_rest_days_F`, `club_rest_days_D`,
`days_since_game_F`, `days_since_game_D`. Split on the static roster
`lineup_position_group` (F vs D), not on the same-game shift-derived
`lineup_unit`.

Group C — opponent strength relative to the date's average opponent (8):

```
gpg_x_opp_gaa  hgpg_x_opp_gaa  five_gpg_x_opp_gaa
gpg_x_opp_save_pct  hgpg_x_opp_save_pct
gpg_x_rel_opp_gaa  hgpg_x_rel_opp_gaa  gpg_x_opp_wins
```

`*_x_rel_opp_gaa` = player rate × (this row's `opp_gaa` − mean `opp_gaa` across
the distinct opposing clubs on that date). Opponent columns are the row's own
same-snapshot pre-game values, joined by team abbreviation upstream, never
`placeName`.

Interaction coverage on challenge rows (92,440): home × form 100%; rest × role
`_F` 69.4% / `_D` 29.8% (NaN outside the role by construction); opponent terms
96.5% (missing where the opposing stats of a team-debut row are absent).

## Config

- Date-blocked 5-fold over the 1,053 challenge dates (`split=kfold`, `splits=5`), `--train-subset all`, `--model ensemble`, `--lgbm-config a`, `ranker=none`.
- `lightgbm==4.7.0`, `OMP_NUM_THREADS=2`, `LGBM_N_JOBS=2`.
- Frame: 617,310 rows (cached build 89s on seed 3). Seed 1 total 2,424s; seed 3 total 815s.
- Reference: same-frame no-interaction OOF, `data/ml/oof_v16_calib_ens_seed{1,3}.csv`; fold assignment matched 0/92,440 mismatches on both seeds. The script also re-scores that file with `model.daily_picks` (no refit).

Commands:

```bash
OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/exp_interact.py --env dev --seed 1
OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/exp_interact.py --env dev --seed 3
```

(`--seed 1` was already on disk, `data/ml/v16_interact_seed1.log`; it already
contained the full standard table, so task 1's re-derivation was a no-op. Seed 3
was launched with `setsid nohup … > data/ml/v16_interact_seed3.log 2>&1` and
polled to completion; it finished in 815s with no retry needed.)

## Pooled OOF results (canonical, from the run logs)

Seed 1 — AUC per fold 0.6198, 0.5999, 0.6012, 0.6222, 0.6089 (0.6104 ± 0.0092):

| arm | pooled | folds mean ± std | g1 | g2 | g3 | 2023-24+ | 2025-26 |
|---|---|---|---|---|---|---|---|
| lgbm | 908/3159 = 28.743% | 28.746 ± 2.729 | 39.8 (419/1053) | 26.3 (277/1053) | 20.1 (212/1053) | 591/2001 = 29.54% | 194/633 = 30.65% |
| ensemble | 941/3159 = 29.788% | 29.790 ± 2.152 | 41.1 (433/1053) | 26.7 (281/1053) | 21.6 (227/1053) | 606/2001 = 30.28% | 199/633 = 31.44% |
| hgb | 933/3159 = 29.535% | 29.534 ± 2.715 | 40.7 (429/1053) | 27.1 (285/1053) | 20.8 (219/1053) | 607/2001 = 30.33% | 186/633 = 29.38% |
| logreg | 891/3159 = 28.205% | 28.207 ± 2.188 | 38.4 (404/1053) | 26.9 (283/1053) | 19.4 (204/1053) | 565/2001 = 28.24% | 190/633 = 30.02% |
| hgpg | 886/3159 = 28.047% | 28.045 ± 2.861 | 38.4 (404/1053) | 26.0 (274/1053) | 19.8 (208/1053) | 563/2001 = 28.14% | 192/633 = 30.33% |
| gpg | 837/3159 = 26.496% | 26.494 ± 2.195 | 37.8 (398/1053) | 23.7 (250/1053) | 17.9 (189/1053) | 542/2001 = 27.09% | 179/633 = 28.28% |

Seed 3 — AUC per fold 0.6166, 0.6147, 0.6053, 0.6096, 0.6064 (0.6105 ± 0.0045):

| arm | pooled | folds mean ± std | g1 | g2 | g3 | 2023-24+ | 2025-26 |
|---|---|---|---|---|---|---|---|
| lgbm | 932/3159 = 29.503% | 29.501 ± 1.506 | 38.7 (408/1053) | 28.3 (298/1053) | 21.5 (226/1053) | 603/2001 = 30.13% | 200/633 = 31.60% |
| ensemble | 943/3159 = 29.851% | 29.852 ± 0.366 | 40.0 (421/1053) | 28.3 (298/1053) | 21.3 (224/1053) | 605/2001 = 30.23% | 203/633 = 32.07% |
| hgb | 943/3159 = 29.851% | 29.850 ± 0.747 | 40.9 (431/1053) | 27.6 (291/1053) | 21.0 (221/1053) | 596/2001 = 29.79% | 199/633 = 31.44% |
| logreg | 890/3159 = 28.173% | 28.175 ± 1.050 | 38.6 (406/1053) | 26.5 (279/1053) | 19.5 (205/1053) | 566/2001 = 28.29% | 187/633 = 29.54% |
| hgpg | 886/3159 = 28.047% | 28.049 ± 1.208 | 38.4 (404/1053) | 26.0 (274/1053) | 19.8 (208/1053) | 563/2001 = 28.14% | 192/633 = 30.33% |
| gpg | 837/3159 = 26.496% | 26.497 ± 2.246 | 37.8 (398/1053) | 23.7 (250/1053) | 17.9 (189/1053) | 542/2001 = 27.09% | 179/633 = 28.28% |

Per-fold accuracies re-picked from the saved OOFs
(`exp_v16_interact_perfold.csv`; ±1 pick per fold can shift because ~166
date/tims groups have exact score ties, see note below):

| seed | fold | lgbm | ensemble | hgb | logreg | hgpg | gpg |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 31.754 | 32.070 | 33.017 | 29.700 | 31.438 | 29.068 |
| 1 | 2 | 26.698 | 28.436 | 29.068 | 27.488 | 26.856 | 26.856 |
| 1 | 3 | 25.434 | 27.014 | 27.172 | 25.592 | 27.330 | 24.961 |
| 1 | 4 | 31.111 | 31.905 | 31.587 | 31.111 | 30.317 | 27.937 |
| 1 | 5 | 28.730 | 29.683 | 26.825 | 27.143 | 24.286 | 23.651 |
| 3 | 1 | 30.964 | 30.174 | 29.226 | 28.910 | 27.962 | 28.594 |
| 3 | 2 | 28.752 | 29.226 | 30.806 | 27.488 | 27.804 | 26.856 |
| 3 | 3 | 31.122 | 29.542 | 30.490 | 26.698 | 26.224 | 22.907 |
| 3 | 4 | 29.048 | 30.000 | 29.524 | 29.206 | 28.889 | 26.032 |
| 3 | 5 | 27.619 | 30.000 | 29.206 | 28.571 | 29.365 | 28.095 |

## Exact McNemar (pooled paired picks)

In-run baselines, identical folds. `a-b` = variant hit / baseline hit only;
two-sided exact binomial.

Seed 1:

| comparison | a-b | p | changed picks |
|---|---|---|---|
| lgbm vs hgpg | 306-284 | 0.3873 | 1,613 |
| lgbm vs gpg | 398-327 | 0.0093 | 2,005 |
| ensemble vs hgpg | 325-270 | 0.0268 | 1,579 |
| ensemble vs gpg | 420-316 | 0.0001 | 1,975 |
| lgbm vs no-interact ref | 127-150 | 0.1861 (Δ −0.728pp) | 722 |
| ensemble vs no-interact ref | 134-118 | 0.3447 (Δ +0.506pp) | 660 |

Seed 3:

| comparison | a-b | p | changed picks |
|---|---|---|---|
| lgbm vs hgpg | 324-278 | 0.0666 | 1,634 |
| lgbm vs gpg | 408-313 | 0.0005 | 1,978 |
| ensemble vs hgpg | 317-260 | 0.0197 | 1,570 |
| ensemble vs gpg | 421-315 | 0.0001 | 1,987 |
| lgbm vs no-interact ref | 150-147 | 0.9076 (Δ +0.095pp) | 742 |
| ensemble vs no-interact ref | 130-122 | 0.6593 (Δ +0.253pp) | 668 |

Reference re-picks were re-computed independently from the same-frame files
with `model.daily_picks` (NaN scores dropped before `idxmax`): seed 1 lgbm
931/3159, ensemble 925/3159; seed 3 lgbm 929/3159, ensemble 935/3159 — matching
the values printed by the run. gpg (837/3159) and hgpg (886/3159) are identical
across seeds, as they must be for deterministic baselines.

Tie caveat: re-picking the saved OOF probabilities reproduces the log exactly
for LGBM and the baselines, but the ensemble has ~166 exact score ties, so
CSV-order `idxmax` moves 1–2 picks: seed 1 ensemble 942/3159 (Δ +0.538pp vs
ref, 135-118, p=0.3145), seed 3 ensemble 941/3159 (Δ +0.190pp, 129-123,
p=0.7529). Neither reading changes the verdict.

## Decision

Gate: **≥ +0.5pp pooled on BOTH seeds vs the paired no-interaction arm,
McNemar-consistent.**

| seed | lgbm Δ vs ref | LGBM pass? | ensemble Δ vs ref | ensemble pass? |
|---|---|---|---|---|
| 1 | −0.728pp (p=0.1861) | no | +0.506pp (p=0.3447) | threshold only |
| 3 | +0.095pp (p=0.9076) | no | +0.253pp (p=0.6593) | no |

- LGBM fails on both seeds (negative on seed 1, ≈zero on seed 3).
- Ensemble scrapes over +0.5pp on seed 1 by 0.006pp but is only +0.25pp
  (re-pick +0.19pp) on seed 3, and neither paired test is significant.
- No arm passes on both seeds. A mixed result — ensemble passing on one seed
  while LGBM fails — does **not** satisfy the pre-declared gate; the gate is
  per arm on both seeds, not "best arm on any seed".
- Seed 0 was therefore not run.

**REJECTED.** The interaction groups do beat the in-run gpg/hgpg baselines on
both seeds (ensemble vs gpg p=0.0001, vs hgpg p=0.03/0.02; LGBM vs gpg
p=0.009/0.0005), but they do not add ≥ +0.5pp over the same-frame
no-interaction model on both seeds, which is the adoption criterion.

## Artefacts

- `smartscore/scripts/backtrack/exp_interact.py` — experiment script.
- `smartscore/scripts/backtrack/exp_v16_interact_seed1.log`, `..._seed3.log` — full run logs.
- `smartscore/scripts/backtrack/exp_v16_interact_results.csv` — pooled stats re-picked from the OOFs.
- `smartscore/scripts/backtrack/exp_v16_interact_perfold.csv` — per-fold accuracies.
- `smartscore/scripts/backtrack/exp_v16_interact_mcnemar.csv` — paired tests from re-picks.
- OOFs (gitignored): `data/ml/oof_v16interact_seed1.csv`, `data/ml/oof_v16interact_seed3.csv`.
