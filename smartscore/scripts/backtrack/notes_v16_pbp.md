### V16-pbp — pass-context first-party per-shot xG proxy (rejected)

Goal (never-tried idea 7): make the first-party xG proxy **richer with
pass-before-shot / zone-entry / rebound context** (V8-own-xG used only shot
geometry) and test whether it moves the ensemble's goal-scorer picks.

**Data.** NHL gamecenter play-by-play was fetched for **every 2021-22+
game in the raw store** (the seasons that carry tims-era challenge rows and
the pointwise training rows that matter): **6,993 games fetched /
6,993 in scope (2021-10-12 .. 2026-06-14), 0 fetch failures and 0 parse
failures** over all passes (2025-26 1,394; 2023-24+24-25 2,595; 2021-22+22-23
2,801; plus 172 full payloads adopted read-only from `data/cache/`). Retry
envelope respected throughout (4 attempts, 30s timeout, 1/2/4s backoff,
0.34s/0.75s throttle between requests; progress every 100 games). Trimming /
aggregation is done by `fetch_pbp.py`:

- `data/ml/pbp/pbp-{game_id}.json` — trimmed per-game plays (reuse-safe,
  idempotent; re-running refetches only missing games).
- `data/ml/pbp_shots.csv` — **608,813 unblocked shot attempts** (44,175
  goals = 7.26%), one row per `shot-on-goal / missed-shot / goal` event with
  raw-attribute context.
- `data/ml/pbp_player_game.csv` — **279,659 player-games (6,993 games x
  both rosters, exactly the player_games 2021+ row count), incl. 65,460
  zero-attempt rows** (pass-only players / quiet defencemen), so rolling
  windows rate over *games*, not shot-games.

The feed has **no pass, zone-entry or rebound event attributes**, so the
context is raw event adjacency (raw attributes only; no external xG/odds):

- possession chain: own-team events since the last faceoff / stoppage /
  period start (`prior_own_events`); a goal ends its chain;
- **one-touch proxy** = the attempt is the own team's first event of the
  chain (one-touch vs carry-in cannot be separated further from this feed);
- **rebound** = the immediately preceding play is an own-team shot event of
  the same period within **|4s|** (the period clock jitters +-1s between
  adjacent events, hence abs; share 8.1% — known NHL rebound rate 6-9%);
- **cross-ice proxy** = |dy| between the shot and the last own-team coord
  event (hit/takeaway/giveaway/blocked/prior shot) >= 25 ft (share 25.8%);
- **PP** = situationCode digit asymmetry, digit order calibrated on the
  shooters' side across asymmetric codes (share 9.5%);
- empty net = `goalieInNetId` null (0.74%).

**Per-shot model (first-party, leak-free).** LGBM config-shape 400/0.05/31
(`SHOT_LGBM`), label = raw goal flag, OOF by date fold: the 1,053 challenge
dates are cut into five shuffled blocks with `model.fold_blocks(seed)`,
every other date with shots gets five more (10 blocks), no fold trains on
its own dates (asserted). Two variants per fold:

- geo-only: dist/angle, rebound, PP, empty net, period, shot type, HD
  (< 20 ft), slot (54<=|x|<=89, |y|<=22 — the V7 definitions);
- enriched: geo + possession block (prior_own_events, one_touch_raw,
  |dx|/|dy| of the last carry/pass origin, cross_ice).

| seed | geo-only AUC | enriched AUC | delta |
|---|---|---|---|
| 1 | 0.7645 +- 0.0044 | 0.7652 +- 0.0044 | **+0.0007** |
| 3 | 0.7653 +- 0.0117 | 0.7662 +- 0.0118 | **+0.0009** |

(The V8 geometry-only MONEYPUCK reference was AUC 0.769; on the NHL feed the
same geometry reaches 0.765 — the possession block adds almost no per-shot
separation beyond geometry.) Leak check: perturbing a game's own xG left its
own rolling features untouched (asserted each run).

**Player features (strictly prior-game).** Per-game OOF xG sums and context
counts roll into `rs_*` features per player over **covered games only**,
shift(1) per player, windows 15/30 (min-coverage 10/20 covered games):
`rs_pbxg_pg15`, `rs_pbxg_pg30`, `rs_pbxg_pa15` (xG per attempt),
`rs_pbprior_share15/30`, `rs_pbone_touch_share15`, `rs_pbrebound_share15`,
`rs_pbhd_share15`, `rs_pbcross_share15`, `rs_pbslot_share15`,
`rs_pbpp_share15`, plus a `_miss` indicator + 0.0 imputation per feature for
rows without enough covered history. Challenge-row coverage: **92,282 /
92,440 = 99.83% measured**. Rows across 2013-17/pre-2021 (training-only) are
imputed with the indicator.

