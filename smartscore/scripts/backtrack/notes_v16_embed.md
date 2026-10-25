# V16-embed — leak-free player latent factors / embeddings: REJECTED

## Idea

Test whether a small latent player factorization adds finishing-skill /
aging signal beyond the aggregate career rates that empirical-Bayes
shrinkage (V8) and identity features (V12) both failed to convert into
picks. Two implementations were run; the one kept on disk is the
pure-numpy weighted-ALS version (`exp_embed.py`).

## Implementation kept (exp_embed.py): numpy weighted ALS on player-season outcome cells

Per fold (date-blocked k=5, training = every scored row outside the
held-out challenge-date block — the `--train-subset all` regime, 617,310-row
frame incl. V15 2013-17 pre-challenge rows):

- Build a `players x (season x channel)` outcome matrix from raw
  `player_games` lines **of the fold's training dates only**, four
  weighted channels: `gpg = goals/gp` (weight gp), `spg = shots/gp`
  (weight gp), `fin = goals/shots` (weight shots where shots > 0),
  `tpg = toi_min/gp` (weight gp where toi present).
- Weighted column z-score, drop all-zero columns, then ridge-regularised
  weighted ALS `Mz ~ U V'` (SVD init, alternating batched small linear
  solves; `--als-reg` 10.0 ridge lambda, `--als-iters` 15 sweeps, rank 3
  (`--embed-dim` default 3, supported range 2-5).
- Factor columns (dim 3 + 4 reconstruction columns so the block
  carries a cross-season "aged/denoised" prediction of each player's rate
  in the row's season): `e1..e3`, `e_rec_gpg/spg/fin/tpg`.
  Frozen-model emission for the held-out block; unseen test players/season
  cells are NaN, filled with the train mean (0 after standardization,
  train-row stats only).

Leak-free verification: factor fit uses `lines[lines.date.isin(train_dates)]`
only; a per-fold `assert` that fold train/test date sets do not overlap;
emission uses the frozen fold model for all rows; standardization statistics
come from train rows only. In-run baseline reproductions on both seeds:
gpg **837/3159 = 26.496%**, hgpg **886/3159 = 28.047%** — identical to the
documented references; the same-run no-embed LGBM reproduces the V15
augmented-data reference exactly (931/29.471% seed 1, 929/29.408% seed 3),
which is the correct same-data pairing baseline (the frame is now 617,310
rows post-V15; the V6 427k-row references 928/916 are no longer the
same-data baseline).

## First implementation tried (torch MF) — superseded, notes only

A torch CPU supervised matrix factorization
(`logit = b + U[player].S[season]`, dim 6, Adam lr 0.03, wd 1e-2, 2% valid
split with early stopping, ~190-200 epochs/fold; torch 2.14.1 installs
fine from the uv cache, ~5s/fold) was run on seed 1 only: pooled
**922/3159 = 29.186%** vs the 931 baseline (**−0.285pp**, McNemar vs
baseline 126-135 p = 0.621). Diagnostics: the standardized player factors
correlate ~0 with gpg/hgpg (all |r| ≤ 0.02) and with the label (|r| ≤ 0.007)
— the supervised MF learned structure orthogonal to the target. Replaced by
the ALS approach; torch OOF log kept at `/tmp/opencode/v16_torch_seed1.log`.

## Commands

```bash
cd SmartScore-backtrack
for s in 1 3; do
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with torch,lightgbm python \
    smartscore/scripts/backtrack/exp_embed.py --env dev --seed "$s" \
    --splits 5 --models lgbm \
    --save-oof data/ml/oof_v16_embed_lgbm_seed"$s".csv
done
```

(OOF files: 92,440 challenge rows with `lgbm`, `lgbm_e`, the factor
columns and `fold`; torch is no longer imported by the final script.)

## Results — LGBM config a, date-blocked k=5, train=all, seeds 1/3

Factor fit per fold: 2,764-2,775 players x 52 columns (13 seasons x 4
channels), obs share 0.359, test coverage 1.000, ALS 1.9-2.9s/fold,
corr(e_rec_gpg, gpg) 0.66-0.68 on train rows.

| seed | model | pooled hits / 3159 | acc | per-fold mean ± std | AUC |
|---|---|---:|---:|---:|---:|
| 1 | LGBM baseline (no factors) | 931 | 29.471% | 29.474 ± 3.308 | 0.6929 |
| 1 | **LGBM + factors (rank 3)** | **930** | **29.440%** | 29.442 ± 1.707 | 0.6937 |
| 1 | Δ vs baseline | −1 | **−0.031pp** | McNemar 256-257, p = 1.000 | |
| 3 | LGBM baseline (no factors) | 929 | 29.408% | 29.408 ± 0.970 | 0.6934 |
| 3 | **LGBM + factors (rank 3)** | **947** | **29.978%** | 29.978 ± 1.094 | 0.6938 |
| 3 | Δ vs baseline | +18 | **+0.570pp** | McNemar 237-219, p = 0.426 | |

Baseline accounting note (seed 3): the seed-3 pooled LGBM is 929 here vs
the documented V6 428k-frame reference 916/28.997% — the V15 frame with
2013-17 rows is the current training set, and 929/29.408% matches the V15
augmented-training reproduction exactly.

McNemar exact, pooled paired picks:

| comparison | seed 1 | seed 3 |
|---|---|---|
| factors vs same-run no-factor LGBM | 256-257, p = 1.000 | 237-219, p = 0.426 |
| factors vs hgpg | 349-305, p = 0.093 | 330-269, **p = 0.014** |
| factors vs gpg | 441-348, **p = 0.0010** | 438-328, **p = 0.0001** |
| factors vs reference LGBM OOF file (428k baseline) | 276-274, p = 0.966 | 244-213, p = 0.160 |

Per-group hit % (factors arm / baseline, seeds pooled per seed):
seed 1 g1 40.2/39.9, g2 27.9/28.7, g3 20.2/19.8; seed 3 g1 40.7/38.7,
g2 28.9/28.4, g3 20.3/21.1. No group moves > ±2pp; seed 3's +0.57pp comes
mostly from g1 (+2.0pp), partly offset by g3 (−0.8pp).

Era subsets (factors arm vs baseline): seed 1 2023-24+ 585/2001 = 29.235%
vs 30.185%; 2025-26 191/633 = 30.174% vs 30.332%. Seed 3 2023-24+
603/2001 = 30.135% vs 29.585%; 2025-26 193/633 = 30.490% vs 30.332%.

## Verdict: REJECTED

The pre-declared rule ("promising" = ≥ +0.5pp on BOTH seeds) fails: seed 1
is a statistical tie (−0.03pp, p = 1.0) and seed 3 is +0.57pp with a loose
McNemar vs its own baseline (p = 0.43). Mixed across seeds, well inside
fold noise — same pattern as the V15 augmented-training row (+0.095 /
+0.412pp). The ensemble arm was not run (its condition — the factor LGBM
clearing the bar on both seeds — did not hold). Default remains the 2017+
no-lineup rank ensemble at 29.71% mean.

Both iterations of the embedding idea point the same way: the factors
either duplicate the existing rates (corr(e_rec_gpg, gpg) ≈ 0.67 —
LGBM already has gpg/hgpg/shots/TOI season-to-date columns) or, in the
torch variant, land orthogonal to the label. A static per-player factor
does not encode anything about finishing or aging that the frame's
prior-games columns have not already provided as rotationally-dominant
features.

## Notes for the next attempt

- If revisited, the only genuinely new twist would be an *explicit
  time-varying* aging term (e.g. factor vs career-games random walk); the
  static one is exhausted.
- No data/code outside `exp_embed.py` and this notes file was touched;
  `model.py` untouched; data/ml CSV/OOF caches left in place (gitignored).
