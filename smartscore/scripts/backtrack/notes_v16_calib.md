# V16-calib — discrimination-vs-calibration diagnostic + within-group swap attempt

## Reproduction state

Pooled OOF rows come from `model.py --model ensemble --model-file` regenerations
under the **current** data cache (617,310 rows incl. the 2013-17 backfill):

```
OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/model.py --env dev --model ensemble --seed S \
    --split kfold --splits 5 --train-subset all \
    --model-file data/ml/oof_v16_calib_ens_seedS.csv > data/ml/v16_calib_ens_seedS.log
```

Files: `oof_v16_calib_ens_seed1.csv`, `oof_v16_calib_ens_seed3.csv`
(92,440 rows + header each; columns date, player_id, name, team_name, home,
tims, scored, gpg, hgpg, live, logreg, hgb, lgbm, ensemble, random, fold).

**Cache-generation caveat.** The archived MODEL_NOTES references
(ensemble 29.408 / 29.820 % for seeds 1/3) predate the 2013-17 row backfill
(96f0ff2-era cache). Under the current cache the regenerations give:

| method | seed 1 | seed 3 |
|---|---|---|
| gpg  | 837/3159 = 26.496 % | 837/3159 = 26.496 % (identical, deterministic) |
| hgpg | 886/3159 = 28.047 % | 886/3159 = 28.047 % (identical) |
| lgbm | 931/3159 = 29.471 % | 929/3159 = 29.408 % |
| ensemble | 925/3159 = 29.281 % | 935/3159 = 29.598 % |

Baselines gpg/hgpg reproduce the references exactly; the ensemble arms differ by
≈ −0.15/+0.22 pp vs the archived references (extra 2013-17 training rows), which
is why every comparison below is internal to this cache. All diagnostics are
computed by re-deriving picks from the dumps with `model.daily_picks` /
`model.score_picks` semantics (NaN-filtered idxmax per (date, tims)).

## Stage 1 — diagnostic (OOF-only, both seeds pooled over picks)

### Accuracy by predicted-probability decile (ensemble picks, pooled seeds, n=6,318)

`pmax` = max of (hgb, lgbm, logreg) component probs on the pick; `pmean` = mean.

| decile | pmax mean | n | hit % | | pmean mean | hit % |
|---|---|---|---|---|---|---|
| 0 | 0.140 | 632 | 12.3 | | 0.128 | 13.0 |
| 1 | 0.191 | 632 | 16.6 | | 0.172 | 17.3 |
| 2 | 0.233 | 632 | 24.7 | | 0.210 | 22.5 |
| 3 | 0.268 | 631 | 25.4 | | 0.246 | 26.6 |
| 4 | 0.299 | 632 | 33.7 | | 0.275 | 31.8 |
| 5 | 0.335 | 632 | 32.1 | | 0.307 | 33.4 |
| 6 | 0.382 | 631 | 31.7 | | 0.352 | 29.6 |
| 7 | 0.438 | 632 | 32.6 | | 0.405 | 34.5 |
| 8 | 0.503 | 632 | 42.1 | | 0.455 | 40.4 |
| 9 | 0.613 | 632 | 43.2 | | 0.523 | 45.4 |

Ordering is strong top-vs-bottom (12→43%) with a high-plateau between deciles 4-7
(~32%) — the model's mid-range is weakly differentiated, but the top decile is
clearly separated.

### Per-group calibration (ALL 92,440 challenge rows per seed, pooled; pmean quintiles)

| group | mean pmean | realized | bin0 pred/real | bin4 pred/real | max |gap| pp |
|---|---|---|---|---|---|
| g1 (n=62,264) | 0.267 | 27.08 % | 0.158/0.166 | 0.396/0.384 | 1.19 |
| g2 (n=60,430) | 0.155 | 16.04 % | 0.087/0.089 | 0.240/0.246 | 1.02 |
| g3 (n=62,186) | 0.089 | 8.61 % | 0.045/0.038 | 0.156/0.159 | 0.69 |

Bins are CLOSE: worst binned gap ≤1.2 pp, no monotone bias direction. The global
2.2 pp "mean max pred 0.315 vs realized 0.293" overconfidence in MODEL_NOTES
lives at the extreme (a few very-high-p predictions overshoot), not in the bulk
distribution: per-group realized-vs-predicted is essentially calibrated.
**Conclusion: miscalibration is NOT the problem — recalibration (isotonic/Platt)
cannot change within-group top-1 picks by construction (monotone map preserves
order) and the binned gaps are small. Skipped entirely as a variant.**

### Confident misses vs lucky hits (pooled picks, both seeds; pick-level quantiles)

Thresholds: pmax top-quartile ≥ 0.4388, bottom-quartile ≤ 0.2331.

- Confident picks: 1,580/6,318 (25.0%) → 655 hits (41.5% hit rate).
- **Confident misses: 925** (seed 1: 461, seed 3: 464) — 14.6% of picks.
- Confident hits: 655. **Lucky hits: 248** of 1,580 bottom-quartile picks.

Hindsight bounds (NOT leak-free, upper bounds only):

- If ALL top-pmax-decile (n=632) misses were identified and flipped: 2,219/6,318
  = 35.12% (+5.68 pp pooled ceiling).
