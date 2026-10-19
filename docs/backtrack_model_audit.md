# Backtrack model evaluation audit

Date: 2026-10-19 · Worktree `/home/nathan/code/SmartScore-audit` · branch `audit-eval`
(base `backtrack-recon` @ 758d275). Audit scripts: `docs/backtrack_audit.py` (standalone
baselines + fold check) and `docs/backtrack_ablation.py` (lineup ablation).

## Bottom line

The harness arithmetic is sound: no date overlap, no label leakage, exact 3×dates
denominator, and the published 959/3159 reproduces exactly. **But the feature set is not
pre-game**: `lineup_unit` and `pp_unit` are reconstructed from the *same game's* shift
charts (`lineup_status=ACTUAL`). Removing them drops LGBM from 30.358% to **28.237%
(892/3159), statistically tied with hgpg (28.047%, McNemar p=0.83)**. The +2.3pp
"edge" is attributable to same-game information and is not deployable as-is.

- 30.358% is a real *out-of-fold* number **for this feature set**.
- It is **not** a valid pre-game/deployment held-out estimate.
- The two same-game columns account for the entire LGBM edge over hgpg (with vs
  without lineups: 266–199 discordant, exact p=0.0022).

## Check verdicts

| # | Check | Verdict |
|---|---|---|
| 1a | Features use only current date or earlier (pre-game) | **FAIL** — `lineup_unit`, `pp_unit` are same-game actuals; goalie identity uses actual appearances |
| 1b | `scored` never a feature; rows aligned to player/date | **PASS** |
| 1c | One pick per (date, tims 1/2/3); denominator 3×dates | **PASS** |
| 1d | Folds split by date; zero train/holdout date overlap | **PASS** |
| 1e | NaN score cannot silently pick the first row | **PASS WITH CAVEAT** (latent: call sites fill NaN before the guard) |
| 1f | Season dtypes / "New York" ambiguity handled | **PASS** |
| 2 | hgpg 28.05% (886/3159), gpg 26.50% (837/3159) | **PASS**, exact |
| 3 | LGBM 959/3159 = 30.358% | **PASS**, exact (fresh run + saved OOF) |

Overall: **FAIL as a pre-game model evaluation** (1a). Harness mechanics: **PASS**.

## Evidence

### 1a. Pre-game features — one family fails

Independently recomputed every rate from `data/raw_nhl.sqlite` box scores using windows
strictly before each row's game; all match the cached CSVs exactly (0 mismatches over
427,240 rows / 23,740 team-rows / 25,326 goalie-appearances):

- `gpg`, `five_gpg`, `ppg`, `hgpg`, `hppg`: max abs diff ≤ 5e-16.
- `tgpg`, `otga`, `otshga`: max abs diff ≤ 5e-15.
- opponent `gaa`, `save_pct`, `record`: exact (947 no-prior-game rows NULL as intended).
- `prev2_goals`, `prev3_goals`, `games_since_goal`, `days_since_game`, `club_rest_days`,
  `opp_rest_days`, `rank_*`: exact recomputation; all strictly prior/current-day only.

`lineup_unit` / `pp_unit` are the material exception. `player_lineups` is built from
the same game's shift charts and published with `lineup_status=ACTUAL` (425,140 of
427,240 rows; 2,100 null). `local_store.publish` writes `l.lineup_unit, l.pp_unit` into
`Player-Snapshots-backtrack-*`. These values are only knowable after the game; they are
not a morning projection. `lineup_position_group` (F/D) is roster classification and
`tims` is pre-game.

Secondary lookahead: the opponent-goalie block is deduped from *actual appearances*
(`games_played`), so the identity of the goalie is post-game even though every rate
value is prior. 1,584 of 23,740 team-dates have two appearances; single-appearance
dates use whoever actually played rather than a morning projection. The values are
pre-game; only the choice of goalie benefits from knowing who played. Not quantified
separately.

`lineup_unit` was also the top permutation-importance feature in the V3b HGB model
(AUC drop .042 vs hgpg .017), consistent with the ablation below. The `model.py`
docstring claim that "every feature column is a pre-game value by construction" is false.

### 1b. Label and alignment

- `DATASET_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES`; no `scored`, no IDs,
  names, or team names.
- `scored` equals `(raw box score goals > 0)` on all 427,240 player-game rows.
- Fresh OOF rows join back to the challenge CSV on `(date, player_id, tims)` with
  92,440/92,440 matches and 0 `scored` / `gpg` / `hgpg` mismatches.
- 0 duplicate `(player_id, date)` rows; row count unchanged by `add_derived` (427,240).

### 1c. Picks and denominator

92,440 challenge rows, 1,053 challenge dates, 3,159 `(date, tims)` groups
(3 × 1,053), group sizes 4–92, full grid. Every date has exactly 3 picks for gpg, hgpg
and LGBM; no all-NaN groups.

