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

### V3f — LGBM + baseline blends on saved OOF (notes only)

Inputs: `data/ml/oof_v3e_lgbm_seed{1,2,3}.csv` (3,159 picks/seed). Fixed,
pre-declared weights only; rank/z transforms within `(date,tims)`.

| variant | seed 1 | seed 2 | seed 3 | mean ± std | McNemar vs lgbm (pooled) |
|---|---:|---:|---:|---:|---|
| lgbm alone | 30.294% | 30.073% | 29.566% | 29.978 ± 0.373 | — |
| rank 0.75 lgbm + 0.25 hgpg | 29.978% | 29.756% | 29.788% | 29.841 ± 0.120 | 173-186, p=0.527 |
| rank 0.50 lgbm + 0.50 hgpg | 29.630% | 29.471% | 29.218% | 29.440 ± 0.208 | 485-536, p=0.118 |
| z equal lgbm+hgpg | 29.693% | 29.376% | 29.376% | 29.482 ± 0.183 | 541-588, p=0.171 |
| z 0.75 lgbm + 0.25 hgpg | 29.471% | 29.915% | 29.693% | 29.693 ± 0.222 | 269-296, p=0.274 |
| rank 0.75 lgbm + 0.25 gpg | 29.250% | 29.250% | 29.376% | 29.292 ± 0.073 | 329-394, p=0.017 (worse) |

McNemar vs hgpg pooled over all three runs: lgbm 1025-842, p=2.5e-05;
rank75_hgpg 917-747, p=3.4e-05; rank50_hgpg 612-480, p=7.2e-05; z_eq
529-393, p=8.4e-06; z75_hgpg 802-646, p=4.6e-05; rank75_gpg 996-878,
p=0.0069.

Verdict: **no blend qualified**. None reached 30.0% in all three seeds (the
best, rank75_hgpg, was 29.76-29.98%), and none beat lgbm alone consistently —
rank75_hgpg won only seed 3, the others lost every seed; pooled McNemar vs
lgbm was p>=0.12 for all hgpg blends. No `--blend` flag added; notes only.

### V4 — raw shot / TOI / shooting-talent block (winning feature set)

Environment check first: `Player-/Team-/Goalie-Snapshots-backtrack-prod` all
still return **0 rows and no columns** (exact count queries, 2026-10-19), so
the work continued on `--env dev` with the existing `data/ml/*.csv` caches.
Dev row counts unchanged (427,240 / 23,740 / 25,326).

Hypothesis: `gpg` is a season-to-date **goal** rate, and goals are the noisiest
possible talent estimate (~15% of all rows, ~27%/16%/9% by tims group). The
Supabase snapshots carry no shots or ice time, but `player_games` in
`raw_nhl.sqlite` has the boxscore line per player per game. New block, all
aggregates over a player's **strictly earlier** games (season-to-date prefix
sums minus the current row; last-5/10; EWM halflife 5; career expanding):

- `sd_shots_pg`, `sd_goals_pg`, `sd_toi_pg`, `sd_sh_pct`
- `r5_shots_pg`, `r5_goals_pg`, `r10_shots_pg`, `r10_goals_pg`, `r10_toi_pg`
- `ewm_goals`, `ewm_shots`, `career_games`, `career_goals_pg`,
  `career_shots_pg`

Leakage checks: spot-checked a player-date against a manual prior-games query
(`sd_shots_pg` 1.80, `sd_goals_pg` 0.125, `r10_shots_pg` 1.90 — exact match);
the current game's line is excluded; 418,885 / 427,240 rows join a shot block
(the rest are first NHL games). No row-count change and no suffixed columns.

Each extra feature group was added to the V2 set on its own (HGB, date-blocked
k=5, train=all, **seed 0** fold assignment, 3,159 picks). Seed 0 V2 baseline
reproduced at 916 hits / 28.997%.

