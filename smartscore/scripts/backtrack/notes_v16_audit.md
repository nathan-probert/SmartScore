# V16 audit — independent pre-game leakage, number, and commit-integrity review

**Auditor:** independent worker (read-only; no `model.py` edits, no retraining).
**Branch:** `backtrack-recon` @ `4f1816c` (V16 commits `493ba96..4f1816c`).
**Method:** read the seven V16 experiment scripts and notes; re-derived
features from the raw store/Supabase caches where needed; independently
re-scored the committed/ignored OOF CSVs with `model.daily_picks` semantics
(NaN-filtered `idxmax` per `(date, tims)`, tims 1/2/3); grepped all V16
commits. No artifact was deleted or modified.

## Verdict table

| Experiment | Commit(s) | Pre-game leakage | Number integrity | Other | Verdict |
|---|---|---|---|---|---|
| **embed** | `493ba96` | **FAIL — future lookahead** | n/a (result invalid) | — | **INVALID** |
| calib | `3de58b4` | PASS | PASS (1-pick parser tie) | — | **PASS** |
| group3 | `84ed8dd`, `1e94c4a` | PASS | PASS (exact with round-trip parse) | — | **PASS** |
| pbp | `e56b159`, `d8b2ef5` | PASS (see caveat) | PASS (exact) | force-added 135 MB | **PASS\*** |
| interact | `ef69863` | PASS | PASS (1–2-pick tie/provenance) | — | **PASS** |
| exposure | `4f1816c` | PASS (see caveat) | PASS (exact with round-trip parse) | OOF path collision | **PASS** |
| collect | `2f5481a` | n/a | n/a | **prod-write claim false; runbook broken** | **FAIL** |

`PASS*` = leakage/number review passes; commit hygiene fails separately (see
below).

---

## 1. Baseline integrity (all experiments)

Every V16 OOF inspected has the same shape and the same labels:

| check | result |
|---|---|
| rows | **92,440** in all 16 OOF files |
| challenge dates | **1,053** |
| folds | **1–5** |
| `gpg` | **837 / 3,159 = 26.4957 %** (target 26.496 %) |
| `hgpg` | **886 / 3,159 = 28.0469 %** (target 28.047 %) |
| date → fold map, same seed | **0 mismatches** across every within-seed OOF |
| seed 1 vs seed 3 fold map | 827 / 1,053 dates differ (expected: `fold_blocks` shuffles) |

`model.fold_blocks` (`model.py:896-901`) is a seeded shuffle of the sorted
challenge dates followed by `array_split`, so same-seed runs are paired on
identical dates/folds. The 3,159 challenge picks and both baselines reproduce
exactly in the OOF CSVs, the run logs, and the notes.

## 2. Leakage review

### 2.1 embed — **FAIL, INVALID**

`exp_embed.py` fits a weighted ALS matrix on **player × season** outcome cells
(`exp_embed.py:125-131`) built from the fold's **training dates**
(`exp_embed.py:388-394`). The fold split is a random date-block split, not a
time split (`model.py:896-901`), so a fold's training dates include dates
**after** each held-out date. A player-season cell therefore contains future
games of the same season, and `emit_factors` assigns that one cell value to
every game in the season.

Leak quantification (recomputed from `player_games` + the seed-1 fold map):

- challenge rows with at least one same-season game on a **later** training
  date for that player: **90,450 / 92,440 = 97.85 %**
- per fold: 18,270/18,635, 18,300/18,756, 17,835/18,217, 18,139/18,494,
  17,906/18,338.

This is direct target-adjacent lookahead: for a test game on date *d*, the
player's `e1..e3`/`e_rec_*` were partly estimated from that player's goals,
shots and TOI in later games of the same season. The notes' “leak-free
verification” only asserts fold **date-set disjointness**
(`exp_embed.py:378-379`); it does not test that the training dates are
chronologically prior to each test date. The seed-3 result that looked
promising (+0.570 pp, 947 vs 929) is therefore not evidence of signal.

**Do not adopt; mark the experiment INVALID. Artifacts left in place.**

### 2.2 calib — PASS

`exp_calib.py:354-395` builds test/train by date set and scores test rows with
a model fit on the complementary dates. The stage-2 features
`group_stage_stats` are per-`(date,tims)` summaries of **stage-1 predictions**
(not labels), and the competitor-swap margin is selected on **train folds
only** and applied to the test block (`exp_calib.py:463-510`). No same-game
lineup/shift column, current-game TOI/goals/shots, or current-date outcome
groupby enters a feature. `Categorical` patch is static `lineup_position_group`
+ `tims`. PASS.

