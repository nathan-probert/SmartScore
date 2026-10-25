# Backtrack model evaluation audit

> **Post-V6 addendum (current branch):** commit 8b077e7 removes `lineup_unit`/`pp_unit`.
> The V6 section at the end supersedes the pre-V6 verdicts below for the current code.

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
pre-game; only the choice of goalie benefits from knowing who played. Quantified in the
follow-up below: removing the block does not hurt, so this lookahead is immaterial.

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

## Follow-up: opponent-goalie identity ablation

Same LGBM k-fold (seed 0, k=5, train=all, `timeout 300`), dropping `lineup_unit`,
`pp_unit` **and** the seven `opp_*` goalie features (`opp_gaa`, `opp_save_pct`,
`opp_shutouts`, `opp_games_played`, `opp_wins`, `opp_losses`, `opp_otl`).
`lineup_position_group` / `tims` stay; `opp_rest_days` stays (team rest, pre-game).

| variant | hits / 3159 | acc | AUC |
|---|---:|---:|---:|
| full LGBM | 959 | 30.358% | 0.6947 |
| no lineup | 892 | 28.237% | 0.6878 |
| no lineup + no opp goalie | **902** | **28.553%** | 0.6869 |
| hgpg | 886 | 28.047% | — |

McNemar exact (wins–losses, same 3,159 groups):

- no-goalie vs hgpg: 286–270, **p=0.525 (tie)**.
- no-goalie vs no-lineup: 179–169, **p=0.630 (tie)**.
- no-goalie vs full LGBM: 232–289, p=0.014 (lineup model still better).

Removing the opponent-goalie block does not hurt (it slightly helps, within noise), so
the actual-appearance identity lookahead is not inflating the headline. No proxy is
needed for this evaluation. For a strictly pre-game deployment, replace it with the
morning projected starter (or, without projections, the previous game's starter /
most starts as-of the prior date) and attach that goalie's prior rates. The lineup
leak remains the material one.

## Reproduce

```bash
cd /home/nathan/code/SmartScore-audit
OMP_NUM_THREADS=2 uv run --no-sync python docs/backtrack_audit.py \
  --oof /tmp/opencode/audit_lgbm_oof.csv --seed 0

OMP_NUM_THREADS=2 timeout 420 uv run --no-sync python \
  smartscore/scripts/backtrack/model.py --env dev --split kfold --splits 5 \
  --train-subset all --model lgbm --seed 0 --model-file /tmp/opencode/audit_lgbm_oof.csv

OMP_NUM_THREADS=2 uv run --no-sync python docs/backtrack_ablation.py
OMP_NUM_THREADS=2 timeout 300 uv run --no-sync python docs/backtrack_ablation.py --drop-goalie
```

## V6 corrected pipeline (8b077e7)

Rebased `audit-eval` onto `backtrack-recon` @ 8b077e7. `DATASET_FEATURES` is now 42
features; `lineup_unit` and `pp_unit` are absent, `lineup_position_group` and `tims`
remain categoricals. The earlier 892/902 ablations predate V4's shot block: that was a
28-feature set and is not comparable to the 42-feature V6 baseline.

### Feature date basis