| added block | hits / 3159 | acc |
|---|---:|---:|
| none (V2 baseline) | 916 | 28.997% |
| opponent team rates + expected-goals interactions | 909 | 28.775% |
| own/opp goalie relative strength | 917 | 29.028% |
| schedule density / back-to-backs | 887 | 28.079% |
| calendar (month, day-of-week, season-day, playoffs) | 896 | 28.363% |
| recent-label form (prev5/10, EWM) | 913 | 28.902% |
| **shot / TOI block** | **961** | **30.421%** |

Stacking other blocks onto the shot block did not improve on it:
+goalie 929 (29.408%), +opponent team 962 (30.453%), +recent form 938
(29.693%), +calendar 953 (30.168%), +schedule 963 (30.484%), +opponent team
& schedule 939 (29.725%), +opp+schedule+recent 959 (30.358%),
+opp+schedule+calendar 954 (30.199%). All within ~1pp pick noise; the single
shot block is the simplest and already at the top.

Leave-one-subgroup-out from the shot block (same seed-0 protocol), showing
all four subgroups carry signal: −season-to-date 926 (29.313%), −rolling 931
(29.471%), −EWM 921 (29.155%), −career 914 (28.933%).

Monotonic constraints (domain-sign on gpg/hgpg/ppg/shots/opp_gaa/
opp_save_pct, HGB native `monotonic_cst`) **hurt**: shot block 924
(29.250%), shot+schedule 937 (29.661%) vs 961/963 unconstrained. Rejected.

End-to-end default run (`--model hgb --seed 42`, the current CLI defaults;
3m16s): **950 / 3159 = 30.073%**, per-fold 30.8/33.3/26.7/30.2/29.4
(mean 30.07 ± 2.40), held-out AUC 0.694. Per-group hit rates g1/g2/g3 =
40.9/27.9/21.4 vs V2 38.9/27.2/20.9 — the gain is spread across all three
groups, not one lucky slate. Same-run baselines: hgpg 28.047%
(886), gpg 26.496% (837), logreg 28.965% (915). Exact McNemar on pooled picks:
**HGB vs hgpg 406-342, p = 0.0212; HGB vs gpg 468-355, p = 9.2e-05** — both
baseline gaps are significant at p < 0.05 in this run.

Seed robustness (`--seed 0` and the default `--seed 42`, same config):

| seed | hits / 3159 | acc | per-fold std |
|---|---:|---:|---:|
| 0 | 961 | 30.421% | 2.54* |
| 42 (default) | 950 | 30.073% | 2.40 |

\* seed-0 per-fold figures come from the experiment harness, which uses the
same fold shuffle as the CLI `--seed 0`. A wider seed sweep was abandoned:
other swarm jobs saturated the 6-core box and one fold took 30+ minutes, so
only the two completed seeds are reported rather than a starved run.

Secondary chronological split (`--split seasons`, train < 2024-08-01, test
2024-25 + 2025-26, 1,305 picks, one fit): HGB **390 = 29.885%**, hgpg
**396 = 30.345%**, gpg 376 = 28.812%, live 376/1288 = 29.193%. McNemar
HGB vs hgpg 153-159 (p = 0.78), HGB vs gpg 190-176 (p = 0.50) — statistically
a tie, with HGB a hair below hgpg. The shot block therefore does **not** beat
hgpg on the deployment-shaped split (same pattern V3e found for LGBM); the
>30% headline is a date-blocked k-fold result, not a chronological one. The
k-fold edge over both baselines is significant, but do not claim it transfers
across eras.

Verdict: **promoted**. The shot/TOI block is now part of `NUMERIC_FEATURES`;
`--model hgb --seed 42` remains the default and clears 30.0% pooled OOF.

### V3g — seed bagging and LGBM config sweep

Housekeeping: the dangling V4 changes were a complete shot/TOI feature block
(from `player_games`: `sd_*`, `r5_*`/`r10_*`, `ewm_*`, `career_*`, 14
features added to `NUMERIC_FEATURES`) plus V4 notes. Committed them to branch
`v4-shot-features` (`08e92e7`); concurrently another worker landed V4 on
`backtrack-recon` with follow-up commits through `beaee74`, so V4 is now the
current codebase and nothing is dangling. `data/ml/` remains an untracked
cache.