### 2.3 group3 — PASS

`exp_group3.py:254-295` uses the same date-blocked train/test split; weights
depend only on `tims`; the dedicated group-3 model is trained on
`train_all[tims == 3]`; all ranks are within `(date, tims)` on **predicted**
scores. `znop` (`exp_group3.py:346-357`) is a diagnostic on already-predicted
base scores, not a feature. No leak. PASS.

### 2.4 pbp — PASS, with a methodological caveat

Per-shot xG is generated out-of-fold: shot dates are partitioned into the five
challenge blocks plus five non-challenge blocks (`exp_pbp.py:310-316`), each
block's classifier is fit on `~test_mask` and predicts only its block
(`exp_pbp.py:329-346`). Train and test shot dates are disjoint by construction.

Two caveats, neither a same-game leak:

1. The assertion at `exp_pbp.py:333` is **vacuous**:
   `set(shot_dates[~test_mask]) & set(block_dates)` is empty by definition of
   `test_mask = isin(block_dates)`. The split is still disjoint, but the assert
   proves nothing.
2. The blocks are random date blocks, so each shot's xG is predicted by a model
   trained on later shot dates too. That is future-label lookahead in the
   xG **mapping** (not the target player's current-game outcome). If “strictly
   pre-game” means time-ordered xG training, this needs tightening; under
   standard OOF stacking it is acceptable.

Downstream player features use only prior games: every rolling feature is
`shift(1)` over player-sorted games (`exp_pbp.py:184-216`) and the perturbation
assert (`exp_pbp.py:353-363`) confirms the current game's own xG does not move
its own features. The shot-fold columns in `shot_xg_seed{1,3}.csv` match the
model fold map for the challenge dates (0 mismatches over 1,939 dates).
PASS with the caveat above.

### 2.5 interact — PASS

All interactions are products of columns already in the pre-game frame
(`exp_interact.py:92-143`): home flag × form/rates, rest × static role, rates ×
opponent goalie/team metadata. `lineup_position_group` was independently
checked to be a static roster position: **0 / 2,422 players change F/D**, and
2,145 null rows; it is not the same-game shift-derived `lineup_unit`/`pp_unit`
(the V6 leak). `opp_gaa`/`opp_save_pct` are same-date **pre-game** goalie
snapshots, not post-game totals (`MODEL_NOTES.md:274`). The date-mean opponent
adjustment uses only distinct `(date, opp)` metadata, not the row's own game.
PASS.

### 2.6 exposure — PASS, with caveat

`exp_exposure.py:255-258` computes `xg_before`/`toi_before` with
`cumsum() - current` within `(player, season)` after sorting by date, so only
strictly earlier games enter. `r10_toi_pg` is a prior-10-games mean. The
product (`exp_exposure.py:295`) is therefore pre-game for the target game.

The per-shot xG source is the same random-date-block OOF model as pbp, so the
same “xG mapping trained with future shots” caveat applies. No current-game
stats or same-game lineup column enters the feature. PASS with that caveat.

---

## 3. Independent re-scoring of OOF CSVs

Re-scored every V16 OOF with the exact `model.daily_picks` loop
(`groupby(date, sort=True)`, `tims` 1/2/3, drop NaN, `idxmax`). Because
ensemble/base score columns are means of percentile ranks, they contain exact
and near ties; **`pandas.read_csv`'s default float parser flips 1–3 picks**
relative to the in-memory values. Re-reading with
`float_precision="round_trip"` reproduces the notes exactly. Both readings are
shown; no delta reaches the **0.1 pp** report threshold (3 picks = 0.095 pp).

| experiment | column | notes / log | default parse | round-trip parse | delta vs claim |
|---|---|---:|---:|---:|---:|
| calib | lgbm s1 / s3 | 931 / 929 | 931 / 929 | 931 / 929 | 0 |
| calib | ensemble s1 / s3 | 925 / 935 | 925 / 935 | **926** / 935 | ≤1 pick (0.032 pp) |
| pbp | lgbm s1 / s3 | 926 / 925 | 926 / 925 | 926 / 925 | 0 |
| group3 | base s1 / s3 | 926 / 925 | 925 / 928 | **926 / 925** | 0 with round-trip |
| interact | lgbm s1 / s3 | 908 / 932 | 908 / 932 | 908 / 932 | 0 |
| interact | ensemble s1 / s3 | 941 / 943 (log, notes) | 942 / 941 | **941 / 943** | 0 with round-trip; own results CSV is 942/941 (1–2 picks) |
| exposure | lgbm s1 / s3 | 922 / 924 | 922 / 924 | 922 / 924 | 0 |
| exposure | ensemble s1 / s3 | 921 / 933 | 922 / 936 | **921 / 933** | 0 with round-trip |
| embed | lgbm_e s1 / s3 | 930 / 947 | 930 / 947 | 930 / 947 | 0 (but experiment INVALID) |

Tie counts in the score columns: 165–195 exact-max `(date,tims)` groups for
ensemble/base; 15–22 additional groups within 1e-12. LGBM columns have zero
ties and reproduce exactly under either parser. The group3 and exposure
“mismatches” are entirely a CSV float round-trip artifact, not a data or
re-pick discrepancy. The interact notes quote the round-trip re-pick
(941/943), while its `exp_v16_interact_results.csv` holds the in-run picks
(942/941); that 1–2-pick provenance inconsistency is below threshold but
should be noted if the numbers are cited.

**No mismatch > 0.1 pp was found.**

---

## 4. Commit hygiene

| check | result |
|---|---|
| `model.py` untouched | **PASS** — 0 commits touching it since `96f0ff2`; not modified in the worktree |
| shared code touched | only new `exp_*.py`, `fetch_pbp.py`, `snapshot_projected.py`, notes; no edits to existing shared modules |
| external xG/odds artifacts committed | **PASS** — `git ls-files` has no MoneyPuck/xG/odds data; the only `odds` match is the code file `smartscore/scripts/get_odds.py`. `data/moneypuck/` (140 MB) is untracked |
| heavy `data/ml` caches untracked | **FAIL** — `.gitignore:51` ignores `data/ml/`, but `d8b2ef5` force-added 6 files: 4 OOF CSVs (7.1 MB each) + `pbp_player_game.csv` (22.6 MB) + `pbp_shots.csv` (90.7 MB), **~135 MB / 1,258,381 lines** |
| OOF artifacts durable | **partial** — only the pbp/base OOFs are committed; the embed/calib/group3/exposure/interact OOF CSVs are untracked and ignored, so the re-scoring above depends on local files |
| `data/ml/oof_v16_base_seed3.csv` tracked+modified | **investigated** — HEAD is the 8-column pbp base (7,111,785 B); the worktree file is the 16-column exposure base (18.5 MB). The 8 shared columns (`date,player_id,tims,scored,gpg,hgpg,lgbm,fold`) have **0 differences over 92,440 rows**; only added columns differ. Cause: `exp_exposure.py:396` writes `oof_v16_{arm}_seed{seed}.csv`, so the `base` arm writes the same `oof_v16_base_seed{seed}.csv` path as pbp (`base_seed1` was overwritten by pbp on 10-20, `base_seed3` by exposure on 10-22) |
| worktree litter | untracked `exp_v16_picks_*.pkl` (~50 files, ~1.5 MB each) and `exp_v16_perfold_*.csv` under `smartscore/scripts/backtrack/`; a `git add -A` would sweep them in |

No MoneyPuck xG/odds **data** is committed. The `pbp_shots.csv` header is raw
first-party event attributes (`shot_type,x,y,dist,angle,...`), no xG column.

---

## 5. Collect worker (`snapshot_projected.py`, `2f5481a`)

### 5.1 Code safety — PASS with two issues

Safe properties verified:

- Upsert key is `(date, player_id)` with `ignore_duplicates=True`
  (`snapshot_projected.py:454-466`), and `rows` is a dict keyed by the same
  pair (`build_rows`), so append cannot create duplicate prod rows.
- `refresh` PATCHes only `lineup_unit`, `lineup_position_group`, `pp_unit`,
  `lineup_status` (`_patch_lineup_fields`, lines 401-419), and skips rows whose
  stored status is `PROJECTED`/`CONFIRMED`/`ACTUAL` (`KEEP_STATUSES`, lines
  105-107, 474). No stats/scored column is in any payload.
- `--dry-run`, `--limit`, explicit `--env prod`, and a date gate on the
  article's `datePublished` exist; `--force` is required to bypass the date
  gate.
- `PAYLOAD_COLUMNS` (line 90) is **dead code** — defined but never used; rows
  are built explicitly, so the “never add a stats column” warning is not
  enforced by a single constant.

Issues:

1. **TOCTOU in refresh.** `existing` is read before the PATCH; the UPDATE
   filters only `date` + `player_id` (`lines 411-416`), not the status. A row
   that becomes `CONFIRMED` between the read and the PATCH would be
   overwritten back to `PROJECTED`. Low probability, but the invariant
   “PROJECTED/CONFIRMED/ACTUAL are never touched” is not atomic.
2. **The 3×-per-day rationale does not work.** The runbook says 13:00/16:00
   runs carry “the freshest RotoWire PP units”, but after the first run the
   rows are `PROJECTED`, and `KEEP_STATUSES` makes refresh skip them. Later
   runs cannot update units on already-projected rows. Only null/`UNKNOWN`
   rows are patchable.

### 5.2 Did it actually write prod rows? — **NO EVIDENCE; claim contradicted**

Live Supabase read on 2026-10-22 (service role, read-only):

| env | date | PROJECTED | UNKNOWN | D units? | PP vocabulary |
|---|---|---:|---:|---|---|
| dev | 2026-10-20 | **390** | 105 | **D1/D2/D3 present** | PP1/PP2 + verbatim |
| prod | 2026-10-20 | **255** | 240 | **none — all rows `F`** | verbatim `POWER PLAY #1/#2` only |
| prod | 2026-10-21 | 46 | — | none | verbatim |
| prod | all dates | **301** | 5,487 | none | verbatim |

The runbook/notes claim (`MODEL_NOTES.md:1624-1630`) is “run against **prod**
the same evening: `UNKNOWN` 4,900 → 4,765, `PROJECTED` 255 → 390 (all
2026-10-20), collector filled 135 more (incl. defence)”. The **390 / 105**
state exists on **dev**, not prod; prod still has **255 / 240**. The collector
always writes D1–D3 and PP1/PP2, so 135 prod rows “incl. defence” would leave
D rows and normalized PP labels; prod has neither. The prod state is the live
pipeline signature (255 PROJECTED, all F, verbatim labels). Conclusion: the
prod-write claim is **false or at best unverified** — the recorded verification
numbers are the dev post-state. There is no
`snapshot_projected.log` anywhere to prove a prod run, and a later pipeline
re-publish could in principle have overwritten rows, but nothing supports the
claimed prod write.

### 5.3 Runbook vs code — **FAIL**

- The cron line in `MODEL_NOTES.md` targets
  `cd /home/nathan/code/SmartScore`. That checkout is on `main` at `990b0b6`,
  which **does not contain** `snapshot_projected.py` (the file exists only on
  `backtrack-recon`, `2f5481a`). The command would fail immediately.
- `crontab -l` → **“no crontab for nathan”**. The 10:30/13:00/16:00 schedule is
  not installed.
- No log file exists at the documented path.
- The documented “three runs keep PP units fresh” behavior contradicts
  `KEEP_STATUSES` (see 5.1).

---

## 6. What would need fixing (not done — read-only audit)

1. **embed:** either drop the season-cell ALS block or rebuild it from
   strictly prior-season cells only; re-run both seeds before citing any
   embed number. Current seed-3 +0.57 pp is leak-inflated and INVALID.
2. **pbp/exposure:** if strict causality is required, fit the per-shot xG model
   on time-ordered prior blocks only (or a rolling origin), not random date
   blocks.
3. **Repo:** remove the 6 force-added `data/ml` files from tracking (history
   rewrite required to reclaim size); add `data/moneypuck/` to `.gitignore`;
   resolve the `oof_v16_base_seed*` path collision in `exp_exposure.py`.
4. **Collect:** correct the prod verification in `MODEL_NOTES.md`; fix the
   cron path to the worktree; either install the crontab or mark it as
   unverified; decide whether later runs should be allowed to refresh
   `PROJECTED` PP units (and make the UPDATE status-conditional atomically).

---

## 7. Commands / evidence trail (selected)

```bash
# baselines + picks, both parsers
.venv/bin/python /tmp/opencode/rescore.py
.venv/bin/python /tmp/opencode/rescore_rt.py

# embed future lookahead
.venv/bin/python /tmp/opencode/embed_leak_recheck.py
# -> rows 92440, leak rows 90450 = 97.85 %

# fold-map consistency
.venv/bin/python /tmp/opencode/foldcheck.py
# -> within-seed 0 mismatches; seed 1 vs 3 = 827/1053

# collect prod/dev state
.venv/bin/python /tmp/opencode/prodcheck.py
# -> dev 2026-10-20 PROJECTED 390 (D units present)
# -> prod 2026-10-20 PROJECTED 255, all F, no D units
```

**Committed file:** `smartscore/scripts/backtrack/notes_v16_audit.md` (this
file). No code or data artifact was changed.