- For pick-level confident misses, the best runner-up in the group would have
  hit 368/925 = 39.8% of the time → if we could rerank every confident miss onto
  its eventual scorer: +368 hits = +5.82 pp pooled ceiling.
- BUT the runner-up hit rate trail over ALL runner-up candidates is ~29% (only
  slightly above the picked 29.3%): the runner-up pool is NOT enriched outside
  the ones we wrongly discarded. A discriminator that could harvest a quarter of
  the 368 recoverable hits (~+1.4 pp) without dropping equivalent hits would
  need to identify specific losers among 925 near-top picks — that is
  discrimination improvement, not calibration.

Cost of blind swapping (bounds the rule-based upside): among confident picks
(hits + misses) with pmean gap top-vs-runner < 0.05, blind swapping to the
runner-up nets −21 hits (−0.33 pp pooled) — 152 wins vs 173 losses. Any
threshold rule must earn its keep from the margin calibration.

## Stage 2 — improvement attempt: within-group top-1 competitor swap (variant a)

**Config.** Cheap, no refits: on each seed's saved OOF rows, pmean =
mean(hgb,lgbm,logreg); within each (date, tims) group pick top-1 by pmean; when
the top-2 pmean gap < margin AND the runner-up has strictly better `hgpg`
(prior-game goals-per-game support — strictly pre-game, no leak) than the top
pick, prefer the runner-up. Margin calibrated per fold on TRAIN folds only (the
train folds' own picks are rescored under each candidate margin; argmax margin
applied to the test block). Fixed-margin sweep reported alongside as the
leak-clean reference.

```
OMP_NUM_THREADS=1 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/exp_calib.py --env dev --mode threshold-oof \
    --oof-tag v16_calib_ens --seed S
```
(`--mode threshold-oof` added to exp_calib.py: reads only the OOF dumps, no frame
build, no refits.)

### Results

**Seed 1** — train-fold margin chose 0.02 in all 5 folds. Pooled 936/3159 =
**29.630 %** vs ensemble-ref 29.281 % = **+0.35 pp** (per-fold 29.63 ± 2.40);
per-group g1=40.0 g2=27.2 g3=21.7; 2023-24+ 30.18, 2025-26 30.17.
McNemar (exact, pooled paired): vs ensemble 122-111 p=0.5125 (n.s.);
vs gpg 406-307 p=0.0002; vs hgpg 237-187 p=0.0172.

**Seed 3** — train margin chose 0.01/0.02. Pooled 930/3159 = **29.440 %** vs
ensemble-ref 29.598 % = **−0.16 pp** (per-fold 29.44 ± 1.16); per-group g1=39.0
g2=28.0 g3=21.3; 2023-24+ 29.49, 2025-26 30.65.
McNemar: vs ensemble 102-107 p=0.7821 (n.s.); vs gpg 402-309 p=0.0006;
vs hgpg 247-203 p=0.0425.

Fixed-margin sweep (pooled, both seeds): 0.01→926/934, 0.05→920/922,
0.10→917/921, 0.25→921/921 — nothing clears +0.5 pp on either seed consistently;
every margin ≥0.05 is negative vs the unmodified ensemble on both seeds.

### Verdict: REJECTED

- Seed 1 +0.35 pp, seed 3 −0.16 pp — the "promising" bar (≥ +0.5 pp pooled on
  BOTH seeds, McNemar-consistent) fails on both counts; McNemar vs the
  unmodified ensemble is not significant on either seed (p=0.51 / 0.78), and the
  sign flips between seeds (pure pick noise).
- No seed-0 run (variant failed seed 3). The swap variant barely moves anything
  (changes ≤2 % of picks), consistent with the stage-1 finding: the recoverable
  margin sits with confident misses needing genuine discrimination skill, not
  with threshold-adjacent ties.

## Notes

- **Monotone/recalibration-only variants cannot change picks**: within-group
  top-1 selection depends only on the score ORDER; any strictly monotone
  per-row transform (Platt, isotonic, per-group scaling) preserves the argmax.
  They are skipped entirely by design, and stage 1 shows their upside is small
  anyway (bulk calibration gaps ≤1.2 pp).
- Variant (b) (leak-free two-stage refit, `--mode twostage`) was implemented
  earlier in exp_calib.py but NOT run under the final protocol — stage 2 scope
  was exactly one attempt, the cheaper (a). Code remains available for a later
  worker; note it is untested end-to-end.
- `career_goals_pg` (used by the earlier `--mode threshold` swap idea as
  evidence) is verified strictly pre-game (`shift(1).expanding().mean()`), but
  the run here uses `hgpg` from the dumps, avoiding the 25-min frame build
  entirely.
- Repo hygiene: `data/ml/` is gitignored ("Model training caches, re-fetched
  from Supabase") — the 18.5 MB OOF dumps and small diagnostic CSVs
  (`v16calib_diag_cm_picks.csv`, `v16calib_decile_pmax.csv`,
  `v16calib_swap_picks_seed{1,3}.csv`) are therefore kept untracked, per repo
  convention; the regeneration command above reproduces them. Committed:
  `exp_calib.py` (diagnostic + threshold-oof modes added to the previous
  worker's file) and this notes file.