Seed bagging (saved OOFs): z-score each seed's `lgbm` within `(date, tims)`,
average the three, one pick pass. **945 / 3159 = 29.915%**; per-fold std
2.932 using seed-1 fold labels (1.495/1.563 under seed-2/3 labels, which are
an arbitrary partition for a bag); McNemar vs hgpg **328-269, p=0.0175**.
Bagging does not beat lgbm-alone mean (29.978%).

New `--lgbm-config {a,b,c}` (a=400/0.03/31 default; b=600/0.02/15/reg5;
c=800/0.02/63). Seeds 1 and 3, date-blocked k=5, train=all, 200s cap each.
These runs used the pre-V4 V2 feature worktree so they match the V3e
references; re-run on the now-current V4 features before promoting a config.

| config | seed 1 | seed 3 |
|---|---:|---:|
| a (reference) | 30.294% (957) | 29.566% (934) |
| b | timeout at 200s | timeout at 200s |
| c | 29.725% (939) | timeout at 200s (2/5 folds done) |

The b timeouts completed no folds; c seed 1 finished in 190s but is -0.569pp
vs a, so it cannot meet the +0.5pp-on-both-seeds bar. The other heavy jobs on
the host (V4 final HGB, audit LGBM, V3h) are the likely cause. No config
qualified, so seed 2 was not run and the default stays **a / hgb**.

### V4 results — clean LGBM check (2026-10-19)

V4 = 14 prior-games-only shot/TOI/talent features (`sd_*`, `r5_*`/`r10_*`,
`ewm_*`, `career_*`) from `player_games`; carried by `08e92e7` on branch
`v4-shot-features` and merged/followed up on `backtrack-recon` through
`beaee74`.

LGBM, date-blocked k=5, train=all, `OMP_NUM_THREADS=2`, `LGBM_N_JOBS=2`:
each seed completed in 100s with `--seed` 1/3.

| seed | V3e pre-V4 | V4 | delta | McNemar V4 vs V3e |
|---|---:|---:|---:|---|
| 1 | 957/3159 = 30.294% | 959/3159 = 30.358% | +0.063pp | 220-218, p=0.962 |
| 3 | 934/3159 = 29.566% | 926/3159 = 29.313% | -0.253pp | 207-215, p=0.733 |
| pooled | — | — | — | 427-433, p=0.865 |

AUC rose slightly (0.6964/0.6968 vs 0.6946/0.6944), but picks did not: seed 1
was positive by 0.06pp, seed 3 negative. Per the stop rule, seed 2 and HGB
seed 0 were **not** run.

Fold-1 LGBM gain importance for the shot block (rank among 47 features):
career_goals_pg #2 (151,047), ewm_shots #4, sd_shots_pg #5, r10_toi_pg #6,
sd_toi_pg #7, career_shots_pg #9, career_games #14, ewm_goals #18, sd_sh_pct
#20, r10_shots_pg #21, sd_goals_pg #24, r5_shots_pg #31, r10_goals_pg #39,
r5_goals_pg #40. Permutation AUC drops were small: career_goals_pg 0.0089,
ewm_shots 0.0020, all others <=0.0015.

Existing V4 accuracy runs: HGB seed 42 = 950/3159 30.073%
(`/tmp/opencode/v4_final.log`), seasons 390/1305 29.885%; the HGB seed-0 run
is incomplete (2/5 folds). Verdict: V4 is heavily used but does not improve
LGBM and shows no cross-family gain; its 30.4% seed-0 headline is still an
unverified harness number. Keep the code, but do not rely on the V4 promotion
until a clean HGB seed-0 run is completed.

### V5 — pick voting and recency-weighted LGBM (notes only)