### 1d. Folds

`fold_blocks`: sort unique challenge dates → `default_rng(seed).shuffle` →
`array_split`. Recomputing seed 0 reproduces the OOF `fold` column exactly (5/5).
Holdout sizes 211/211/211/210/210; every date is held out exactly once; training dates
are the complement (842/842/842/843/843) and the intersection is 0 in every fold.

### 1e. NaN handling

`daily_picks` filters `notna()` and skips an all-NaN group, so it cannot pick a NaN row.
However `run_folds` pre-fills baselines with `-inf` and model scores with `0`, which
defeats that guard: a synthetic all-NaN group yields 0 picks, but after
`fillna(-inf)` it yields the first row. Not triggered here — 0 all-NaN groups, LGBM
scores span 0.015–0.580 with no tied maxima — but it is a latent first-row fallback.

### 1f. Season dtype / New York

- `season` cast to `Int64` → string on all frames: dtype `str`, 0 `<NA>`, no `_x`/`_y`
  suffix columns.
- 27,066 rows carry `team_name="New York"`; the raw `player_games` join resolves them
  to NYI 13,643 / NYR 13,423, with 408 dates where both clubs play. 0 unresolved clubs;
  no join uses `team_name` for identity (only `club_map` from the raw store does).

## Reproduced numbers

| method | hits / 3159 | acc | per-fold mean ± std | AUC |
|---|---:|---:|---:|---:|
| gpg | 837 | 26.496% | 26.496 ± 1.163 | — |
| hgpg | 886 | 28.047% | 28.046 ± 1.631 | — |
| LGBM (all features) | **959** | **30.358%** | 30.356 ± 1.719 | 0.6947 |
| logreg (numeric only) | 917 | 29.028% | 29.029 ± 1.252 | — |
| LGBM minus `lineup_unit`/`pp_unit` | **892** | **28.237%** | — | 0.6878 |

Fresh run (same command as the claim, plus `--model-file`; `--no-sync` only avoids
mutating the shared venv), exit 0 in ~4 minutes under `timeout 420`; log at
`data/ml/audit_lgbm.log`. Per-fold LGBM: 31.6 / 29.4 / 31.4 / 31.6 / 27.8%. The saved
`data/ml/oof_v3d_lgbm.csv` independently re-picks to the same 959/3159.

McNemar exact (pooled picks, same 3,159 groups; first number = first method's wins,
second = second method's wins):

- LGBM vs hgpg: 354–281, p=0.0042 (reproduces notes).
- LGBM vs gpg: 442–320, p<1e-4.
- LGBM without lineups vs hgpg: 261–255, **p=0.83 (tie)**.
- LGBM with vs without lineups: 266–199, p=0.0022.

## Why ~30% is optimistic

1. **Same-game feature leakage is worth the whole edge.** The backtrack has no historical
   *projected* lineups, so the ACTUAL shift-chart unit labels are lookahead. The
   no-lineup variant (keeping F/D and `tims`; the smaller goalie-identity lookahead
   remains) scores 28.24%, tied with hgpg. Morning projections might recover part of the
   gap, but that has not been measured; a truly pre-game variant is ≤28.24%.
2. **Random-date CV, not forward time.** K-fold shuffles dates across 2017–2026; the
   deployment-shaped `--split seasons` result in MODEL_NOTES is LGBM 30.19% vs hgpg
   30.35% (no edge).
3. **Seed/config selection.** 30.358% is seed 0, the highest of seeds 0–3
   (30.294 / 30.073 / 29.566; mean 29.978 ± 0.373). Many model families and feature sets
   were tried on the same dates; there is no untouched final test.
4. **Sampling noise.** Binomial SE at 959/3159 is ~0.82pp (95% CI ≈ 28.8–32.0).
5. **Minor coverage bug (not leakage).** `sort_values("games_played")` puts NaN last and
   `drop_duplicates(keep="last")` then keeps the debut goalie over an experienced one:
   161 team-dates (3,199 challenge rows, 3.46%) lose opponent-goalie features they could
   have had. This only makes the model's inputs poorer, not leakier.

## Reproduce

```bash
cd /home/nathan/code/SmartScore-audit
OMP_NUM_THREADS=2 uv run --no-sync python docs/backtrack_audit.py \
  --oof /tmp/opencode/audit_lgbm_oof.csv --seed 0

OMP_NUM_THREADS=2 timeout 420 uv run --no-sync python \
  smartscore/scripts/backtrack/model.py --env dev --split kfold --splits 5 \
  --train-subset all --model lgbm --seed 0 --model-file /tmp/opencode/audit_lgbm_oof.csv

OMP_NUM_THREADS=2 uv run --no-sync python docs/backtrack_ablation.py
```
