# SmartScore Model Notes

> **2026-10-27 (stage-2 exactness):** inference recomputes the rolling/career
> shot block EXACTLY from the published per-game raw columns
> (game_goals/game_shots/game_toi_min on Player-Snapshots-backtrack) — the
> stage-1 approximation is gone; only missing recent days are fetched from
> the NHL API.

## FINAL RESULT — corrected, leak-free (2026-10-19)

**Default:** fixed equal-weight rank ensemble `hgb+logreg+lgbm`
(`--model ensemble`). Mean **29.71%** pooled OOF across seeds 0/1/3
(29.92 / 29.41 / 29.82; std **0.28**), vs hgpg **28.05%** and straight GPG
**26.50%**.

**V16 (2026-10-22) — no V16 change adopted.** Six follow-up experiments
(exposure product, group-3 weights/dedicated model, PBP pass-context xG,
calibration swap, interactions, ALS latent factors) all failed the
+0.5pp-on-both-seeds adoption gate; exact rows are in the rejected master
table below. (The per-experiment `notes_v16_*.md` notebooks were deleted in
`chore/clean-tree`; full runs are preserved in git history at the
pre-cleanup commit `c82b2e7`.)
The **29.71%** mean pooled all-era result stands (seeds 0/1/3 = 29.92 / 29.41
/ 29.82 on the 96f0ff2 cache); regenerating the ensemble on the current data
cache (617,310 rows incl. the V15 2013-17 rows) gives **29.28% / 29.60%** for
seeds 1/3. gpg **837/3159 = 26.496%** and hgpg **886/3159 = 28.047%**
reproduce exactly in every V16 run, validating the fold wiring.

**≥30% is invalid:** the only ≥30% result was LGBM **30.36%** using
`lineup_unit`/`pp_unit` from the same game's shift chart. Those columns are
removed; corrected LGBM is **29.06%**.

**Tims era:** challenge starts **2021-12-09** (243 pre-2023 dates; owner’s
2023 claim is wrong). Ensemble **29.71%** all 1,053 challenge dates
(29.87% strict 2023-24+); LGBM 29.14/29.84; hgpg 28.05/28.14; GPG
26.50/27.09.

**Lineup check:** projected ensemble **29.88%** seed 1 (end-to-end) and
**29.60%** seed 3 (mean **29.74%**); oracle-lineup LGBM 4-seed mean
**30.33% ± 0.26** (seeds 0/1/2/3: 30.36/30.61/30.39/29.98). V11 sensitivity:
61.6% → 100% unit fidelity maps to 28.97/28.49 → 30.61/29.98 (seeds 1/3);
real 80-90% morning projections imply roughly **29.25-29.54%**, below 30.
Default stays no-lineup.

**Real projections (V10):** 0 `PROJECTED` rows are archived (5,155 `UNKNOWN`
rows across 17 dates, with null `lineup_unit`/`pp_unit`). Real-projection
accuracy and the live simulation cannot be measured — collect PROJECTED rows
with units forward.

**Tried and rejected — master table (V3..V16; seeds 1/3 unless stated,
pooled OOF hits/total on the 3,159 challenge picks; every McNemar is the
exact two-sided binomial on pooled paired picks). All experiment code was
deleted in `chore/clean-tree`; the blobs live in git history at the
pre-cleanup commit `c82b2e7`:**

| experiment | pooled hits / total + result | exact McNemar (pooled) | verdict |
|---|---|---|---|
| V3a lambdarank within-day ranking objective | ranker **832/3159 = 26.34%** (HGB 916, logreg 917, hgpg 886, gpg 837) | ranker vs gpg 394-399 p=0.89; vs hgpg 332-386 p=0.048 (**worse**); HGB vs gpg 442-363 p=0.0059 | rejected (fell to the GPG level) |
| V3b calendar/schedule/rel-goalie features | HGB 907/3159 = 28.71%; ensemble 921 = 29.16% | V3b HGB vs V2 HGB 202-211 p=0.69; ens vs hgpg 280-245 p=0.14 | reverted (no gain) |
| V3c post-hoc blends on saved OOF | 9 variants; best z-score hgb+logreg+live 935/3159 = 29.598% (+0.317pp) | vs hgpg 211-162 p=0.013, but vs ensemble 164-154 p=0.614 | no variant qualified (needed +0.5pp & p<0.10 vs ens) |
| V3d model-family diversity | lgbm single 959/3159 = 30.358% (+1.08pp); all-four bag 946 = 29.946% | lgbm vs hgpg 354-281 p=0.0042 but vs ensemble 237-203 p=0.116 | defaults unchanged (missed p<0.10 bar vs ens) |
| V3e LGBM seed robustness | seeds 1/2/3 = 957/950/934, mean 29.978% (seed std 0.373) | pooled vs hgpg 1025-842 p=2.5e-05; loses the seasons split (394 < 396 hgpg) | default stays hgb |
| V3f LGBM + baseline blends | best rank 0.75 lgbm + 0.25 hgpg mean 29.841 ± 0.120 | vs lgbm pooled p>=0.12 for all hgpg blends (173-186 p=0.527) | no blend qualified |
| V3g seed bagging / LGBM config sweep | bag 945/3159 = 29.915% (< lgbm-alone 29.978%); config b timings out at 200s, c seed 1 939 = 29.725% | bag vs hgpg 328-269 p=0.0175 | no bag / no config qualified |
| V4 raw shot/TOI block — **promoted** | HGB seed 0 961/3159 = 30.421%, seed 42 950 = 30.073% | HGB vs hgpg 406-342 p=0.0212; vs gpg 468-355 p=9.2e-05 | promoted into `NUMERIC_FEATURES` (clean cross-family re-check LGBM +0.063/-0.253pp, p=0.962/0.733; the k-fold edge did NOT transfer to the seasons split) |
| V5 pick voting / recency weighting | A1 vote 929/3159 = 29.408%; A2 seed vote 938; recency mixed (linear -0.475/+0.886pp, half worse both) | vote vs hgpg 249-206 p=0.049 but vs lgbm 148-178 p=0.108 | rejected (nothing beat LGBM alone) |
| V6 lagged lineup/PP replacements | mean **+0.095pp** (29.376→29.408 / 28.870→29.313 / 28.997→28.807) | pooled 454-445 p=0.790 | reverted (< +0.5pp; the mandatory same-game leak correction is retained) |
| V7-objective Poisson LGBM + conditional softmax | Poisson pooled 1,855/6,318 = 29.361% (+0.174pp) +0.25/+0.09pp per seed; csoftmax 1,782 = 28.205% (−0.981pp, AUC 0.569) | Poisson vs binary 284-273 p=0.672 | rejected (both arms below the gate) |
| V7-context team form / special teams / goalie rest | 930/3159 = 29.440% (+0.063pp) / 908 = 28.743% (−0.253pp); mean −0.095pp | vs base p=0.954/0.680 | rejected |
| V7-shots raw distance/angle/HD/slot/rebound/PP | 924/3159 = 29.250% (−0.127pp) / 934 = 29.566% (+0.570pp), McNemar p>=0.37 | vs base p=0.874/0.369 | rejected (mixed) |
| V8-own-xG first-party per-shot xG model | 923/3159 = 29.218% (−0.158pp) / 905 = 28.648% (−0.349pp); per-shot AUC 0.769 | vs base p=0.82/0.57 | rejected |
| V8-shrinkage empirical-Bayes rate features | 907/3159 = 28.712% (−0.665pp) / 899 = 28.458% (−0.538pp) | vs base p=0.229/0.326 | rejected |
| V8-tune LGBM configs b/d | b +0.19 / −0.03pp; d −0.06 / +0.19pp vs a | vs a p=0.755/1.0 (b), 0.945/0.728 (d) | rejected (mixed) |
| V9-projected-lineups (prior-games projector, stages 1-5) | hard b +0.98 / −0.25pp (seed 1 p=0.081 / seed 3 p=0.695); probabilistic −0.41 / 0.00pp; projected ensemble +0.48 / −0.22pp (end-to-end seed 1 944/3159 = 29.883%, p=0.432 vs no-lineup); oracle LGBM 4-seed 30.334% ± 0.263 | projected ens vs hgpg p=0.025/0.049; vs no-lineup p=0.465/0.783 | rejected (mixed); oracle ensemble −0.16 / +0.38; `--lineup projected` stays opt-in, default no-lineup |
| V12-identity (player/team/age) | 881/3159 = 27.889% (−1.487pp) / 907 = 28.712% (−0.285pp) | p=0.058/0.734 | rejected |
| V12-era recent training (2021-22+) | LGBM −1.07 / −0.95pp; ensemble −0.15 / +0.08pp vs full-era | — | rejected |
| V12-qoc (shift-chart QoC/QoT) | 944/3159 = 29.883% (+0.507pp) / 917 = 29.028% (+0.032pp) | vs base p=0.413/1.0 | rejected (mixed) |
| V12-interactions (tims x rates) | 931/3159 = 29.471% (+0.095pp) / 0.000pp; g3 −0.57pp | vs base p=0.904/1.0 | rejected |
| V15 pre-2017 data (2013-17) | LGBM +0.10 / +0.41pp (931/3159 = 29.471% / 929 = 29.408%); ensemble 933 = 29.535% (−0.380pp) / 935 = 29.598% (−0.222pp) | LGBM p=0.91/0.52 | rejected (not adopted) |
| V16-exposure (prior xG/60 x projected-TOI product) | base ens 926→921 seed 1, 935→933 seed 3; LGBM 931→922, 929→924 | p=0.8023/0.9508 and 0.6108/0.8067 vs paired base; product correlates r=0.94 with xg60_prior and 0.85 with hgpg | rejected |
| V16-group3 (g3 weights w2/w4 + dedicated g3 model) | everything below the paired base on both seeds (g3 20.7/20.2 vs base 21.7 seed 1; 20.9/20.6 vs 21.1 seed 3) | vs base p≥0.27 all arms (g3pure 67-95 p=0.0336 *worse* seed 1) | rejected |
| V16-pbp (pass-context per-shot xG) | per-shot AUC +0.0007/+0.0009 over geometry-only 0.765; decisive matched-data pair LGBM −0.158/−0.127pp (6,993 games / 608,813 shots) | a-b p=0.8189/0.8567 | rejected |
| V16-calib (confident-miss competitor swap) | seed 1 936/3159 = 29.630% (+0.35pp), seed 3 −0.16pp; McNemar vs ensemble p=0.51/0.78 | | rejected |
| V16-interact (home x form / rest x role / opponent-relative) | ensemble +0.506/+0.253pp vs paired arm, LGBM −0.728/+0.095pp; ens McNemar p=0.3447/0.6593 | | rejected |
| V16-embed (ALS latent factors) | seed 1 −0.03pp / seed 3 +0.57pp | | **INVALID (leakage)** — see below |

**INVALID — excluded (leakage):** the ALS latent-factor experiment
(`493ba96`) built player x season aggregate cells over **ALL training dates**
while the folds are shuffled date blocks, so a held-out game's factor includes
the player's **LATER same-season games** — **97.85%** of challenge rows are
affected (90,450 / 92,440 rows; per fold 97.57-98.08%); the seed-3 +0.57pp is
lookahead, not signal (independent OOF re-score audit, 2026-10-22; runs at
pre-cleanup commit `c82b2e7` in history). The latent-factor idea remains
**UNTESTED**; if retried it must use only strictly prior games per row
(expanding window).

The **V6-era shift-unit leak** is the same class of failure: the
pre-correction headlines (V2-V5, incl. LGBM 30.36% / HGB 30.07-30.42%) used
`lineup_unit`/`pp_unit` from the same game's shift chart, so they are invalid
as deployment estimates; corrected LGBM is **29.06%** (918/3159).