| feature(s) | basis |
|---|---|
| `gpg`, `ppg`, `five_gpg` | prior-only; current-season games strictly before the row (`five_gpg` last ≤5) |
| `hgpg`, `hppg` | prior-only; current season before + two complete prior seasons |
| `tgpg`, `otga`, `otshga` | prior-only; team/opponent games strictly before |
| `home` | current-date pre-game venue flag |
| `opp_gaa`, `opp_save_pct`, `opp_shutouts`, `opp_games_played`, `opp_wins`, `opp_losses`, `opp_otl` | prior-only values (chosen goalie's season-to-date, strictly before); row chosen from actual appearances (caveat below) |
| `rank_gpg`, `rank_hgpg`, `rank_five_gpg`, `rank_ppg` | current-date pre-game; percentile within `(date, tims)` of that date's pre-game rates |
| `prev2_goals`, `prev3_goals`, `games_since_goal` | prior-only labels |
| `days_since_game` | prior-only |
| `club_rest_days`, `opp_rest_days` | prior-only club appearance dates |
| `sd_shots_pg`, `sd_goals_pg`, `sd_toi_pg`, `sd_sh_pct` | prior-only; current season before |
| `r5_shots_pg`, `r5_goals_pg`, `r10_shots_pg`, `r10_goals_pg`, `r10_toi_pg` | prior-only; last 5/10 games |
| `ewm_goals`, `ewm_shots` | prior-only; EWM halflife 5 |
| `career_games`, `career_goals_pg`, `career_shots_pg` | prior-only; all prior games |
| `lineup_position_group` | static; F/D from box-score position (pre-game roster role) |
| `tims` | current-date pre-game challenge group |

Verification: all 14 shot features recomputed independently from `player_games` with
shift/prefix windows — 0 mismatches over 427,240 rows (max abs diff 0). `lineup_position_group`
matches the raw box-score F/D mapping on 425,140 rows, 0 mismatches (2,100 nulls);
it is not shift-derived. `opp_*` rate values recompute exactly from the goalie's prior
appearances (0 mismatches); the only residual same-game dependence is *which* actual
goalie row is selected (1,584 team-dates have two appearances). The post-audit ablation
showed removing that block does not hurt (892 → 902, p=0.63), so it does not inflate the
headline.

**Verdict: PASS on feature values** — no remaining feature contains same-game events,
labels, or current-game rates. One immaterial caveat: opponent-goalie identity is chosen
from actual appearances, not a morning projection.

### Corrected LGBM reproduction

Fresh run: `--model lgbm --seed 3`, k=5, train=all, `OMP_NUM_THREADS=2`, `timeout 420`
→ exit 0 in ~75s. **916/3159 = 28.997%**, per-fold 29.1/28.3/29.5/27.3/30.8%, AUC
0.6922. The OOF is bit-identical to the saved `data/ml/oof_v6_baseline_seed3.csv`
(lgbm max abs diff 0.0; identical folds). Fold check: 1,053 dates held out exactly once,
seed-3 blocks reproduced, 0 train/holdout overlap.

Saved files:

| file | feature set | seed 3 hits / acc |
|---|---|---:|
| `oof_v6_baseline_seed{1,2,3}.csv` | current V6 corrected set, 42 features | 916 / 28.997% |
| `oof_v6_lgbm_seed{1,2,3}.csv` | corrected + six lagged line/PP features, 48 features | 910 / 28.807% |

The `*_lgbm_*` files use the temporary lagged block (`last_lineup_unit`,
`last_pp_unit`, `mode_lineup_unit_5`, `mode_pp_unit_5`, `line_stability_5`,
`pp_share_5`); it was reverted and is not reproducible from current `model.py`.
Three-seed means: corrected **29.081%** (928/912/916), lagged **29.176%**
(929/926/910).

McNemar vs hgpg (corrected baseline):

| seed | corrected wins–losses | p |
|---|---|---:|
| 1 | 341–299 | 0.105 |
| 2 | 322–296 | 0.315 |
| 3 | 325–295 | 0.244 |
| pooled | **988–890** | **0.0252** |

Caveat: the pooled test treats three runs over the same 3,159 groups as independent;
they are not (seeds reuse the same outcomes/slates), so the pooled p overstates the
evidence. Note: the `MODEL_NOTES.md` McNemar line (336–293 / 331–291 / 319–295; pooled
986–879, p=0.0141) matches the **lagged** files, not the corrected baseline — the
corrected baseline is 988–890, p=0.0252.

**Final verdict: PASS for the leakage correction; corrected LGBM 916/3159 = 28.997%,
pooled McNemar vs hgpg 988–890 (p=0.025), but per-seed p=0.10/0.31/0.24 and the seeds
reuse the same groups — the ~+1.0pp edge is weak/suggestive, not established.**
