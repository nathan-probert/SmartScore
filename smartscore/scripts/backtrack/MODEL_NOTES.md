# SmartScore Model Notes

Working log for the goal-probability model built on the backtracked archive.
Purpose: record what was tried, what worked, what broke, and the data gotchas,
so nothing gets re-derived twice. Update it every session.

## Source of truth facts (do not re-discover)

- Backtracked data lives in the **dev** Supabase tables (`ENV` in `.env` is
  `prod`, but the backtrack publish shipped to dev):
  - `Player-Snapshots-backtrack-dev` — 427,240 rows, 2017-10-04 .. 2026-06-14.
  - `Team-Stats-backtrack-dev` — 23,740 rows (per team per game).
  - `Goalie-Snapshots-backtrack-dev` — 25,326 rows (per goalie appearance).
- `Player-Snapshots-backtrack-prod` is EMPTY. Add `--env dev` or the script
  fails fast with "table is empty".
- Table download takes ~2.5 min; cached to `data/ml/*.csv` (see fetch_table).
- `data/raw_nhl.sqlite` (1.7 GB) is the raw layer the migrations call the
  source of truth. Supabase rates alone are NOT recomputable.
- `tims` column: 1/2/3 = challenge pick group that date; 0 = not in the
  challenge (334,800 rows; 92,440 rows are in groups). Sim rows = tims 1/2/3.
- Label: `scored` (100% filled on backtrack rows). Positives ~15% of rows.

## Gotchas that already cost time (do not repeat)

1. **"New York" is ambiguous.** Both clubs publish `team_name="New York"` in
   Supabase. A name join leaves 14,685 rows (3.4%) unresolvable. The club per
   player per game must come from `player_games` in raw_nhl.sqlite (it has
   `team_abbrev`/`opponent_abbrev` per `player_id` per `game_date`).
2. **CSV round-trip types.** `pd.read_csv` parses season `20232024` as int64
   while the DB carries TEXT (and `player_games.season` is INTEGER). Cast
   season to string on every frame before any merge.
3. **`live_score` mask bug** (fixed 2026-10-18): passing a train boolean mask
   onto the *test* frame selects nothing via `.loc` index alignment → all-zero
   scores → `idxmax` picks the first row per group → fake ~16% accuracy for
   the live weights. Rule: the min/max function takes the train frame itself.
4. **`idxmax` on all-NaN groups raises.** `daily_picks` must filter NaN scores
   first (team-debut rows have no tgpt/otga; live scores inherit those NaNs).
5. **`TEST_START & frame["scored"]` precedence** bit once — always
   parenthesise `(frame["date"] < TEST_START) & (...)`.
6. The Rust module (`make_predictions_rust.so`) cannot be built on this box
   (no `cc` linker). If the live evaluator is needed, replicate
   `predictions.rs` in Python: it is literal - min-max on the hardcoded
   `service.get_min_max()` values, then a linear weighted sum.
   The hardcoded MinMax is: gpg/hgpg/five_gpg max 2.0, tgpt/otga max 4.0,
   otshga 1.12, hppg 0.314, min 0 everywhere.
7. Train-era extrema have outliers (train tgpt max 10.0, hgpg max 3.0, gpg
   max 4.0) — because the archived era is 9 seasons. Live scorer avoids this
   by hardcoding. Any min-max variant must state which era it normalises on.
8. The scorer pipeline in the repo (`calculate_accuracy.py`) reads the LIVE
   archive `Player-Snapshots-prod` (143,263 rows), evaluates dates >=
   2025-01-25 only, and summarises per-dates per-group accuracy.

## Evaluation protocol (agreed)