**Post-roadmap findings:** identity features were negative; recent-era
training did not beat full-era on identical dates, but per-season accuracy
rises (2025-26 ensemble **31.6-32.9%** vs hgpg **30.3%**). QoC/QoT was mixed
(+0.51/+0.03pp) and reverted. Baseline per-group accuracy is **g1 39.9% /
g2 27.2% / g3 20.9%**; mean max predicted probability **0.315** vs realized
**0.293**.

**Pre-2017 data (V15):** 190,070 extra skater rows were ingested and published
(2013-17, 617,310 snapshot rows total); LGBM +0.10/+0.41pp but the default
ensemble was **−0.38/−0.22pp**, so it was **not adopted**. Default remains the
2017+ no-lineup ensemble (**29.71%** mean all-era, **~30-32%** recent-era).
The four store dumps are committed (`9c9f315`) and `--refresh` now uses
date-window pagination to avoid the PostgREST 57014 timeout.

**Conclusion:** with the currently available historical raw data, **~29.7% is
the pooled all-era expectation** (and **~30-32% on recent-era dates**),
contingent on real projection fidelity; V11 puts realistic 80-90% fidelity at
**~29.3-29.9%**. ≥30% was reachable only with same-game lookahead leakage.

**Data priorities (owner rule, 2026-10-24):** (1) persist real **PROJECTED**
lineups with units going forward — `snapshot_projected.py` + the nightly
unit-match of build projections vs backtracking actuals (`fidelity_tracker.py`,
PR #137 tracker) make projection fidelity measurable, and the adoption gate
is fidelity >= 90% AND paired beats of the no-lineup ensemble on both seeds.
(2) **No market odds** — per owner rule the market-anytime-goal-scorer-odds
collection idea is dropped (historical odds are not free before 2023-05
anyway; the feasibility scan is recorded in the V7 section).

**Legitimate deployment lever, unverifiable historically:** projected
lineups and confirmed starting goalies are available pre-game in the live
pipeline — the crude projector gave an ensemble 29.88/29.60 (seeds 1/3) and the
oracle-lineup LGBM 4-seed mean is 30.33 ±0.26. V11 puts 80-90% line fidelity at
roughly **29.25-29.54%**; reaching 29.7-30.3 needs a near-oracle feed (~100%).
Test a real feed before switching the default.

**Reproduce (default model — the canonical live ensemble):**
```bash
cd SmartScore-backtrack
for s in 0 1 3; do
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/model.py --env dev --model ensemble \
    --seed "$s" --split kfold --splits 5 --train-subset all
done
```
(V4 shot/TOI features are retained in `NUMERIC_FEATURES`; the injury filter
was never worth testing - fields 100% null.)

The V6-era leakage audit scripts and their write-up
(`docs/backtrack_audit.py`, `docs/backtrack_ablation.py`,
`docs/backtrack_model_audit.md`) were deleted in `chore/clean-tree`;
see commit `c82b2e7` in history.

Working log for the goal-probability model built on the backtracked archive.
Purpose: record what was tried, what worked, what broke, and the data gotchas,
so nothing gets re-derived twice. Update it every session.

**Repository state (2026-10-24, `chore/clean-tree`):** this is now the single
results/history document. All experiment scripts, per-experiment notes,
audit scripts, and experiment CSV caches were **deleted** (not archived) —
every exact blob is preserved in git history at the pre-cleanup commit
**`c82b2e7`** (`git show c82b2e7:<path>`). What remains: the 11 canonical
model/pipeline scripts plus `snapshot_projected.py` / `fidelity_tracker.py`,
the operational runbooks, this file, and `docs/supabase_shot_columns.md` on
its own branch (see the deployment paragraph).

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
   Follow-up: 22,176 of the 474,721 `player_games` rows carry a NULL
   `opponent_abbrev` (the first player rows of each game's block). Every one
   of the 11,870 games has exactly two distinct clubs, so repair the opponent
   from the other club in the same `game_id` before grouping by opponent.
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
9. **PBP fetch retry envelope (V16, 2026-10-23).** The gamecenter
   play-by-play fetcher pulled 6,993/6,993 games with 0 fetch/parse
   failures, but the endpoint's HTTP 429s are NOT retried by the
   `nhl_client._get_json` retry envelope (4 attempts, 30s timeout, 1/2/4s
   backoff) by design; three fan-out processes trip them, so the fetcher
   runs a >50%-failure-rate breaker that stops the pass. One serialized
   process at a 0.75s delay sustains ~1.3 games/s with zero failures;
   exhausted game_ids landed in `data/ml/pbp_failed.csv` for re-running.
10. **PostgREST 57014 statement timeout on big table reads.** `--refresh`
    on 617,310 rows died with `57014 canceling statement due to statement
    timeout` after 254s (ordered offset pagination); `fetch_table` now
    fetches monthly date windows (`_month_windows` + `_fetch_window`,
    153 windows for 2013-10-01..2026-06-14). Fix pagination, don't raise
    the statement timeout.

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

Deployment pointer (2026-10-27): the deployed picks path now uses exact
same-day shot-block computation via the runner-side store refresh (dumps
load + current-season crawl on cache-miss, zero Supabase) - commit cd05868
plus the refresh-store/workflow commit; see docs/ml-picks-runbook.md §1.1.

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

### V6 — leakage correction: current-game line/PP labels removed

**Confirmed leak:** `DATASET_FEATURES` contained `lineup_unit`, `pp_unit` and
`lineup_position_group`. `lineup_unit`/`pp_unit` are built by
`lineup_reconstruct.reconstruct_game` from the **same game's shift chart**
(completed-game shifts), so they are lookahead. `lineup_position_group` is
F/D from the box-score `positions` mapping (static roster position), not a
shift chart, and is retained. All V2-V5 headline numbers that used
`lineup_unit`/`pp_unit` are therefore invalid as deployment estimates.

**Lagged replacements (tested, then reverted):** `last_lineup_unit`,
`last_pp_unit`, `mode_lineup_unit_5`, `mode_pp_unit_5`, `line_stability_5`,
`pp_share_5`, computed per player sorted by date from rows `[i-5, i)` only;
NaN before the first game.

Leakage proof: 10 distinct players with a lineup change all had
`last_lineup_unit` equal to the previous game's unit, never the current one:

| player | date | prev | current | lag |
|---|---|---|---|---|
| 8448208 | 2017-10-14 | F3 | F1 | F3 |
| 8464989 | 2017-10-14 | F4 | F3 | F4 |
| 8465009 | 2017-10-09 | D2 | D3 | D2 |
| 8466138 | 2017-10-07 | F2 | F1 | F2 |
| 8466139 | 2017-10-09 | F3 | F2 | F3 |
| 8466251 | 2017-10-09 | F3 | F4 | F3 |
| 8467370 | 2018-03-06 | F4 | F3 | F4 |
| 8467400 | 2017-10-13 | D2 | D1 | D2 |
| 8467407 | 2018-03-11 | F3 | F2 | F3 |
| 8467875 | 2017-10-10 | F3 | F2 | F3 |

The same 10/10 check passed for `last_pp_unit`. The lagged code does no SQL:
it only shifts/slices the already-joined frame. The raw queries are
`club_map` (selects `game_id` but has no current-game predicate) and
`shot_features` (no `game_id` column at all).

LGBM, date-blocked k=5, train=all, seeds 1-3, `OMP_NUM_THREADS=2`,
`LGBM_N_JOBS=2`, 93-103s each:

| seed | corrected baseline (no current lineup/PP) | + lagged features | delta |
|---|---:|---:|---:|
| 1 | 928/3159 = 29.376% | 929/3159 = 29.408% | +0.032pp |
| 2 | 912/3159 = 28.870% | 926/3159 = 29.313% | +0.443pp |
| 3 | 916/3159 = 28.997% | 910/3159 = 28.807% | -0.190pp |
| mean | 29.081% (std 0.264) | 29.176% (std 0.323) | **+0.095pp** |

McNemar vs hgpg: 336-293 p=0.094, 331-291 p=0.118, 319-295 p=0.353; pooled
986-879 p=0.0141. Lagged vs corrected baseline: 166-165 p=1.0, 148-134
p=0.439, 140-146 p=0.768; pooled 454-445 p=0.790.

Lagged-column importances (fold-1 LGBM gain rank among 48 features;
permutation AUC drop): mode_lineup_unit_5 #2 (95,095; 0.0053),
last_lineup_unit #3 (30,011; -0.0001), pp_share_5 #13 (6,785; 0.0014),
last_pp_unit #15 (5,862; 0.0033), mode_pp_unit_5 #24 (3,206; 0.0008),
line_stability_5 #39 (1,071; 0.0004).

Verdict: lagged features are leak-free but recover only **+0.095pp** on
average (< +0.5pp) and are mixed by seed, so they were **reverted**. The
mandatory correction is kept: `lineup_unit` and `pp_unit` are removed from
`DATASET_FEATURES`; only `lineup_position_group` and `tims` remain as
categoricals. The corrected LGBM reference is **29.08%** mean (audit seed-0
corrected baseline 892/3159 = 28.237%). Prior HGB/LGBM headlines that used
current-game lineups are invalid; a corrected HGB run is still needed.

### V6b — corrected (leak-free) headline

Corrected HGB seed 0 (`--model hgb`, 213s) also produced logreg in the same
pass, so no separate logreg run was needed. Corrected LGBM seed 0 took 90s;
the ensemble default run took 257s.

| method | hits / 3159 | acc | per-fold std | vs hgpg | vs corrected LGBM |
|---|---:|---:|---:|---|---|
| gpg | 837 | 26.496% | 1.163 | 326-375, p=0.070 | 319-400, p=0.0028 |
| hgpg | 886 | 28.047% | 1.631 | — | 297-329, p=0.215 |
| hgb | 917 | 29.028% | 2.409 | 373-342, p=0.262 | 223-224, p=1.0 |
| logreg | 918 | 29.060% | 1.940 | 273-241, p=0.172 | 254-254, p=1.0 |
| lgbm | 918 | 29.060% | 2.075 | 329-297, p=0.215 | — |
| rank ensemble (hgb+logreg+lgbm) | 943 | 29.851% | 2.357 | 335-278, p=0.024 | 136-111, p=0.127 |
| ensemble, end-to-end default run | 945 | 29.915% | 2.395 | 338-278, p=0.017 | 139-111, p=0.088 |

The seed-0 corrected LGBM is **918/3159 = 29.060%**, not the audit's 892:
the audit number was on the pre-V4 feature set; the current 42-feature set
includes the V4 shot block, so the same-seed corrected baseline is 918.

The ensemble beats corrected LGBM by **+0.791pp** on the reconstructed OOFs
(+0.855pp in the end-to-end run), so it clears the stated +0.5pp rule and is
now the documented default (`--model ensemble`, mean percentile rank of
hgb+logreg+lgbm). Caveat: the paired McNemar is p=0.127 on the reconstructed
OOF (p=0.088 end-to-end, where one tie makes 946 vs the printed 945 picks), so
the edge is directional rather than decisive.

### V6c — ensemble multi-seed and injury audit

End-to-end default ensemble (`--model ensemble`, k=5, train=all,
`OMP_NUM_THREADS=2`, 256s/seed):

| seed | hits / 3159 | acc | per-fold std | vs hgpg | vs same-seed corrected LGBM |
|---|---:|---:|---:|---|---|
| 0 | 945 | 29.915% | 2.395 | 338-278, p=0.017 | 139-111, p=0.088 |
| 1 | 929 | 29.408% | 2.827 | 319-276, p=0.085 | 122-121, p=1.0 |
| 3 | 942 | 29.820% | 0.633 | 314-259, p=0.024 | 134-109, p=0.124 |
| **mean** | — | **29.714 ± 0.277** | — | — | — |

Same-seed corrected LGBM: seed 0 918 (29.060%), seed 1 928 (29.376%),
seed 3 916 (28.997%). Ensemble deltas: +0.855, +0.032, +0.823pp; mean
+0.570pp. The edge is not uniform — seed 1 is a tie — so treat the ensemble
as directionally better, not a guaranteed per-seed win. OOF re-picks differ
from the printed totals by one tie on seeds 0/3 (946/941 vs 945/942).

**Injury audit** (92,440 challenge rows): `injury_status` non-null **0**,
distinct **0**; `injury_desc` non-null **0**, distinct **0**. Overall scored
rate 17.256%, with no status breakdown possible. Of the 3,159 seed-0 ensemble
picks, **0** landed on a player with non-null injury status. The injury fields
are never populated on the backtrack archive, so a pre-game injury filter or
feature has no signal and is **not worth testing**.

### V3h — player-vs-opponent history features (reverted)

Hypothesis: a player's record against the specific opponent may add pre-game
signal beyond season-to-date / historic GPG. Four features, all built from
`player_games` and strictly prior games:

- `opp_gpg_2y` — player goals / games vs this opponent over the current
  season-to-date plus the immediately preceding season (all prior meetings if
  fewer), NaN if none.
- `opp_gpg_career` — same over every prior meeting with that opponent.
- `opp_goals_last3` — goals in the player's last up-to-3 prior meetings.
- `team_vs_opp_gpg_2y` — club `team_goals_for` per game vs this opponent over
  the same current + previous season window (every club appearance, not only
  this player's).

Built by sorting `player_games` per `(player_id, opp)` (and per
`(club, opp)` at club grain) and using `cumcount` / shifted cumulative sums,
so the current game never enters its own feature. The 2-year window uses the
raw `season` label, not calendar date (the 2019-20 season runs into Sep 2020).
Joined on `(date, player_id)` and `(date, club)`.

Validation: 427,240 rows before and after the join; no duplicate/suffixed
columns; exactly the four new columns. Coverage of 427,240 rows:
`opp_gpg_2y` 360,763, `opp_gpg_career` 380,497, `opp_goals_last3` 380,497,
`team_vs_opp_gpg_2y` 395,421. Hand-checked Draisaitl vs NSH on 2021-11-03
(`opp_goals_last3=8` = 2+2+4 from the three prior meetings) and random
samples against independent recomputation (300/300 career, 150/150 player
2-year, 200/200 club 2-year).

Protocol: date-blocked k=5, `--train-subset all`, LGBM 400 trees, n_jobs=2,
seeds 1 and 3, 3,159 pooled picks. The baseline was rerun in the same worktree
and reproduced V3e exactly (seed 1 957 hits, seed 3 934).

| seed | baseline hits / acc | V3h hits / acc | Δ | McNemar (base-only / V3h-only) |
|---|---:|---:|---:|---:|
| 1 | 957 / 30.294% | 938 / 29.693% | **-0.601pp** | 151-132, p=0.285 |
| 3 | 934 / 29.566% | 940 / 29.756% | **+0.190pp** | 145-151, p=0.771 |
| mean | — / 29.930% | — / 29.725% | **-0.206pp** | — |
| pooled seeds (descriptive) | 1,891 / 6,318 29.930% | 1,878 / 6,318 29.725% | -0.206pp | 296-283, p=0.618 |

Held-out AUC moved only +0.0002 (seed 1, 0.6946→0.6948) and +0.0006 (seed 3,
0.6944→0.6950). Per-fold seed 1 lost in 4 of 5 folds
(-0.8/-0.8/-0.6/-1.5/+0.8pp); seed 3 was mixed (+0.1/-0.9/+0.8/+1.1/-0.2pp).
Only 283-296 of 3,159 picks changed per seed.

LGBM gain importance (mean over folds, share of total; the two seeds agree to
2 d.p.): `opp_gpg_career` 1.06% (rank 12), `team_vs_opp_gpg_2y` 1.03% (13),
`opp_gpg_2y` 0.50% (22), `opp_goals_last3` 0.22% (29). They rank well below
gpg/hgpg and near ppg/otga, i.e. the model uses them only weakly.

Verdict: **reverted**. The gain is not consistent (seed 1 -0.60pp, seed 3
was +0.19pp; mean -0.21pp) and neither seed's McNemar is close to significant
(p=0.28, 0.77). Head-to-head history looks largely absorbed by the existing
season rates plus opponent team/goalie context. No `--opp-history` flag or
feature code retained; notes only.

### V7-objective — Poisson LGBM and conditional-softmax logit (reverted)

Protocol: date-blocked k=5, train=all, seeds 1/3, `OMP_NUM_THREADS=2`,
`LGBM_N_JOBS=2`, `timeout 300`, 3,159 pooled picks. The corrected binary
baseline reproduced exactly in this worktree: seed 1 **928 = 29.376%**, seed 3
**916 = 28.997%**. Both arms use the same 42-feature set and the same folds.

**A. Poisson LGBM.** Added `--objective {binary,poisson}`. The Poisson arm fits
`LGBMRegressor(objective="poisson")` on the raw `player_games.goals` count
(identical tree config) and scores `1 - exp(-lambda)`. All 92,440 challenge
rows join a goal count, and `goals >= 1` matches `scored = 1` exactly (no
mismatch).

| seed | binary | poisson | delta | McNemar vs binary | McNemar vs hgpg |
|---|---:|---:|---:|---|---|
| 1 | 928 / 29.376% | 936 / 29.630% | +0.254pp | 153-145, p=0.685 | 326-276, p=0.046 |
| 3 | 916 / 28.997% | 919 / 29.091% | +0.094pp | 131-128, p=0.901 | 310-277, p=0.187 |
| pooled | 1,844 / 29.186% | 1,855 / 29.361% | +0.174pp | 284-273, p=0.672 | 636-553, p=0.017 |

Held-out AUC 0.6929 (both seeds) vs 0.6924 / 0.6922 for binary. Runtime ~150s
per seed with both seeds running concurrently, under the 300s cap.

Verdict: rejected — positive on both seeds but only +0.09pp on seed 3, well
short of +0.5pp, and the paired McNemar vs binary is a tie.

**B. Conditional softmax.** Added `--model csoftmax`: a within-`(date, tims)`
conditional logit on the same features, fitted by scipy L-BFGS on challenge
rows only (PyTorch was not installed). Design matrix: 40 numeric features
median-imputed and standardised on the training rows, `lineup_position_group`
one-hot (F / D / missing) = 43 columns; `tims` is constant within a group and
cancels exactly in the softmax. Groups average 29.3 rows and 5.05 scorers (223
of 3,159 have none), so the likelihood sums the log probability of every
scored row in the group (multiple-choice conditional logit). Returned score is
the within-group softmax probability.

| seed | binary | csoftmax | delta | McNemar vs binary | McNemar vs hgpg |
|---|---:|---:|---:|---|---|
| 1 | 928 / 29.376% | 888 / 28.110% | -1.266pp | 228-268, p=0.080 | 328-326, p=0.969 |
| 3 | 916 / 28.997% | 894 / 28.300% | -0.697pp | 229-251, p=0.338 | 336-328, p=0.786 |
| pooled | 1,844 / 29.186% | 1,782 / 28.205% | -0.981pp | 457-519, p=0.051 | 664-654, p=0.804 |

Training time 1.9-6.4s per fold (143-300 L-BFGS iterations; one seed-3 fold
hit the 300 cap), ~64s per seed end-to-end including the 14s frame build.
Held-out AUC 0.569 — far below LGBM's 0.692. The linear group-softmax cannot
represent the GBDT's nonlinear interactions, and it optimises within-slate rank
rather than the global 0/1 calibration the pointwise models exploit.

Verdict: rejected — worse than binary on both seeds and no better than hgpg.
No code changes retained; the default remains `--model ensemble`.

### V7-context — team form / special teams / goalie rest (reverted)

Hypothesis: the corrected baseline has player rates and opponent-goalie
quality, but little recent **team** form or special-teams context. Added 13
club-game features, all strictly prior games, from `player_games`:

- `team_shots_for_5/10`, `team_shots_against_5/10` — rolling club shots.
- `team_goals_for_5/10`, `team_goals_against_5/10` — rolling official club
  goals (goals against = the opponent's `team_goals_for` in the same game).
- `shooting_pct_5` = prior-5 goals / shots for; `save_pct_5` =
  1 - prior-5 goals against / shots against.
- `opp_penalties_drawn_pg` — opponent PIM/2 per game, season-to-date prior
  (penalties the opponent takes, i.e. the club's PP exposure).
- `opp_pp_opportunities_pg` — club PIM/2 per game, season-to-date prior
  (penalties the club takes, i.e. the opponent's PP exposure).
- `goalie_rest_days` — days since the opponent starting goalie's previous
  start, NaN on his first start in coverage.

The cached archive has no penalty-event table, so PIM/2 is the only penalty
proxy and over-counts majors/misconducts. `lineup_unit`/`pp_unit` were not
reintroduced; categoricals remain `lineup_position_group` and `tims`.

Leakage checks: 427,240 rows before/after the join, no suffixed columns,
exactly the 13 new columns. Coverage: rolling/PDO features 426,646 rows,
penalty features 422,134 / 422,133, `goalie_rest_days` 423,568. Independent
recomputation matched 120/120 rolling+PDO rows, 150/150 penalty rows and
150/150 goalie-rest rows; every window is shifted one club game and every
season-to-date prefix excludes the current game. Club-game grain is 23,740
rows with one starting goalie per club-game.

Protocol: LGBM, date-blocked k=5, `--train-subset all`, seeds 1 and 3,
`OMP_NUM_THREADS=2`, `LGBM_N_JOBS=2`, 3,159 picks. The corrected baseline was
rerun in the same worktree and reproduced V6c exactly (seed 1 928, seed 3 916).

| seed | baseline hits / acc | V7 hits / acc | Δ | McNemar vs baseline | McNemar vs hgpg |
|---|---:|---:|---:|---:|---:|
| 1 | 928 / 29.376% | 930 / 29.440% | **+0.063pp** | 152-154, p=0.954 | 327-283, p=0.082 |
| 3 | 916 / 28.997% | 908 / 28.743% | **-0.253pp** | 148-140, p=0.680 | 304-282, p=0.386 |
| mean | — / 29.187% | — / 29.092% | **-0.095pp** | — | — |

Held-out AUC moved +0.0004 on both seeds (0.6924→0.6928, 0.6922→0.6926).
Per-fold deltas: seed 1 +0.8/-0.7/-0.6/+0.5/+0.3pp; seed 3
0.0/+0.5/-2.2/+1.1/-0.6pp. Only 288-306 of 3,159 picks changed per seed.

LGBM gain importance (mean over folds, share of total; 47 features; seed-1
rank / share, seed-3 rank / share): `opp_pp_opportunities_pg` 17 / 0.59%,
23 / 0.55%; `opp_penalties_drawn_pg` 18 / 0.59%, 17 / 0.62%; `save_pct_5`
19 / 0.59%, 19 / 0.60%; `team_shots_against_10` 22 / 0.55%, 21 / 0.56%;
`team_shots_against_5` 24 / 0.52%, 24 / 0.51%; `team_shots_for_10`
27 / 0.48%, 29 / 0.44%; `shooting_pct_5` 28 / 0.44%, 27 / 0.48%;
`goalie_rest_days` 29 / 0.43%, 30 / 0.44%; `team_shots_for_5`
32 / 0.40%, 32 / 0.42%; `team_goals_against_10` 40 / 0.28%, 40 / 0.29%;
`team_goals_for_10` 41 / 0.28%, 41 / 0.28%; `team_goals_for_5`
44 / 0.19%, 45 / 0.19%; `team_goals_against_5` 45 / 0.17%, 44 / 0.19%.
All are well behind the player shot/career block and gpg/hgpg.

Verdict: **reverted**. Seed 1 is a statistical tie (+0.06pp, p=0.95) and seed
3 is worse (-0.25pp); mean -0.10pp. The team context block adds no
deployment-relevant edge on top of the corrected baseline. No
`--context-features` flag or feature code retained; notes only.

**Anytime-goal-scorer odds feasibility (no-cost check, no sign-up).** Not
available for free for this archive's era. The Odds API carries NHL player
props including Anytime Goal Scorer (Yes/No) for US books, but historical
player-prop snapshots only start **2023-05-03** and historical access is
**paid**; NHL game-line history starts 2020-06-29. Its free tier is
current/upcoming odds only, so it cannot backfill 2017-2023 and cannot supply
a free historical training set. OddsPortal / Covers expose historical game
odds on public pages (no official free API; player-prop coverage is patchy
and scraping is ToS-constrained); OddsPapi advertises free historical odds
including anytime goalscorer but needs an account and has unverified depth;
SportsGameOdds / BallDontLie / OddsBlaze / RapidOdds offer current props,
usually with paid historical tiers. Practical options: (a) collect current
anytime-goal-scorer snapshots forward via a free-tier odds API, or (b) pay
for a 2023+ player-prop backfill; neither helps the current 2017-2026
backtest at zero cost.

### V7-shots — own raw shot-event quality block (reverted)

Hypothesis: replace MoneyPuck's xG with our own pre-game shot-quality features
built from the raw shot-event archive: distance/angle, high-danger and slot
attempts, rebounds, rushes, PP usage and goals, all prior-games only. The
V7-xG experiment was collinear with V4's volume/TOI block; raw geometry is a
different signal and does not import any external xG column.

**Data.** MoneyPuck raw shot events, one row per unblocked attempt
(`event in {SHOT, MISS, GOAL}`): 2017-18 .. 2025-26, 1,025,121 attempts and
71,616 goals over 11,268 games. Nine per-season files were fetched from
`moneypuck.com/moneypuck/playerData/shots/shots_{season}.zip` (2025/2026) and
the MoneyPuck mirror `peter-tanner.com/moneypuck/downloads/shots_{season}.zip`
(2017-2024), verified season-by-season from `gameDate` min/max. Cached under
`data/moneypuck/shots/` (gitignored). Season start year = filename year;
`shots_2020` is the COVID 2020-21 season (Jan-Jul 2021). The partial 2026-27
file was excluded.

The raw files have no `gameDate`, `situation` or opponent columns. Game date is
mapped from `game_id` via `nhl_game_id = season*1_000_000 + game_id` against
`player_games` (100% of 2017-2025 games mapped); opponent/strength come from
home/away codes and skater counts. Build table:
`data/moneypuck/shots/player_game_shots_full.csv` — **427,402 player-games**
(364,488 with ≥1 attempt, 62,914 zero-attempt skater-games) and 1,940 players.

**Features** (18, strictly prior games; shift(1) before every window; the
zero-attempt rows stay in the denominators):

| feature | definition |
|---|---|
| `rs_shot_att_pg_5/10` | mean unblocked attempts per game |
| `rs_hd_att_pg_5/10` | mean attempts with distance < 20 ft and abs(angle) < 30° |
| `rs_slot_att_pg_5/10` | mean attempts with 54 ≤ abs(x) ≤ 89 and abs(y) ≤ 22 (arena-adjusted) |
| `rs_rebound_att_pg_5` | mean `shotRebound == 1` |
| `rs_pp_att_pg_5` | mean own skaters > opponent skaters |
| `rs_goals_pg_5` | mean raw goal flag |
| `rs_mean_distance_5` | attempt-weighted mean distance (ft) |
| `rs_mean_angle_5` | attempt-weighted mean abs angle (deg) |
| `rs_hd_share_5` | sum(HD attempts) / sum(attempts) over prior 5 |
| `rs_sd_shot_att_pg`, `rs_sd_hd_att_pg`, `rs_sd_goals_pg`, `rs_sd_hd_share`, `rs_sd_mean_distance`, `rs_sd_mean_angle` | same rates season-to-date before the row |

**Leakage / merge checks.**

- Frame stays **427,240 rows**; no `_x`/`_y` and no duplicate columns.
- Coverage `rs_shot_att_pg_5` **425,349 / 427,240 = 99.56%** (1,891 first
  career games are NaN).
- Hand-checked three player-dates against the raw event rows (McDavid
  2017-10-21, Ovechkin 2017-10-17, and player 8448208 2017-11-09 whose prior
  five games include attempts `[5, 0, 4, 1, 0]`). All nine rolling values
  matched exactly (max abs diff 3.6e-15); e.g. 8448208 `rs_shot_att_pg_5` =
  10/5 = 2.0, proving zero-attempt games count in the denominator.
- Cross-check against the abandoned team files: 2024-25 regular unblocked
  attempts 112,126 vs `I_F_shotsOnGoal + I_F_missedShots` 112,222 (-0.09%),
  goals 7,891 vs 7,898 (-0.09%).

**Protocol.** Date-blocked k=5, `--train-subset all`, `OMP_NUM_THREADS=2`,
`LGBM_N_JOBS=2`, `timeout 300`, LGBM config a. Baselines are the corrected V7
base without the block (`oof_v7_base_lgbm_seed{1,3}.csv`): seed 1
**928/3159 = 29.376%**, seed 3 **916/3159 = 28.997%**; hgpg **886 = 28.047%**
in both. AUC 0.6933 ± 0.0051 (seed 1), 0.6934 ± 0.0027 (seed 3).

| model | seed | baseline | + raw shots | delta | McNemar vs base | McNemar vs hgpg |
|---|---|---:|---:|---:|---|---|
| LGBM | 1 | 928 = 29.376% | **924 = 29.250%** | **-0.127pp** | 178-182, p=0.874 | 326-288, p=0.135 |
| LGBM | 3 | 916 = 28.997% | **934 = 29.566%** | **+0.570pp** | 188-170, p=0.369 | 324-276, p=0.055 |
| LGBM mean | | 29.186% | 29.408% | +0.221pp | | |

Per-fold: seed 1 31.0/28.1/27.0/32.1/28.1 (std 2.148); seed 3
30.2/27.6/27.5/31.3/31.3 (std 1.883). The ensemble was **not run** because
both LGBM seeds did not improve (the pre-declared condition).

**Importances** (fold-1 LGBM, seed 1, gain; rank among 60 features):

| feature | gain | rank |
|---|---:|---:|
| `rs_sd_shot_att_pg` | 413 | 5 |
| `rs_sd_mean_distance` | 373 | 8 |
| `rs_mean_angle_5` | 364 | 9 |
| `rs_sd_mean_angle` | 341 | 12 |
| `rs_mean_distance_5` | 320 | 14 |
| `rs_slot_att_pg_10` | 268 | 18 |
| `rs_sd_hd_share` | 218 | 24 |
| `rs_pp_att_pg_5` | 207 | 26 |
| `rs_hd_share_5` | 206 | 27 |
| `rs_sd_hd_att_pg` | 184 | 28 |
| `rs_shot_att_pg_10` | 178 | 31 |
| `rs_shot_att_pg_5` | 152 | 36 |
| `rs_slot_att_pg_5` | 127 | 39 |
| `rs_sd_goals_pg` | 112 | 41 |
| `rs_hd_att_pg_10` | 94 | 46 |
| `rs_hd_att_pg_5` | 49 | 53 |
| `rs_rebound_att_pg_5` | 36 | 55 |
| `rs_goals_pg_5` | 13 | 59 |

Geometry features are used heavily (five in the top 14), but the goal/rebound
flags are near the bottom and the pick metric did not follow.

**Verdict: reverted.** Seed 1 is negative (-0.13pp, p=0.87 vs baseline) and
seed 3 is positive (+0.57pp, p=0.37 vs baseline); this is mixed, not the
+0.5pp-on-both-seeds bar, so the feature block and `--shot-features` flag were
removed and `model.py` is back to the corrected baseline. The per-player
table and raw caches were deleted from disk in `chore/clean-tree`
(owner-approved: data only; blobs in git history at `c82b2e7`).

### V8-own-xG — our own per-shot expected-goals model (reverted)

Hypothesis: the external MoneyPuck xG block was collinear with V4's volume
features, but **our own** per-shot model could isolate finishing skill from
shot quality without importing an external model. The label is the raw goal
flag, so this is a first-party model and allowed.

**Per-shot model.**

- **1,025,191** unblocked shot events, 2017-18 .. 2025-26; label `goal`
  (**71,617** positives, 6.99%).
- Raw features only: `shotDistance`, `abs(shotAngle)`, `shotType` (one-hot),
  `shotRebound`, `shotRush`, `homeSkatersOnIce - awaySkatersOnIce`,
  `timeSinceLastEvent`, `period`, `shotOnEmptyNet`. No MoneyPuck `xGoal`,
  `xFroze`, `xRebound`, `xShotWasOnGoal`, `shotGoalProbability` or any other
  model output is read.
- **Fold assignment:** `model.fold_blocks` replicated exactly and verified
  equal for seeds 1 and 3. Five shuffled date blocks over the **1,053**
  challenge dates. For each fold F the classifier trains on every shot whose
  date is **not** in F and predicts the shots in F; an assertion checks the
  train/test date sets are disjoint. The 886 non-challenge dates get their own
  five date blocks, trained only on non-challenge dates, so all 1,025,191
  shots have out-of-fold xG.
- OOF per-shot **AUC 0.7692 / logloss 0.2193** (seed 1) and **0.7691 /
  0.2194** (seed 3). Sum xG 71,941 vs 71,617 goals (mean 0.0702 per shot).
  Per-shot files: `data/moneypuck/shots/shot_xg_seed{1,3}.csv`.

**Player-game features** (aggregate `xg_sum`, `xg_per_attempt`,
`goals_minus_xg`, then shift(1) per player before every window):

| feature | definition |
|---|---|
| `rs_xg_pg_5/10` | mean xG per game, prior 5 / 10 |
| `rs_xg_per_att_5/10` | sum(prior xG) / sum(prior attempts) |
| `rs_finishing_5/10` | sum(goals - xG) over prior 5 / 10 |
| `rs_xg_trend` | `rs_xg_pg_5` / season-to-date prior xG per game |

Zero-attempt games stay in the grid: 0 xG, 0 attempts, 1 game played.

**Merge / leakage checks.**

- Shot grid 427,402 player-games; 425,462 have own-xG features (first career
  games NaN). Model frame stays **427,240 rows**; no duplicate
  `(date, player_id)`, no `_x`/`_y` columns.
- `fold_blocks` output for seeds 1/3 is byte-identical to `model.fold_blocks`.
- Fold assertions passed: no fold's model trained on any of its fold dates.
- Perturbing the target row's own game xG to 9999 left that row's
  `rs_xg_pg_5` unchanged (0.27368485103188367 before and after), proving the
  current game never enters its own rolling features.

**Protocol.** LGBM config a, date-blocked k=5, `--train-subset all`,
`OMP_NUM_THREADS=2`, `LGBM_N_JOBS=2`, `timeout 300`. Baselines are the
corrected V7 base (`oof_v7_base_lgbm_seed{1,3}.csv`): seed 1 **928/3159 =
29.376%**, seed 3 **916/3159 = 28.997%**; hgpg **886 = 28.047%**.

| model | seed | baseline | + own-xG | delta | McNemar vs base | McNemar vs hgpg | AUC |
|---|---|---:|---:|---:|---|---|---:|
| LGBM | 1 | 928 = 29.376% | **923 = 29.218%** | **-0.158pp** | 158-163, p=0.823 | 338-301, p=0.154 | 0.6921 ± 0.0050 |
| LGBM | 3 | 916 = 28.997% | **905 = 28.648%** | **-0.349pp** | 147-158, p=0.567 | 313-294, p=0.465 | 0.6924 ± 0.0014 |
| LGBM mean | | 29.186% | 28.933% | -0.254pp | | | |

Per-fold: seed 1 33.3/28.0/26.5/31.1/27.1 (std 2.897); seed 3
31.3/28.0/28.8/27.6/27.6 (std 1.543). The ensemble was not run because both
LGBM seeds did not improve.

**Importances** (fold-1 LGBM, seed 1, gain rank among 49 features):

| feature | gain | rank |
|---|---:|---:|
| `rs_xg_pg_10` | 439 | 4 |
| `rs_xg_trend` | 419 | 9 |
| `rs_xg_per_att_10` | 395 | 13 |
| `rs_xg_per_att_5` | 374 | 14 |
| `rs_finishing_5` | 330 | 19 |
| `rs_finishing_10` | 293 | 20 |
| `rs_xg_pg_5` | 289 | 21 |

All seven features land in the top 21 and the per-shot model is well
calibrated (AUC 0.769), but the block still reduces pick accuracy — the signal
is either already carried by V4/V6 features or the rolling xG is too noisy at
the player-game grain.

**Verdict: reverted.** Both seeds are negative (-0.16pp, -0.35pp) and both
McNemar tests are non-significant (p=0.82/0.57 vs the same-seed baseline).
The `own_xg.py` builder and `--own-xg-features` flag were removed; the
per-shot xG files and player-game feature tables under `data/moneypuck/shots/`
were deleted in `chore/clean-tree` (owner-approved data deletion; blobs in
git history / regenerable per the V8-own-xG recipe).

### V8-shrinkage — empirical-Bayes rate features (reverted)

Eight prior-only features on the corrected 42-feature set:
`shrunk_gpg_{5,15,40}` (career goals/games shrunk to the prior league mean),
`shrunk_season_gpg_{5,15}` (season rate shrunk to `hgpg`),
`shrunk_five_gpg_{3,10}` (last-5 goals shrunk to the season rate), and
`shrunk_sh_pct_50` (career shooting shrunk to the prior league shooting rate).
League means are cumulative through the previous date; all player counts are
prefix sums excluding the current row.

Leakage/merge checks: 427,240 rows, no duplicate `(date, player_id)`, no
suffix columns; manual recomputation matched for five sampled players on
`shrunk_gpg_5`/`shrunk_season_gpg_5`/`shrunk_five_gpg_3`/`shrunk_sh_pct_50`.
Nulls: gpg/shot features 144 (first date), season blends 97,668, five-game
blends 8,355.

LGBM, k=5, train=all, seeds 1/3, 300s cap (112s/117s):

| seed | baseline hits/acc | shrinkage hits/acc | delta | vs baseline | vs hgpg |
|---|---:|---:|---:|---|---|
| 1 | 928 / 29.376% | 907 / 28.712% | **-0.665pp** | 128-149, p=0.229 | 320-299, p=0.422 |
| 3 | 916 / 28.997% | 899 / 28.458% | **-0.538pp** | 124-141, p=0.326 | 306-293, p=0.624 |

Per-fold std: seed 1 3.272, seed 3 1.651. Both seeds got worse, so the code
was reverted (feature set back to 42). LGBM gain ranks were high —
`shrunk_gpg_40` #1 (179,958; perm 0.0079), `shrunk_gpg_15` #2 (75,879; 0.0011),
`shrunk_gpg_5` #9, `shrunk_season_gpg_15` #15, `shrunk_sh_pct_50` #17,
`shrunk_season_gpg_5` #28, `shrunk_five_gpg_10` #29, `shrunk_five_gpg_3` #30 —
but the signal is redundant with the existing rates and hurt pick accuracy.

### V8-tune — LGBM hyperparameter sweep (reverted)

Config d = 600 trees / lr 0.015 / 31 leaves / min_child_samples 30 /
reg_lambda 2; config b = 600 / 0.02 / 15 / reg5. Seed 1/3, k=5, train=all,
300s cap (105-123s each; b finally completed — its earlier timeouts were CPU
contention). Reference is config a (400/0.03/31).

| config | seed 1 | seed 3 | delta vs a | McNemar vs a | per-fold std |
|---|---:|---:|---:|---|---|
| a | 928 / 29.376% | 916 / 28.997% | — | — | 2.351 / 1.315 |
| b | 934 / 29.566% | 915 / 28.965% | +0.190 / -0.032pp | 131-125 p=0.755; 129-130 p=1.0 | 2.790 / 0.750 |
| d | 926 / 29.313% | 922 / 29.186% | -0.063 / +0.190pp | 105-107 p=0.945; 106-100 p=0.728 | 2.916 / 0.428 |

Verdict: neither config beats a on **both** seeds by +0.5pp; each is mixed
(b/d win one seed by ~0.19pp and lose the other). Defaults unchanged
(config a; ensemble default). Config d was reverted, notes only.

### V9-projected-lineups — prior-games lineup projector (reverted)

Stage-1 module: `smartscore/scripts/backtrack/lineup_project.py` (deleted in
`chore/clean-tree`; blob at `c82b2e7`) writes
`data/ml/projected_lineups.csv` (427,240 rows) in 15.9s, with no `model.py`
change. It emits methods a/b/c plus per-method confidence columns.

Projector uses only games before the target date: (a) last prior unit,
(b) exponentially weighted mode over the last 10 games, (c) team-constrained
greedy assignment into F1-F4 / D1-D3 / PP1-PP2 slots. Model features use (c)
plus confidence (share of last 5 at the projected unit) and linemate quality
(mean prior gpg/ppg of line/PP mates, line_intact). Candidate players per
team-date are the archive's dressed roster; a live run would use the projected
roster, but no current-game unit label enters any feature.

Projection match vs ACTUAL units (rows with actual non-null):

| position | method a | method b | method c |
|---|---:|---:|---:|
| Forwards, unit | 58.11% | 59.63% | 59.44% |
| Defence, unit | 61.03% | 63.52% | 63.41% |
| PP unit | 67.04% | 76.43% | 70.98% |

By season (method c): F 59.4 / 60.6 / 58.6 / 56.0 / 58.3 / 59.7 / 60.8 /
60.5 / 60.0%; D 63.7 / 61.7 / 61.9 / 63.3 / 63.6 / 62.6 / 63.4 / 65.4 / 65.0%;
PP 66.3 / 69.1 / 69.1 / 66.7 / 69.6 / 74.0 / 74.2 / 74.5 / 74.0% for
2017-18 through 2025-26. Method b was best overall (F 59.63, D 63.52,
PP 76.43).

Model results (k=5, train=all; baselines no-lineup LGBM 29.376/28.997,
ensemble 29.915/29.408/29.820):

| run | seed | hits / 3159 | acc | delta | McNemar vs baseline | per-fold std |
|---|---:|---:|---:|---:|---|---:|
| LGBM projected | 1 | 916 | 28.997% | -0.380 | 176-164, p=0.551 | 3.354 |
| LGBM projected | 3 | 908 | 28.743% | -0.253 | 167-159, p=0.698 | 1.362 |
| LGBM oracle | 1 | 967 | 30.611% | +1.235 | 178-217, p=0.056 | 3.298 |
| LGBM oracle | 3 | 947 | 29.978% | +0.981 | 163-194, p=0.112 | 0.532 |
| ensemble projected | 0 | 931 | 29.471% | -0.444 | 171-156, p=0.439 | 2.044 |
| ensemble projected | 1 | 920 | 29.123% | -0.285 | 169-160, p=0.659 | 3.219 |
| ensemble projected | 3 | 918 | 29.060% | -0.760 | 167-146, p=0.258 | 1.155 |

(OOF re-picks for the ensemble differ by 1-2 ties from the logged totals.)
Projected lineups hurt every model/seed; the oracle shows the information is
worth ~+1.0 to +1.2pp, but projection accuracy (~60% line, ~71% PP for method
c) is too low. Reverted; feature set back to 42, notes only.

**Stage 2 — method b re-evaluation.** Used method b (best: F 59.63%, PP
76.43%) plus `proj_line_conf`, `proj_pp_conf`, prior-only linemate means and
`line_intact`. Merge checks: 427,240 rows, no duplicate/suffixed columns;
nulls: line unit/conf 1,902, PP unit/conf 75,491, linemate means 17,040,
PP-mate mean 80,090, line_intact 0. All inputs are prior games only.

| variant | seed 1 | seed 3 |
|---|---:|---:|
| no-lineup LGBM | 928 / 29.376% | 916 / 28.997% |
| projected method b | **959 / 30.358%** | 908 / 28.743% |
| projected delta | **+0.981pp** | -0.253pp |
| projected McNemar | 163-132, p=0.081 | 155-163, p=0.695 |
| projected per-fold std | 3.055 | 1.160 |
| oracle (actual units) | 967 / 30.611% | 947 / 29.978% |
| oracle delta | +1.235pp | +0.981pp |
| oracle McNemar | 217-178, p=0.056 | 194-163, p=0.112 |

Projected feature importances (fold-1 LGBM gain rank/50; permutation AUC):
`proj_lineup_unit_b` #2 (145,540; 0.0118), `proj_pp_unit_b` #12 (5,827;
0.0017), `proj_pp_conf` #13 (5,719; 0.0007), `pp_unit_prior_ppg_mean` #14
(5,416; 0.0016), `linemate_prior_gpg_mean` #20 (4,168; 0.0006),
`linemate_prior_ppg_mean` #25 (3,124; -0.0008), `proj_line_conf` #40 (967;
0.0003), `line_intact` #45 (270; -0.0002).

Verdict: **mixed, not promoted**. Method b reaches 30.36% on seed 1
(+0.98pp, p=0.081) but loses 0.25pp on seed 3; the oracle confirms lineup
information is worth ~+1.0 to +1.2pp, but projection quality is not reliable
enough. `--lineup projected` is retained as an opt-in; default remains
`--lineup none` and the rank ensemble.

**Stage 3 — probabilistic lineups.** Prior-only decayed shares over the last
10 games for F1-F4, D1-D3 and PP1-PP2, plus expected line quality, expected
linemate gpg/ppg and PP expected ppg. No hard labels; all from prior games.

Trio-continuity projector (most common prior trios/pairs over the last 10,
then method b for unassigned): **F 48.66%, D 55.56%** (coverage 81.0%/85.5%),
which does **not** beat method b (59.6%/63.5%).

| variant | seed 1 | seed 3 |
|---|---:|---:|
| no-lineup LGBM | 928 / 29.376% | 916 / 28.997% |
| hard projected method b | 959 / 30.358% | 908 / 28.743% |
| probabilistic | 915 / 28.965% | 916 / 28.997% |
| probabilistic delta | **-0.412pp** | **0.000pp** |
| probabilistic McNemar | 172-185, p=0.525 | 169-169, p=1.0 |
| probabilistic per-fold std | 2.656 | 1.140 |
| oracle (actual units) | 967 / 30.611% | 947 / 29.978% |

Probabilistic feature importances (fold-1 LGBM gain rank/55; permutation AUC):
`expected_line_quality` #2 (33,808; 0.0028), `f1_prob` #3 (26,850; 0.0011),
`d3_prob` #6 (12,934; -0.0019), `f2_prob` #8 (11,279; 0.0009), `pp1_prob` #10
(10,283; 0.0036), `pp_unit_expected_ppg` #12 (9,461; -0.0008),
`expected_linemate_gpg` #21, `expected_linemate_ppg` #24, `f3_prob` #26,
`f4_prob` #28, `pp2_prob` #29, `d2_prob` #31, `d1_prob` #46.

Verdict: **reverted**. Probabilistic features lose 0.41pp on seed 1 and tie
seed 3; the condition to run the ensemble was not met. The projector functions
remain deleted in `chore/clean-tree` (blob at `c82b2e7`); `model.py` keeps the stage-2
`--lineup projected` opt-in only and defaults to no lineup.

**Stage 4 — rank ensemble with lineup features.** Built the fixed equal-weight
rank ensemble from component OOFs (HGB+logreg run + the stage-2 projected/oracle
LGBM OOFs; the end-to-end 3-model run hit the 300s cap at fold 5). Baselines:
no-lineup ensemble 29.408% (seed 1) and 29.820% (seed 3).

| variant | seed 1 | seed 3 |
|---|---:|---:|
| no-lineup ensemble | 929 / 29.408% | 942 / 29.820% |
| projected ensemble | 943 / 29.851% | 935 / 29.598% |
| projected delta | **+0.443pp** | **-0.222pp** |
| projected per-fold std | 3.186 | 0.667 |
| projected McNemar vs no-lineup | 165-151, p=0.465 | 162-168, p=0.783 |
| projected McNemar vs hgpg | 342-285, p=0.025 | 323-274, p=0.049 |
| oracle ensemble | 924 / 29.250% | 954 / 30.199% |
| oracle delta | -0.158pp | +0.379pp |
| oracle per-fold std | 3.171 | 0.805 |
| oracle McNemar vs hgpg | 328-290, p=0.137 | 331-263, p=0.006 |

Projected ensemble mean over seeds 1/3 = 29.72%, same as the no-lineup 29.71%
mean; it does not reach 30.0% on both seeds and is not better on both. **Default
stays `--lineup none`.** Oracle mean is also 29.72% (29.25/30.20), showing the
crude projector is not the only limiter; real morning projections should sit
between the crude projector and the oracle, but that is not verifiable here.

**Stage 5 — final numbers.** Oracle LGBM seed 2 = **960/3159 = 30.389%**
(123s). Four-seed oracle mean: **30.334% ± 0.263** (30.358 / 30.611 / 30.389 /
29.978 for seeds 0/1/2/3).

End-to-end projected ensemble seed 1 with `timeout 600` completed in **328s**:
**944/3159 = 29.883%**, +0.475pp vs the 29.408% no-lineup ensemble; McNemar
vs no-lineup 166-151, p=0.432; vs hgpg 343-285, p=0.023. Projected ensemble
across available seeds: 29.883 (seed 1 end-to-end) and 29.598 (seed 3),
mean **29.74%**. Not a clear win on the same seed (p=0.43), so the default
stays `--lineup none`. Deployment estimate with real morning projections:
roughly **29.7-30.3%**, between the crude projected ensemble and oracle.

### V10 — real projected lineups are not archived

Inventory of `Player-Snapshots-prod` (Supabase, 2026-10-20): 143,935 total
rows; `lineup_status` non-null on 5,155 rows, **all `UNKNOWN`**, covering
17 dates (2026-10-04 .. 2026-10-20) and 745 players. Counts for
`PROJECTED` / `CONFIRMED` / `ACTUAL` are **0**; `lineup_unit` non-null **0**
and `pp_unit` non-null **0**. Mongo prod `players.SmartScore` (139,731 docs)
agrees: 138,949 null status, 782 `UNKNOWN`, **0 PROJECTED**, 0 non-null units;
dev is the same pattern.

Real projection accuracy therefore has **n=0**: no archived PROJECTED rows and
no overlap with backtrack actual lineups (backtrack ends 2026-06-14; the
UNKNOWN prod rows start 2026-10-04). The live simulation cannot run — fewer
than 300 projected picks, in fact zero — so no match-rate, delta or McNemar
numbers exist. Our prior-game projector cannot be scored on those dates
either. The owner’s projection feed is live-only; forward-collect PROJECTED
rows with units before V10 can be evaluated. V9’s “unverifiable historically”
caveat stands.

### V11 — projection-fidelity sensitivity curve

Mixed lineup labels per row: actual with probability q, else method-b
projected; lineup-aware LGBM trained and evaluated at the same q, k=5,
train=all. Fidelity is the realized mixed-label match vs actual on the
challenge rows.

| input | unit fidelity | PP fidelity | seed 1 | seed 3 |
|---|---:|---:|---:|---:|
| no-lineup | — | — | 29.376% | 28.997% |
| V9 stage-2 projected features | ~59.6% | ~76.4% | 30.358% | 28.743% |
| V11 q=0 (mixed-cat) | 61.6% | 80.8% | 28.965% | 28.490% |
| V11 q=0.5 | 81.0% | 90.3% | 29.250% | 28.933% |
| V11 q=0.75 | 90.3% | 95.2% | 29.535% | 29.376% |
| oracle q=1 | 100% | 100% | 30.611% | 29.978% |

Per-fold std: q=0 2.551/1.622; q=0.5 1.957/1.049; q=0.75 2.254/0.960;
oracle 3.298/0.532. The V9 stage-2 projected row uses projected features
(confidence/linemate) and is not directly comparable to the mixed-categorical
curve; it is included because it is the known ~60% reference.

No mixed fidelity reaches **30.0% on both seeds**: the oracle itself is
**29.978%** on seed 3, just under 30, and the best sub-oracle point (q=0.75)
is 29.535/29.376. Seed 1 crosses 30 only at q=1. Real morning projections
likely sit at ~80-90% unit fidelity, implying roughly **29.25-29.54%** on
seeds 1/3 — below 30 unless the feed is essentially oracle-accurate or the
model improves.

### V12 — player/team identity + diagnostics (rejected)

Added `player_id` and `team_abbrev` as regularised LGBM categoricals
(config e: min_data_per_group 200, cat_smooth 50, max_cat_threshold 32), plus
age and age² from NHL landing birthdates (1,891/1,891 cached) and prior career
games (`career_games`, already in the 42-feature set).

| variant | seed 1 | seed 3 |
|---|---:|---:|
| no-lineup baseline | 928 / 29.376% | 916 / 28.997% |
| identity | 881 / 27.889% | 907 / 28.712% |
| delta | **-1.487pp** | **-0.285pp** |
| McNemar | 270-317, p=0.058 | 273-282, p=0.734 |
| per-fold std | 1.573 | 1.327 |

Identity importances (fold-1 gain rank/46; permutation AUC): `player_id` #2
(174,496; 0.0246), `team_abbrev` #10 (7,669; 0.0008), `career_games` #17
(3,241; -0.0003), `age` #21 (2,538; 0.0028), `age_sq` #40 (504; 0.0002).
`player_id` is heavily used but hurts out-of-fold picks; reverted.

**Diagnostics (baseline no-lineup LGBM, seeds 1+3):**
- Per tims group: g1 39.89% (420/1053), g2 27.16% (286/1053), g3 20.89%
  (220/1053).
- Per season: 2021-22 27.64% (492), 2022-23 28.53% (666), 2023-24 28.88%
  (696), 2024-25 29.91% (672), 2025-26 31.28% (633).
- Calibration: mean max predicted probability **0.3154** vs realized
  accuracy **29.31%** (overconfident ~2.2pp); by group maxp/acc g1
  0.4377/39.89%, g2 0.2960/27.16%, g3 0.2123/20.89%.
- Top pick equals the hgpg leader in **1,531/3,159 = 48.5%** of groups
  (g1 46.9%, g2 45.2%, g3 53.3%).

### V12-era — recent-era deployment backtest (full-era kept)

Added `--test-start` and `--min-season`; training restricted to seasons
2021-22+. Two windows, LGBM and default ensemble, challenge picks only.

| window | model | recent-era | full-era seed 1 | full-era seed 3 | hgpg | gpg |
|---|---|---:|---:|---:|---:|---:|
| train <2024-08-01, test 2024-25+25-26 (n=1305, 435 dates) | LGBM | 388 / 29.732% | 406 / 31.111% | 398 / 30.498% | 396 / 30.345% | 376 / 28.812% |
| same | ensemble | 395 / 30.268% | 391 / 29.962% | 403 / 30.881% | 396 / 30.345% | 376 / 28.812% |
| train <2025-08-01, test 2025-26 (n=633, 211 dates) | LGBM | 195 / 30.806% | 200 / 31.596% | 202 / 31.912% | 192 / 30.332% | 179 / 28.278% |
| same | ensemble | 200 / 31.596% | 191 / 30.174% | 208 / 32.859% | 192 / 30.332% | 179 / 28.278% |

Against the mean full-era k-fold on identical dates: A LGBM -1.07pp,
A ensemble -0.15pp; B LGBM -0.95pp, B ensemble +0.08pp. No model beats the
full-era benchmark by >= +0.5pp consistently, so **full-era training remains
the recommended default**; the flags are retained for future backtests.

Recent-era per-season: A LGBM 28.87/30.65 (2024-25/2025-26), A ensemble
28.87/31.75; B both only 2025-26 (LGBM 30.81, ensemble 31.60).
Per tims group: A LGBM 37.01/30.11/22.07, A ensemble 36.78/31.72/22.30;
B LGBM 32.23/31.75/28.44, B ensemble 34.60/30.81/29.38.
### V12-qoc — quality of competition/teammates from shift charts (reverted)

Hypothesis: the existing player rates ignore who a player skated against and
with. The raw shift charts can produce prior-game QoC/QoT and, if zone starts
existed, usage context.

**Data.** `shifts` in `data/raw_nhl.sqlite`: 9,125,547 shift rows, 2017-18 ..
2025-26, one row per player-period shift (`start_time`/`end_time`). Joined to
`player_games` for club/position/date; goalies excluded. For each game and
period the shifts were rasterised to a player × second on-ice matrix, giving
overlap seconds for every player pair.

**Zone starts are not available.** The raw store has no play-by-play/faceoff
event table and no zone-start column in `shifts` or the boxscore JSON, so
`ozs_pct` was skipped.

**Per-game metrics** (rate = prior-season goals/60 or points/60 from
`player_games`; rookies/no prior season → NaN):

| metric | definition |
|---|---|
| `qoc_g60`, `qoc_p60` | overlap-weighted mean of opponents' prior-season goals/60, points/60 |
| `qot_g60`, `qot_p60` | same for teammates (self excluded) |
| `*_rel` | subtract the team-game minutes-weighted mean (team-relative) |

That is 8 per-game metrics. Every model feature is shifted one row per player:
`r5_*`, `r10_*` and `sd_*` (season-to-date prior mean over valid games), 24
columns total.

**Checks.** 425,176 player-games in the QoC cache; merge keeps the frame at
**427,240 rows**, no `_x`/`_y`, no duplicate keys. `r5_qoc_g60` is non-null on
374,516 rows (87.7%; 2017-18 has no prior-season rates). Three player-games
were recomputed from raw shifts and prior-season totals and matched exactly
(e.g. 8479981 2021-12-28 1.903186 vs 1.903186; 8478403 2023-04-27 0.520029;
8480835 2023-03-14 0.121633). A rolling check matched the prior-5 mean to
1e-16, and setting the target game's QoC to 9999 left its `r5_qoc_g60`
unchanged.

**Protocol.** LGBM config a, date-blocked k=5, `--train-subset all`,
`OMP_NUM_THREADS=2`, `LGBM_N_JOBS=2`, `timeout 300`. Baselines are the corrected
V7 base (`oof_v7_base_lgbm_seed{1,3}.csv`): seed 1 **928/3159 = 29.376%**,
seed 3 **916/3159 = 28.997%**; hgpg **886 = 28.047%**.

| model | seed | baseline | + QoC/QoT | delta | McNemar vs base | McNemar vs hgpg | AUC |
|---|---|---:|---:|---:|---|---|---:|
| LGBM | 1 | 928 = 29.376% | **944 = 29.883%** | **+0.507pp** | 176-160, p=0.413 | 348-290, p=0.024 | 0.6931 ± 0.0049 |
| LGBM | 3 | 916 = 28.997% | **917 = 29.028%** | **+0.032pp** | 167-166, p=1.000 | 318-287, p=0.223 | 0.6928 ± 0.0019 |
| LGBM mean | | 29.186% | 29.455% | +0.269pp | | | |

Per-fold: seed 1 33.6/27.2/25.3/33.2/30.2 (std 3.663); seed 3
30.2/28.4/28.6/29.0/28.9 (std 0.684). The ensemble was not run because both
LGBM seeds did not clear +0.5pp.

**Importances** (fold-1 LGBM, seed 1, gain rank among 66 features):

| feature | gain | rank |
|---|---:|---:|
| `r5_qot_p60_rel` | 302 | 10 |
| `r5_qot_p60` | 225 | 17 |
| `r10_qot_g60_rel` | 221 | 18 |
| `sd_qoc_g60_rel` | 218 | 19 |
| `r5_qoc_g60_rel` | 217 | 20 |
| `r10_qot_p60` | 214 | 21 |
| `sd_qoc_g60` | 210 | 22 |
| `r5_qoc_g60` | 206 | 23 |

The teammate-relative block is used most, but seed 3's gain is only +0.03pp
and its per-fold spread collapses to 0.684.

**Verdict: reverted.** Seed 1 clears +0.5pp by 0.007pp (p=0.41 vs baseline),
seed 3 is flat (+0.03pp, p=1.0), so the both-seeds rule fails. The `qoc.py`
builder and `--qoc-features` flag were removed; the QoC cache in
`data/moneypuck/qoc_player_game.csv` was deleted in `chore/clean-tree`.

### V12-interactions — explicit tims-group interactions (reverted)

Added nine numeric interactions: `tims` (1/2/3; 0 for non-challenge) times
gpg, hgpg, five_gpg, ppg, rank_gpg, rank_hgpg, games_since_goal,
days_since_game, club_rest_days. Verified 427,240 rows, no duplicate/suffixed
columns, all inputs pre-game.

| seed | baseline | interactions | delta | McNemar vs base | vs hgpg | per-fold std |
|---|---:|---:|---:|---|---:|---:|
| 1 | 928 / 29.376% | 931 / 29.471% | +0.095pp | 138-135, p=0.904 | 337-292, p=0.079 | 2.784 |
| 3 | 916 / 28.997% | 916 / 28.997% | 0.000pp | 140-140, p=1.0 | 326-296, p=0.245 | 0.736 |

Per-group (combined seeds): baseline g1 39.89 / g2 27.16 / g3 20.89;
interactions g1 39.98 / g2 27.40 / g3 20.32 — **group 3 got worse by 0.57pp**.
Interaction importances were low (fold-1 gain rank/51: tims_x_rank_hgpg #29,
tims_x_rank_gpg #33, tims_x_hgpg #34, tims_x_gpg #36, tims_x_ppg #40,
tims_x_games_since_goal #43, tims_x_days_since_game #45,
tims_x_club_rest_days #47, tims_x_five_gpg #50).

Verdict: **reverted**. No overall +0.5pp on both seeds and the group-3 rule
failed; `--interactions` code removed.

### V14 — Tims-era re-evaluation and pre-2017 feasibility

**True Tims start:** earliest date with `tims in {1,2,3}` is **2021-12-09**,
not 2023. There are **243 pre-2023 challenge dates** (729 picks); the owner’s
2023 claim is wrong. Challenge dates/picks by season:

| season | dates | picks |
|---|---:|---:|
| 2021-22 | 164 | 492 |
| 2022-23 | 222 | 666 |
| 2023-24 | 232 | 696 |
| 2024-25 | 224 | 672 |
| 2025-26 | 211 | 633 |
| **total** | **1,053** | **3,159** |

**All Tims-era challenge dates (n=3,159):** no-lineup ensemble seeds 0/1/3 =
**29.946 / 29.408 / 29.788%**, mean **29.714%**; LGBM seeds 0/1/3 =
29.060 / 29.376 / 28.997%, mean **29.144%**; hgpg **28.047%**; gpg **26.496%**.
**Strict 2023-24+ (n=2,001, 667 dates):** ensemble **29.868%** mean
(29.735 / 29.835 / 30.035), LGBM **29.835%** mean (29.685 / 30.285 / 29.535),
hgpg 28.136%, gpg 27.086%. The owner-facing Tims-era headline is therefore
**~29.7% all challenge dates, ~29.9% strict 2023+** — below 30%.

| season | ensemble mean | LGBM mean | hgpg | gpg |
|---|---:|---:|---:|---:|
| 2021-22 | 28.32% | 27.24% | 24.80% | 22.97% |
| 2022-23 | 30.28% | 28.48% | 30.18% | 27.33% |
| 2023-24 | 29.17% | 28.54% | 23.99% | 23.85% |
| 2024-25 | 29.41% | 29.86% | 30.36% | 29.32% |
| 2025-26 | 31.12% | 31.23% | 30.33% | 28.28% |

**Training strategy on identical 2023-24+ dates** (LGBM seed 1, k=5 over the
667 test dates): all seasons **614/2001 = 30.685%** vs train-2023+ only
**590/2001 = 29.485%**; all seasons +1.200pp, McNemar 159-135, p=0.180.
Full-era training wins; keep it.

**Pre-2017 feasibility (investigation only, no scrape):** the endpoints serve
older seasons — one 2013-14 game log returned 78 games, `schedule/2013-10-01`
returned a 7-day window, and shiftcharts for game 2013021223 returned 730 rows.
Per season the pipeline would use `local_store.py --build/--derive/--lineups/
--feeds/--publish` (or `reconstruct.py --season <season> --write`) plus
`dump_store.py --export --season`. Four seasons (2013-14..2016-17) are roughly
190k extra player-games (+45% over 427k), ~0.7GB cache + ~0.7GB raw DB, and
~10k requests — about 1-2h at the 0.34s throttle plus parsing. They carry
`scored` labels and all feature inputs, but **no Tims groups before 2021-12**
(tims=0), so they can only augment pointwise training, not challenge
evaluation; era/style drift makes the expected value uncertain.

### V14b — pre-2017 runbook

Full copy-paste runbook: `docs/backtrack_pre2017_runbook.md` — deleted in
`chore/clean-tree`; blob at `c82b2e7` (commands preserved below). Entry points were
verified with `--help`; one 2013-14 game log, schedule window, and shiftchart
call returned data. Per season:

```bash
export ENV=dev
for SEASON in 20132014 20142015 20152016 20162017; do
  uv run python smartscore/scripts/backtrack/local_store.py --build "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --derive "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --lineups "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --feeds "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --publish "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --publish-team "$SEASON"
  uv run python smartscore/scripts/backtrack/local_store.py --publish-goalies "$SEASON"
  uv run python smartscore/scripts/backtrack/dump_store.py --export --season "$SEASON"
done
```

Expect ~55k player-games, ~13 MB dump, ~0.18 GB cache + ~0.18 GB raw DB per
season, and 30-45 min/season (2-3h total). Verification SQL (row counts, date
range, `scored` sanity, feature-null rates, join integrity) and the model
refresh/evaluation commands are in the runbook. Blocker: pre-2021 rows have
**no Tims groups** (`tims=0`), so they are pointwise training rows only; verify
`pre-2021 rows with tims group = 0` and keep `rows in tims groups = 92,440`.

### V15 — augmented pre-2017 training (not adopted)

`Player-Snapshots-backtrack-dev` now has **617,310 rows** (427,240 + 190,070
published 2013-17 skaters), dates **2013-10-01 .. 2026-06-14**; team cache
34,300 and goalie cache 36,696 rows. Total scored rows **617,310**; per-fold
train size **567,430–572,421** (mean **570,391**).

`--refresh` failed on `Player-Snapshots-backtrack-dev` with PostgREST
`57014 canceling statement due to statement timeout` after **254s** (ordered
offset pagination at 617k rows). Cache was refreshed with date-windowed REST
queries instead. **Follow-up:** fix `fetch_table` pagination (keyset pagination
or per-season/date windows) rather than increasing the statement timeout.

Augmented LGBM: seed 1 **931/3159 = 29.471%** (+0.095pp vs `oof_v6_baseline`
29.376%); seed 3 **929/3159 = 29.408%** (+0.412pp vs 28.997%); mean +0.253pp,
neither McNemar significant (p=0.91 / 0.52).

Augmented ensemble (end-to-end, timeout 420): seed 0 **933/3159 = 29.535%**
(−0.380pp vs 29.915%); seed 3 **935/3159 = 29.598%** (−0.222pp vs 29.820%).
Both lower overall, so seed 1 was not run.

Restricted metrics (complete component-OOF reconstruction; ±1–3 picks vs the
end-to-end runs):
- seed 0: 2023-24+ **608/2001 = 30.385%** (+0.650pp vs pre 29.735%); 2025-26
  **200/633 = 31.596%** (+1.264pp vs 30.332%).
- seed 3: 2023-24+ **598/2001 = 29.885%** (−0.150pp vs 30.035%); 2025-26
  **195/633 = 30.806%** (−2.054pp vs 32.859%).

Verdict: **do not adopt** the augmented training set for the ensemble; overall
accuracy is slightly worse and the recent-era seed-0 gain is not consistent
across seeds. Keep the 2017+ training default.

### V15b — pre-2017 dumps + refresh pagination fix

Committed the four store dumps (`9c9f315`):

| dump | bytes |
|---|---:|
| 20132014.sql.gz | 11,913,769 |
| 20142015.sql.gz | 11,932,704 |
| 20152016.sql.gz | 11,957,819 |
| 20162017.sql.gz | 11,855,755 |

Plain git (no LFS; `.gitattributes` only sets the gzip diff driver, matching
the existing 2017-2021 dumps).

`fetch_table` refresh now fetches **monthly date windows** via
`_month_windows` + `_fetch_window` instead of one 617k-row ordered range,
which tripped PostgREST `57014 statement timeout`. 153 windows cover
2013-10-01..2026-06-14. Verification on the 2016-10-01..2016-11-01 player
window: **4,680 rows in 1.72s**, columns identical to cache, key diff 0/0.
CSV format is unchanged; model behavior is unchanged. Full refresh was not
rerun (the chunked cache already has 617,310 rows).

### V16- collect: projected-lineup persistence

Shipped `smartscore/scripts/backtrack/snapshot_projected.py` (worktree
`SmartScore-backtrack`, branch `backtrack-recon`): a standalone collector that
reads the two public pre-game lineup sources the live pipeline reads (NHL.com's
daily lineup-projections article and RotoWire), matches both onto that date's
NHL schedule, resolves every player to an NHL id via `roster/{team}/current`,
and writes `Player-Snapshots-{ENV}` rows with
`lineup_status='PROJECTED'`, `lineup_unit` (F1-F4 **and** D1-D3; the live
product's merge nulls defence), `lineup_position_group` and `pp_unit`
(normalized to PP1/PP2; the pipeline persists RotoWire's verbatim
"POWER PLAY #1/#2" - both vocabularies now exist side-by-side in prod).

Writes are idempotent on the `(date, player_id)` PK, in two modes:

- `append` - inserts only pairs not yet stored (`Prefer: resolution=ignore-duplicates`); an existing row is never merged or overwritten.
- `refresh` - PATCHes **only the four lineup columns** on stored rows whose
  `lineup_status` is null or `UNKNOWN` (i.e. a source failure stamp). Rows with
  PROJECTED/CONFIRMED/ACTUAL are never touched, and no stats/scored column is ever in the batch.

Safety gates: the article `datePublished` must equal the requested game date
(ET) or writes are refused (prevents filing yesterday's lineups under today,
and makes the pre-publish morning runs collect nothing instead of wrong data);
`--env prod` is explicit; `--limit` and `--dry-run` are available.

**Audited state (independent leakage/number audit, 2026-10-22 — notes were deleted in `chore/clean-tree`, blob at `c82b2e7`; live Supabase check):** on **prod** as
of 2026-10-20 there are **255 PROJECTED / 240 UNKNOWN** rows with **0 D-unit
rows and 0 PP1** — the live pipeline's own signature (forwards only, verbatim
"POWER PLAY #1"). The collector's post-state (PROJECTED rows carrying D units:
**dev: 128 D / 22 PP1**; dev 390 PROJECTED / 105 UNKNOWN) appears on **dev
only**. The prod-write claim printed earlier in this section (UNKNOWN
4,900→4,765, PROJECTED 255→390 "incl. defence") was that **dev** post-state.

Known source noise (stored verbatim): the article occasionally numbers extra
defence pairs (label "D4" for a 4th pair) and a handful of projected players
are not on the current roster feed (logged, skipped). PP labels are mixed
vocabulary until historical rows are rewritten (owner decision).

FIXED 2026-10-22 (`d0ee9b9`): `snapshot_projected.py` now accumulates rows
with units. Prod after first runs: **540 rows — 525 with `lineup_unit`**
(D units 180, PP1 139, PP2 237), statuses **525 `PROJECTED_PROXY`**
(prior-game proxy units, provenance-flagged) / **2 `PROJECTED`** (official
article F/D + RotoWire PP) / **13 `UNKNOWN`** (no deployable history);
idempotency verified (second run: 0 writes). Stats columns untouched
(gpg/hgpg non-null on 539/540). Runbook: `PROJECTED_LINEUP_RUNBOOK.md`
(correct worktree path, PATH fix, cron + systemd-timer, 10:30/13:00/16:00 ET,
verification query).
Limits: proxy units come from last-season final deployments until fresher
shifts accumulate; on article-lag days RotoWire identity+PP still collects
with proxy flags; PP3/PP4 proxy rows surface verbatim under PROJECTED_PROXY
and the model boundary may need clamping. **Prospective projected-lineup
validation can now begin:** watch fidelity vs actuals over the next 2-3
weeks per the V11 sensitivity (81% fidelity ~ +0.1-0.3pp, 100% ~ +1.2pp);
adopt only if fidelity >= 90% AND paired tests beat the no-lineup ensemble
on both seeds.

Health check: PROJECTED dates accumulating forward under this heading; 0 new
dates on successive game days means the sources stopped (article moved hosts
or slug changed) and the collector refuses to write by design.

### Legacy experiment code — deleted in chore/clean-tree

Every rejected arm above was reproducible from a script this cleanup
**deleted**; each line below is therefore: *code deleted in
`chore/clean-tree`; see commit `c82b2e7` in history* (`git show c82b2e7:<path>`
for the exact file, the `notes_v16_*` run notebooks, and the tracked
`data/ml/` experiment CSVs; `score_oof.py`/`score_embed_oof.py` re-scorers
were untracked and are gone).

Command shape for every arm: `OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with
lightgbm==4.7.0 python smartscore/scripts/backtrack/<script> --env dev
--seed {1,3}`:

- V16-exposure: `exp_exposure.py` — self-contained (base + exposure arms, ensemble + LGBM). Code deleted in chore/clean-tree; see commit c82b2e7 in history.
- V16-group3: `exp_group3.py --oof-file data/ml/oof_v16_group3_seed{1,3}.csv` (w2/w4/g3blend/g3pure vs the paired base arm; `--train-min-date 2017-10-01` default; frame pickle `data/ml/v16_group3_frame.pkl`). Code deleted in chore/clean-tree; see commit c82b2e7 in history.
- V16-pbp: `fetch_pbp.py --env dev` (builds `data/ml/pbp_shots.csv` / `pbp_player_game.csv`, 608,813 / 279,659 rows from 6,993 games) then `exp_pbp.py --seed {1,3} --with-pbp` (base arm = same command without `--with-pbp`). Code deleted in chore/clean-tree; see commit c82b2e7 in history.
- V16-embed (INVALID / retry spec): `exp_embed.py --seed {1,3} --splits 5 --models lgbm --save-oof data/ml/oof_v16_embed_lgbm_seed{1,3}.csv` — retried only with strictly prior games per row (expanding window), never season cells. Code deleted in chore/clean-tree; see commit c82b2e7 in history.
- V16-calib: regenerate the current-cache ensemble OOF via `model.py --model ensemble --seed S --split kfold --splits 5 --train-subset all --model-file data/ml/oof_v16_calib_ens_seedS.csv`, then `exp_calib.py --mode threshold-oof --oof-tag v16_calib_ens --seed S`. Code deleted in chore/clean-tree; see commit c82b2e7 in history.
- V16-interact: `exp_interact.py --seed {1,3}` (20 interaction columns on the standard frame). Code deleted in chore/clean-tree; see commit c82b2e7 in history.
- V6-era leakage audit: `docs/backtrack_audit.py` (pick-audit, reproduced the 30.36% claim and flagged same-game lineup leakage) + `docs/backtrack_ablation.py` (LGBM ablation dropping lookahead `lineup_unit`/`pp_unit`). Code deleted in chore/clean-tree; see commit c82b2e7 in history.


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

## Deployment / artifact / vintage pointers

The model production chain lives outside this flat tree:

- **Predict/pass runner:** the daily ML picks pass runs as the scheduled
  `.github/workflows/ml-picks.yml` workflow; `docs/ml-picks-runbook.md` is
  its runbook (artifact refresh, secrets, cost, stale roll-forward).
  Per-game kick: pipeline state machine KickMLPicks (post-PublishToDb) ->
  MLPicksKick-{ENV} -> repository_dispatch (race fix: serialized after the
  write path); pipeline runtime: GitHub Actions; runbook: ml-picks-runbook.md.
- **Column spec:** `docs/supabase_shot_columns.md` on `feat/supabase-spec`
  (PR #138) is the in-force 16-column Player-Snapshots-backtrack spec
  referenced by the run code — kept on that branch, not folded here.
- **Operational runbooks in this tree:** `PROJECTED_LINEUP_RUNBOOK.md`
  (lineup-unit collection), `FEED_FIDELITY_RUNBOOK.md` (nightly unit-match
  tracking), and the per-season data-capture flow documented via the
  `backtrack-*` Makefile targets; `DAILY_DATA_RUNBOOK.md` lives on the
  `feat/daily-data-runbook` branch.
- **Data lineage:** the raw dumps under `data/dumps/` are the committed
  source of truth for training (V15b dumps `9c9f315`; monthly-window cache
  refresh after the 57014 fix).

## Next actions

- Watch projected-lineup fidelity accumulate via `fidelity_tracker.py`
  (PR #137 tracker); adopt lineup features only at >= 90% fidelity AND
  paired beats on both seeds.
- No market-odds collection (owner rule).

## Pre-cleanup watch list (verbatim next-steps items)

- **V6 correction:** `lineup_unit`/`pp_unit` are same-game shift-chart
  lookahead and are removed from all models. Prior HGB/LGBM headlines that
  used them (V2-V5; V4 HGB 30.073/30.421, LGBM 30.36) are invalid as
  deployment estimates.
- Corrected seed-0 headline: HGB 29.028%, logreg 29.060%, LGBM 29.060%,
  rank ensemble 29.851%; default is now `--model ensemble`
  (hgb+logreg+lgbm mean percentile rank).
- Ensemble seeds 0/1/3 mean 29.714 ± 0.277; +0.570pp vs mean corrected
  LGBM but seed 1 is a tie (29.408 vs 29.376). Run the seasons split before
  external claims.
- Injury fields are 100% null on the 92,440 challenge rows; no filter/feature
  is worth testing.
- Lagged lineup/PP replacements were leak-free but added only +0.095pp and
  were reverted; do not re-add without a new idea.
- Group 3 remains the weak spot (corrected LGBM g3 ~20%); per-group
  calibration or group-specific interactions are the obvious next lever.
- V3b/V4 calendar/schedule/goalie features (day_of_season, month,
  rel_goalie_strength, schedule_density) did not help; do not re-add.
- V3h player-vs-opponent history (opp_gpg_2y / opp_gpg_career /
  opp_goals_last3 / team_vs_opp_gpg_2y) did not help (-0.60pp seed 1,
  +0.19pp seed 3) and was reverted. Do not re-try these four as-is;
  head-to-head form is evidently covered by the existing rates.
- V8 empirical-Bayes shrinkage rates hurt both seeds (-0.67 / -0.54pp) and
  were reverted; the information is already in the existing rates.
- V8-tune configs b/d were mixed (+0.19pp on one seed, negative/low on the
  other); config a stays the reference.
- V9 lineup work: hard b +0.98/-0.25pp, probabilistic -0.41/0.00pp,
  projected ensemble +0.48/-0.22pp; oracle LGBM 4-seed mean 30.33%. Keep
  projected as opt-in; default remains no-lineup. V11 puts 80-90% real
  morning fidelity at ~29.25-29.54%; near-oracle is needed for ~30.3%.
- V12 identity/age hurt both seeds (-1.49/-0.29pp); reverted. Baseline
  calibration is ~2.2pp overconfident (maxp 31.5% vs 29.3% realized), and the
  top pick matches the hgpg leader only 48.5% of the time.
- V12-era recent training (2021-22+) did not beat full-era consistently
  (LGBM -1.07/-0.95pp; ensemble -0.15/+0.08pp), so full-era stays the default.
  `--test-start` / `--min-season` are retained for future backtests.
- V7 team form / special teams / goalie rest did not help (+0.06pp seed 1,
  -0.25pp seed 3) and was reverted. Historical anytime-goal-scorer odds are
  not available free before 2023-05; forward collection or a paid backfill
  would be required.
- V7-shots (18 raw distance/angle/HD/slot/rebound/PP columns) was mixed:
  LGBM -0.13pp seed 1 / +0.57pp seed 3, both McNemar p>=0.37; reverted.
  V7 MoneyPuck individual xG was also negative and collinear with V4.
- V8-own-xG (per-shot LightGBM, AUC 0.769) was negative on both seeds
  (-0.16 / -0.35pp, McNemar p=0.82/0.57) and reverted.
- V12-qoc (shift-chart QoC/QoT, 24 columns) was mixed: +0.507pp seed 1
  (p=0.41 vs baseline) and +0.032pp seed 3 (p=1.0); reverted. Zone starts are
  not in the raw layer. QoT-relative features ranked highly (top 10-20), so a
  narrower teammate-only block is the only follow-up worth trying.
- V12-interactions (tims × rates) did not help (+0.10/0.00pp) and group 3 got
  worse; reverted.
- Tims era starts 2021-12-09 (not 2023): headline 29.71% all challenge
  dates / 29.87% strict 2023-24+; all-season training beats 2023+ by 1.2pp
  on identical dates. Pre-2017 ingest is feasible but has no Tims labels.
- Every new feature must be justified as pre-game from the joins; if it
  needs labels from other rows, only shifted-per-player ones qualify.

Update this file after every experiment: config used, split mode, pooled
  OOF hits/total, per-headline McNemar vs hgpg AND gpg.