**Concurrent-worker fix noted** (same experiment ran in two sessions after
server restarts): the shared merge into `build_frame` selected `game_id`,
which the frame already carries -> `_x/_y` suffixes tripped the row-count/
suffix assertion. Fixed (drop `game_id` from the merge columns). No model.py
change; the corrected-schema feature block is otherwise untouched.

**Protocol (unchanged).** Model.py machinery, same k=5 challenge-date
k-fold (`fold_blocks`, seed-controlled), `--train-subset all`, LGBM config a
for the gate and the full rank ensemble per fold,
`OMP_NUM_THREADS=2 LGBM_N_JOBS=2`, lightgbm==4.7.0. References are the
same-seed corrected OOF files (`oof_v7_base_lgbm_seed{1,3}.csv` 928/916;
`oof_v6c_ensemble_seed{1,3}.csv` 929/942; hgpg 886 = 28.047%; gpg 837 =
26.496%).

| model | seed | reference | + pbp block | delta | McNemar vs own ref |
|---|---|---:|---:|---:|---|
| ensemble | 1 | 929 = 29.408% | **945 = 29.915%** | **+0.507pp** | n/a (target==ensemble; own-run pair vs hgpg 324-265, p=0.0168) |
| ensemble | 3 | 942 = 29.820% | **918 = 29.060%** | **-0.760pp** | — |
| LGBM | 1 | 928 = 29.376% | **926 = 29.313%** | **-0.063pp** | 189-192, p=0.918 |
| LGBM | 3 | 916 = 28.997% | **925 = 29.281%** | **+0.284pp** | 182-198, p=0.442 |

Per-fold. Seed 1 ensemble folds 29.914 +- 2.514 (g1 39.2 / g2 28.8 / g3 21.7;
2023-24+ 30.48%, 2025-26 31.60%). Seed 3 ensemble folds 29.060 +- 0.639
(g1 38.5 / g2 27.8 / g3 20.9; 2023-24+ 29.34%, 2025-26 28.59%). Seed 1 LGBM
folds 29.314 +- 2.351; seed 3 LGBM 29.282 +- 0.948.

McNemar vs baselines (pooled paired picks):
- seed 1: ensemble vs hgpg 324-265, p=0.0168; ensemble vs gpg 416-308,
  p=0.0001. (These compare against the within-run hgpg/gpg columns; the
  pre-registered stop rule is the both-seeds delta above.)
- seed 3: ensemble vs hgpg 310-278, p=0.2011; vs gpg 409-328, p=0.0032.
- LGBM: seed 1 vs hgpg 313-273, p=0.1071; seed 3 vs hgpg 329-290, p=0.1266.

**Verdict: REJECTED** under the pre-declared rule (">= +0.5pp pooled on BOTH
seeds 1 and 3"). Seed 1 was exactly at the bar (+0.507pp) but seed 3 lost
0.76pp, i.e. the same direction the geometer-only blocks always showed:
one seed up, one seed down, nothing consistent and no McNemar support on the
losing seed. Seed 0 was therefore NOT run (condition unmet).

Diagnosis worth keeping: the per-shot enriched model is barely better than
geometry-only (+0.0007/+0.0009 AUC on full data; enough to matter only in
smoke-scale samples), so the pass-before-shot context as available on the
NHL feed adds little real shot-quality information beyond distance/angle/
rebound/PP. Player-level rolling xG/context shares are then ~8 extra
correlated columns on top of the V4/V7 shot-history block — which is the
pattern that failed V7-shots/V8-own-xG every time. The pick-metric noise
band on this experiment (swing between seeds of +/-0.5-0.8pp from 5-pick
folds) is the same size as the effect sought.

Commands:

```bash
cd SmartScore-backtrack
# fetch (resumable; seasons via --min-season/--max-season)
OMP_NUM_THREADS=2 uv run python smartscore/scripts/backtrack/fetch_pbp.py --env dev
# eval (per seed; lgbm gate + ensemble)
for m in lgbm ensemble; do
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/exp_pbp.py --env dev --seed 1 --with-pbp --model $m
  OMP_NUM_THREADS=2 LGBM_N_JOBS=2 uv run --with lightgbm==4.7.0 python \
    smartscore/scripts/backtrack/exp_pbp.py --env dev --seed 3 --with-pbp --model $m
done
```

Artefacts kept (untracked data cache): `data/ml/pbp/` (6,993 JSONs),
`data/ml/pbp_shots.csv`, `data/ml/pbp_player_game.csv`,
`data/ml/pbp_failed.csv` (empty coverage), logs `/tmp/opencode/exp_pbp_*.log`
and `data/ml/pbp_fetch*.log`. Code: `fetch_pbp.py`, `exp_pbp.py` on
`backtrack-recon` (commit 'V16-pbp: pass-context first-party xG proxy').