- The only metric that counts: per game date, one pick from each of tims
  groups 1/2/3 (argmax of the method's score), hit iff `scored=1`.
- **k-fold over DATES is the standard** (not rows): shuffle the challenge
  dates, cut into k=5 blocks, train on the rest, simulate on the block.
  Report per-fold accuracy (mean ± std), pooled out-of-fold hits, and a
  McNemar exact test on disagreeing picks vs the hgpg baseline.
- `--split seasons` (train < 2024-08-01, test 2024-25+25-26) is the
  deployment-shaped backtest, but optimistic as the headline: the model makes
  up ~2.4pp of "edge" training on earlier eras there.

## Results log

### V1 — 16 baseline features (rates, opp goalie, lineup), HGB

Config: lr=0.03, max_iter=1200, 31 leaves, l2=1.0 (5-config sweep, all
differences ~1pp = pick noise). Features: gpg, five_gpg, ppg, hgpg, hppg,
tgpt, otga, otshga, home, opp goalie block (gaa, save_pct, shutouts, gp,
W/L/OTL), categorical lineup_unit / lineup_position_group / pp_unit / tims.

| split | method | acc | n picks |
|---|---|---:|---:|
| seasons (chronological) | gpg | 28.8% | 1305 |
| seasons | hgpg | 30.3% | 1305 |
| seasons | live weights | 30.2% | 1288 |
| seasons | HGB | 31.2% | 1305 |
| kfold k=5 pooled | gpg | 26.5% | 3159 |
| kfold pooled | hgpg | 28.0% | 3159 |
| kfold pooled | live weights | 28.3% | 3124 |
| kfold pooled | HGB | 26.8% | 3159 |
| kfold pooled | random | 17.8% | 3159 |

- McNemar on kfold pooled: HGB vs hgpg 378-416 (p=0.19), HGB vs gpg 418-407
  (p=0.73). **No model separated.**
- Lesson: the chronological "2.4pp model edge" was era-composition optimism.
  hgpg (~28%) was the best simple rule under date-CV; and HGB trained on
  challenge rows only had issues in eras where hgpp/rank inputs are sparse.

### V2 — added relative-context features (same HGB config)

New pre-game features (all verified leak-free):
- rank_* : within-(date, tims group) percentile of gpg, hgpg, five_gpg, ppg.
- prev2_goals / prev3_goals: goals in the 2/3 games strictly before the row's
  game (shift-by-one of the scored label per player).
- games_since_goal: consecutive past games without a goal, capped 15, NaN if
  never scored in coverage (mostly depth defencemen).
- days_since_game: per player.
- club_rest_days / opp_rest_days: per club appearance dates from team table.

| split / config | method | pooled acc | hits / total |
|---|---|---:|---:|
| kfold, train=challenge | HGB | 27.35% (std 0.89) | 864 / 3159 |
| kfold, train=challenge | logreg | 28.46% | 899 / 3159 |
| kfold, train=all rows | **HGB** | **29.0%** (std 1.25) | **916 / 3159** |
| kfold, train=all rows | logreg | 29.0% | 917 / 3159 |
| kfold, train=all rows | ensemble (mean pct-rank of HGB+logreg) | 29.3% | 925 / 3159 |
| baselines (unchanged) | hgpg | 28.0% | 886 / 3159 |
| baselines | gpg | 26.5% | 837 / 3159 |

Significance (McNemar exact, pooled picks):
- **HGB(all) vs gpg: 442-363 on disagreements, p = 0.0059 — significant.**
- HGB(all) vs hgpg: 373-343, p = 0.28 — direction right, not conclusive.
- logreg(all) vs hgpg: 201-170, p = 0.12 — same story.
- Ensemble vs hgpg: 290-251, p = 0.10; ensemble vs HGB: p = 0.69 (tie).

Current headline: **V2 features + train on ALL rows, HGB or logreg ≈ 29.0%
pooled OOF; ensemble 29.3%** — clearly beats the straight-GPG rule, roughly
beats/ties hgpg and the live weights, margin short of significance vs hgpg.

### V3a — within-day ranking objective (lambdarank)

Config: `--ranker lambdarank`, LightGBM 4.7.0 `LGBMRanker`
(objective=lambdarank, 400 trees, lr=0.03, 31 leaves, min_child_samples=50,
subsample/colsample=0.8). Same V2 feature set; trained on ALL scored rows.
Rows grouped by `(date, tims)` (tims=0 forms its own per-date group), so the
model optimises the order inside each slate/group. Date-blocked kfold, k=5;
3,159 pooled picks. Code falls back to pairwise logistic on (winner, loser)
feature differences if lightgbm is not importable.

Smoke (k=2, train=all, 173s): ranker 27.03% (854/3159), HGB 28.21%,
hgpg 28.05%, gpg 26.50%.

Full k=5, train=all (426s):

| method | pooled hits / 3159 | acc | per-fold mean ± std |
|---|---:|---:|---:|
| HGB | 916 | 29.00% | 28.995 ± 1.245 |
| logreg | 917 | 29.03% | 29.029 ± 1.252 |
| **ranker (lambdarank)** | **832** | **26.34%** | **26.336 ± 1.339** |
| hgpg | 886 | 28.05% | 28.046 ± 1.631 |
| gpg | 837 | 26.50% | 26.496 ± 1.163 |

McNemar exact, pooled picks:
- ranker vs gpg: 394-399, p=0.89 (tie).
- ranker vs hgpg: 332-386, p=0.048 (**ranker worse**).
- HGB vs gpg: 442-363, p=0.0059; HGB vs hgpg: 373-343, p=0.28.

Verdict: the ranking objective **did not help** — it fell to the straight-GPG
level and lost to hgpg, while both pointwise models stayed at ~29%. Per-group
ranker hit rates (g1/g2/g3 = 36.4/24.0/18.6) trail HGB (38.9/27.2/20.9); the
likely cause is that lambdarank only sees within-slate order (small groups,
truncation at 30) and discards the global 0/1 calibration that HGB/logreg use
across all 380k training rows. Do not promote it; keep `--ranker none` as the
default.

### V3b — calendar / schedule / relative-goalie features (reverted)

Added four leak-free pre-game features to the V2 set:
- `day_of_season`: 0-based index of the date among that season's distinct
  dates (season derived from date, Aug-Jul boundary).
- `month`: 1-12 from the date.
- `rel_goalie_strength`: opponent starter's pre-game GAA minus the mean GAA
  of that date's starters (same dedup rule as `build_frame`; NaN if unknown).
- `schedule_density`: club games in the 7 days before the game
  (`date-7 <= prior game < date`), from the team table; NaN if unknown.

Leakage/merge checks: row count stayed 427,240 before/after `add_derived`;
no `_x`/`_y` suffix columns; `month` matched the parsed date; each season's
first date had `day_of_season=0`; sampled `rel_goalie_strength` and
`schedule_density` matched independent recomputation. Calendar features use
the date only, goalie means use same-date pre-game snapshots, and density
counts strictly earlier dates.

Full date-blocked k=5, train=all, 284s, 3,159 pooled picks:

| method | pooled hits / 3159 | acc | per-fold mean ± std |
|---|---:|---:|---:|
| HGB | 907 | 28.71% | 28.710 ± 1.763 |
| logreg | 906 | 28.68% | 28.680 ± 1.079 |
| ensemble | 921 | 29.16% | 29.151 ± 2.697 |
| hgpg | 886 | 28.05% | 28.046 ± 1.631 |
| gpg | 837 | 26.50% | 26.496 ± 1.163 |

Delta vs V2 (29.00 / 29.03 / 29.28): HGB **-0.29pp**, logreg **-0.35pp**,
ensemble **-0.13pp** — all within fold noise, no gain.

McNemar exact, pooled picks:
- HGB vs gpg: 423-353, p=0.013; HGB vs hgpg: 353-332, p=0.44.
- logreg vs gpg: 331-262, p=0.0052; logreg vs hgpg: 193-173, p=0.32.
- ensemble vs gpg: 377-293, p=0.0013; ensemble vs hgpg: 280-245, p=0.14.
- **V3b HGB vs V2 HGB: 202-211, p=0.69** (tie); 1,062 of 3,159 picks changed.

HGB permutation importance (fold-1 model, 3,000 held-out rows, 1 repeat,
ROC-AUC decrease), top 15: lineup_unit .0420, hgpg .0171, pp_unit .0125,
gpg .0092, hppg .0049, opp_save_pct .0042, otshga .0033, club_rest_days
.0027, otga .0019, ppg .0016, opp_wins .00082, opp_gaa .00070, tims .00070,
games_since_goal .00066, day_of_season .00053. The other new features were
negative (schedule_density -0.0008, month -0.00084, rel_goalie_strength
-0.0013).

Verdict: **reverted**. No metric improved and the new features contributed
little or negatively; keep the V2 feature set as the baseline.

### V3c — post-processing / blends on saved OOF scores (notes only)

Source: `data/ml/oof_v2_baseline_repro.csv` (92,440 OOF rows, 3,159 challenge
picks); no retraining. Pick rule unchanged: argmax within `(date, tims)`, hit
iff `scored=1`. Rank blends use the percentile rank within `(date, tims)`;
z-score blends standardise within `(date, tims)`; the logit blend uses the
HGB/logreg probabilities (ranges 0.009-0.73 and 0.003-0.97, clipped 1e-6).
Missing `live` ranks/z-scores were filled neutral (0.5 / 0); 1,228 of 92,440
rows lack `live`. No adaptive/tuned weights were used.

| variant | hits / 3159 | acc | mean ± std | Δ vs ens | McNemar vs hgpg | McNemar vs ens |
|---|---:|---:|---:|---:|---|---|
| ensemble rank hgb+logreg | 925 | 29.281% | 29.278 ± 2.386 | — | 290-251, p=0.102 | — |
| rank hgb+logreg+live | 896 | 28.363% | 28.362 ± 2.243 | -0.918 | 216-206, p=0.661 | 112-141, p=0.078 |
| rank hgb+live | 907 | 28.712% | 28.708 ± 2.723 | -0.570 | 272-251, p=0.382 | 126-144, p=0.301 |
| rank logreg+live | 882 | 27.920% | 27.920 ± 0.609 | -1.361 | 162-166, p=0.869 | 215-258, p=0.053 |
| z-score hgb+logreg | 930 | 29.440% | 29.440 ± 2.042 | +0.158 | 257-213, p=0.047 | 126-121, p=0.799 |
| z-score hgb+logreg+live | 935 | 29.598% | 29.598 ± 1.302 | +0.317 | 211-162, p=0.013 | 164-154, p=0.614 |
| logit-avg hgb+logreg | 930 | 29.440% | 29.439 ± 1.857 | +0.158 | 260-216, p=0.049 | 127-122, p=0.800 |
| rank hgb+logreg+live+gpg | 892 | 28.237% | 28.235 ± 1.711 | -1.045 | 249-243, p=0.822 | 164-197, p=0.092 |
| rank hgb+logreg+live+gpg+hgpg | 904 | 28.617% | 28.615 ± 1.596 | -0.665 | 207-189, p=0.393 | 179-200, p=0.304 |

Verdict: **no variant qualified** (needed >= +0.5pp vs ensemble and p<0.10
vs ensemble). Best was z-score hgb+logreg+live at 29.60% (+0.32pp), but its
paired McNemar vs the ensemble was p=0.61. Keep the existing ensemble; no
`--blend` option added.

### V3d — model-family diversity

Added `--model {hgb,lgbm,extratrees}` (default hgb unchanged). `lgbm` is
`LGBMClassifier` binary (400 trees, lr 0.03, 31 leaves); `extratrees` is
`ExtraTreesClassifier` (400 trees, min_samples_leaf=20, n_jobs=-1), with
categoricals as ordinal codes and numeric NaNs left native. Logreg, ranker,
`daily_picks` and `score_picks` paths are unchanged.

Full date-blocked k=5, train=all, 3,159 pooled picks. Runtime: lgbm **97s**,
extratrees **346s** (both under the 420s cap; AUC 0.6947/0.6933 vs HGB 0.6903).

| variant | hits / 3159 | acc | mean ± std | Δ vs ens | McNemar vs hgpg | McNemar vs ens |
|---|---:|---:|---:|---:|---|---|
| current ensemble rank hgb+logreg | 925 | 29.281% | 29.278 ± 2.386 | — | 290-251, p=0.102 | — |
| lgbm single | 959 | 30.358% | 30.356 ± 1.719 | +1.076 | 354-281, p=0.0042 | 237-203, p=0.116 |
| extratrees single | 938 | 29.693% | 29.691 ± 2.162 | +0.412 | 323-271, p=0.036 | 269-256, p=0.601 |
| rank hgb+logreg+lgbm | 941 | 29.788% | 29.786 ± 2.059 | +0.506 | 306-251, p=0.022 | 107-91, p=0.286 |
| rank hgb+logreg+extratrees | 931 | 29.471% | 29.470 ± 2.309 | +0.190 | 269-224, p=0.047 | 112-106, p=0.735 |
| rank all four | 946 | 29.946% | 29.944 ± 2.122 | +0.665 | 293-233, p=0.010 | 120-99, p=0.176 |
| best three (logreg+lgbm+extratrees) | 940 | 29.756% | 29.754 ± 1.966 | +0.475 | 274-220, p=0.017 | 177-162, p=0.447 |

Verdict: **defaults unchanged**. LGBM is the best single model (+1.08pp), but
its paired McNemar vs the current ensemble is p=0.116, missing the p<0.10 bar;
every ensemble also missed it. LGBM is worth a follow-up (more folds/seeds),
not a default switch yet.

### V3e — LGBM seed robustness

Added `--seed` (default 42) to control the date-shuffle in `fold_blocks`.
V2-V3d used the old hard-coded seed 0; pass `--seed 0` to reproduce those
fold assignments. LGBM, date-blocked k=5, train=all, 3,159 picks/seed:

| seed | LGBM hits / 3159 | acc | per-fold std | vs hgpg | McNemar p |
|---|---:|---:|---:|---|---:|
| 1 | 957 | 30.294% | 3.348 | +2.247pp | 349-278, p=0.0051 |
| 2 | 950 | 30.073% | 2.793 | +2.026pp | 342-278, p=0.0113 |
| 3 | 934 | 29.566% | 1.446 | +1.519pp | 334-286, p=0.059 |
| **mean (1-3)** | — | **29.978%** | seed std **0.373** | — | — |

McNemar vs hgpg pooled over the three seed runs: **1025-842, p=2.5e-05**
(n=1,867 discordant picks). Seed 0 (V3d) was 959/3159 30.358%, 354-281,
p=0.0042.

Seasons split (`--split seasons`, one fit, 1,305 picks): LGBM **394/1305
30.192%**, hgpg **396/1305 30.345%**, gpg 376/1305 28.812%. LGBM does **not**
beat hgpg on the chronological split.

Verdict: **default stays hgb**. The multi-seed mean is 29.98% (<30.0) and seed
3 dipped to 29.57%; LGBM beats hgpg consistently in k-fold (all three seeds
positive, pooled p<0.001) but loses to hgpg on the seasons split. Tune
regularisation / try an LGBM+hgpg blend before promoting it.

## Lessons learned this phase

1. **Train on ALL rows, not just challenge rows.** Challenge-only training
   (V2 27.35%) underperformed hgpg even with the new features; training on
   all scored rows (3.6x data, no selection bias toward the ~93/day
   challenge pool) recovered the top. Default `--train-subset challenge` is
   now the *worse* option — use `--train-subset all`.
2. **Rank features stabilise the model** (fold std 2.5 → 0.9) even when the
   pooled mean barely moves.
3. Do not trust within-season/dating era drift: every evaluation headline in
   this file must carry its split mode; the V1 `seasons` numbers are
   optimistic by design (the CV killed their apparent edge).
4. `groupby(...).rank(pct=True)` aligned through the index of a frame →
   missing rank rows are NaN for tims=0 (fine; HGB handles NaN).
5. Big merged-frame dirty-column risk: every merge introduces suffixed
   columns; verify the column set after each join when features move.

## Next steps

- Try pushing past ~29%: monotonic constraints on gpg/hgpg; Tuesday-style
  ablation of weak features (lineup_* and pp_unit) to see whether they add
  anything beyond hgpg. (Within-day ranking was tried in V3a: 26.3%, worse.)
- LGBM: V3e seeds 1-3 mean 29.98% (seed 3 29.57), k-fold edge over hgpg is
  consistent (pooled p=2.5e-05) but it loses to hgpg on the seasons split.
  Default stays hgb; next try regularisation or an LGBM+hgpg blend.
- V3b calendar/schedule/goalie features (day_of_season, month,
  rel_goalie_strength, schedule_density) did not help and were reverted.
- Every new feature must be justified as pre-game from the joins; if it
  needs labels from other rows, only shifted-per-player ones qualify.
- Update this file after every experiment: config used, split mode, pooled
  OOF hits/total, per-headline McNemar vs hgpg AND gpg.