**A. Pick voting.** Seed-0 files (`oof_v2_baseline_repro.csv`,
`oof_v3d_lgbm.csv`, `oof_v3d_et.csv`) share identical fold columns across all
92,440 rows, so A1 comparisons are paired.

- A1 majority vote over hgb+logreg+lgbm+extratrees+live (tie-break mean rank):
  **929/3159 = 29.408%**, per-fold std 1.562, folds 31.1/28.6/29.9/30.3/27.1.
  vs hgpg 249-206, p=0.049; vs lgbm-alone 148-178, p=0.108. LGBM alone
  remains higher at 959/3159 = 30.358%.
- A2 vote over the three LGBM seeds 1-3: **938/3159 = 29.693%**, std 2.865
  using seed-1 fold labels, folds 33.3/29.4/26.4/31.7/27.6. vs hgpg 331-279,
  p=0.039. vs lgbm seed 1/2/3: 88-107 p=0.197, 68-80 p=0.366, 101-97 p=0.831.
  The three seeds use different fold partitions, so A2's vote-vs-lgbm
  comparisons are not paired; only the hgpg comparison is cleanly paired.

**B. Recency-weighted LGBM** (V4 features, seeds 1/3, `OMP_NUM_THREADS=2`,
`LGBM_N_JOBS=2`, 89-103s each):

| seed | weight | hits / 3159 | acc | per-fold std | Δ vs unweighted | McNemar |
|---|---|---:|---:|---:|---:|---|
| 1 | none | 959 | 30.358% | 3.101 | — | — |
| 1 | linear | 944 | 29.883% | 2.398 | -0.475pp | 135-150, p=0.407 |
| 1 | half | 934 | 29.566% | 2.983 | -0.791pp | 224-249, p=0.270 |
| 3 | none | 926 | 29.313% | 1.178 | — | — |
| 3 | linear | 954 | 30.199% | 0.948 | +0.886pp | 150-122, p=0.101 |
| 3 | half | 917 | 29.028% | 2.115 | -0.285pp | 221-230, p=0.706 |

Verdict: **no variant won consistently**. Linear is mixed (loses seed 1,
wins seed 3), half loses both. The `--recency-weight` code was reverted;
default remains `none`. Pick voting also lost to LGBM alone. No code changes
committed.

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
6. **Shot volume was the only new block that mattered.** Goals are a noisy
   talent signal; prior-games shots/TOI from the raw boxscore store moved
   HGB from 29.0% to 30.1-30.4% and beat both baselines (k-fold). Calendar,
   schedule density, goalie-relative and opponent-team blocks were flat or
   negative on top of V2/V3. Monotonic constraints hurt.

## Next steps

- V4 open question: LGBM on the V4 set is neutral (seeds 1/3: +0.06/-0.25pp
  vs pre-V4). Run HGB seed 0 cleanly over 5 folds to verify the 30.421%
  harness headline; if it does not hold, reconsider the V4 promotion.
- V4 HGB is cleanly verified only at seed 42 (950/3159 = 30.073%); seeds
  1/2/3 remain TBD. Do not cite the seed-0 961/3159 until it is rerun.
- Ablate the weak categoricals (lineup_unit, pp_unit) from the V4 set.
- Group 3 remains the weak spot (V4 21.4% vs g1 40.9 / g2 27.9); per-group
  calibration or group-specific interactions are the obvious next lever.
- LGBM: V3e seeds 1-3 mean 29.98% (seed 3 29.57), k-fold edge over hgpg is
  consistent (pooled p=2.5e-05) but it loses to hgpg on the seasons split.
  Default stays hgb. V3f fixed LGBM+hgpg/gpg blends did not beat LGBM alone.
- V3b/V4 calendar/schedule/goalie features (day_of_season, month,
  rel_goalie_strength, schedule_density) did not help; do not re-add.
- Every new feature must be justified as pre-game from the joins; if it
  needs labels from other rows, only shifted-per-player ones qualify.
- Update this file after every experiment: config used, split mode, pooled
  OOF hits/total, per-headline McNemar vs hgpg AND gpg.
