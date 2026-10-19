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

- Try pushing past ~29%: pairwise / within-day ranking objective (train to
  rank inside a slate instead of the global 0/1 loss); monotonic constraints
  on gpg/hgpg; Tuesday-style ablation of weak features (lineup_* and
  pp_unit) to see whether they add anything beyond hgpg.
- Feature ideas not yet in: season-day-of-season index, goalie-strength
  relative term (opp_gaa vs own_gaa), schedule density, month effects.
- Every new feature must be justified as pre-game from the joins; if it
  needs labels from other rows, only shifted-per-player ones qualify.
- Update this file after every experiment: config used, split mode, pooled
  OOF hits/total, per-headline McNemar vs hgpg AND gpg.
