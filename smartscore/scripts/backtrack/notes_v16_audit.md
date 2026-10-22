# V16 audit — leakage, number, baseline, and commit integrity

**Scope:** read-only audit of the six V16 experiment scripts plus the V16-collect
script on `backtrack-recon` @ `4f1816c`.
**No** `model.py` edit, **no** retraining, **no** artifact deleted or moved.
**Method:** grep-first reads of every script around the high-risk patterns
(`lineup_unit`/`pp_unit`, `shift`, same-game joins, `groupby(...).transform`,
`cumsum`, `fold`, date-disjointness asserts), then independent re-scoring of
the OOF CSVs with `model.daily_picks`/`score_picks` semantics (NaN-filtered
`idxmax` per `(date, tims)`, `tims` 1/2/3), plus direct reads of the raw store
and the two Supabase environment tables.

---

## Verdict table

| Experiment | Commit(s) | Leakage (S1) | Numbers (S2) | Baseline/folds (S3) | Verdict |
|---|---|---|---|---|---|
| **embed** | `493ba96` | **FAIL — future within-season lookahead** (97.85 % of challenge rows) | n/a (result invalid) | PASS | **INVALID** |
| calib | `3de58b4` | PASS | PASS | PASS | **PASS** |
| group3 | `84ed8dd`, `1e94c4a` | PASS | PASS | PASS | **PASS** |
| pbp | `e56b159`, `d8b2ef5` | PASS\* (one vacuous assert) | PASS | PASS | **PASS\*** |
| interact | `ef69863` | PASS | PASS (1–2 pick parser artifact) | **FAIL — log/CSV provenance** | **PASS\*** |
| exposure | `4f1816c` | PASS\* (fold guard documented, not implemented) | PASS (1 pick parser artifact) | PASS | **PASS\*** |
| collect | `2f5481a` | n/a | n/a | **FAIL — prod-write claim contradicted** | **FAIL** |

`PASS*` = the leakage/number question passes; a separate, documented caveat is
recorded in the section named in the cell. Only `embed` is **INVALID**; its
artifacts were left untouched.

---

## 1. Leakage

### 1.0 What every script does *not* touch (checked first)

None of the six scripts adds a same-game shift-derived `lineup_unit` / `pp_unit`
feature — the class of leak that invalidated the 30.36 % result:

```
$ grep -n 'proj_\|line_intact\|linemate\|lineup_unit\|pp_unit' \
      exp_embed.py exp_calib.py exp_group3.py exp_pbp.py exp_interact.py exp_exposure.py
exp_interact.py:26:  same-game shift-derived lineup_unit/pp_unit); opponent ...
exp_exposure.py:24:  * no same-game shift-derived lineup_unit/pp_unit enters any feature.
```

Both hits are prose. `model.add_derived` always merges the V9 projected-lineup
block into the frame (`model.py:634-640`), but those columns are *not* in
`DATASET_FEATURES` (`model.py:135-177`); they are only appended by
`model.py:1112` when `--lineup projected|oracle` is passed, and no V16 script
passes `--lineup` (they call `run_folds`/`build_frame` directly). The one
lineup column in the feature set, `lineup_position_group`, is the F/D letter
only (`service.py:1093` derives it as `unit[:1]`), i.e. a static roster
attribute — it carries no unit or same-game lineup information.

### 1.1 embed — **FAIL / INVALID (future lookahead, not fold leakage)**

What is correct:

- Fold split excludes the held-out dates from training: `exp_embed.py:377-379`
  (`assert not overlap, "leak: fold train/test dates overlap"`).
- Factors are fit on the fold's training dates only:
  `exp_embed.py:386-392`, `lines[lines["date"].isin(train_dates)]`.
- Emission is frozen and train-standardized: `exp_embed.py:213-236` (frozen
  `emb` lookups), `239-250` (train-row mean/sd, then applied to test).
- The test fold's own games therefore never enter the factor fit.

What fails the **strictly pre-game** standard: the ALS matrix is
`players × (season × channel)` and every cell is an *aggregate over the whole
training date set* (`exp_embed.py:125-158`: goals/gp, shots/gp, goals/shots,
toi/gp summed over `train_lines`), while `fold_blocks` is a **shuffled**
date-block split (`model.py:896-901`), so a fold's training dates include dates
*after* each held-out date. `emit_factors` then gives every game in a season the
same cell (`exp_embed.py:228-234`). For a test game on date *d* the player's
`e1..e3` / `e_rec_*` are therefore partly estimated from that player's goals,
shots and TOI in **later games of the same season**.

Quantified independently from `player_games` + the committed seed-1 fold map
(challenge rows with ≥1 later same-season game among that fold's *training*
dates):

| fold | rows | affected |
|---|---:|---:|
| 1 | 18,635 | 18,270 (98.04 %) |
| 2 | 18,756 | 18,300 (97.57 %) |
| 3 | 18,217 | 17,835 (97.90 %) |
| 4 | 18,494 | 18,139 (98.08 %) |
| 5 | 18,338 | 17,906 (97.64 %) |
| **total** | **92,440** | **90,450 (97.85 %)** |

This is not same-game leakage (the target game is excluded) and not test-fold
contamination (the fold's own outcomes are excluded), so the OOF number is
internally unbiased for the model as written — but the model as written could
not be deployed, because at pick time the rest of the season is unknown. The
notes' "leak-free verification" (`notes_v16_embed.md:33-37`) only checks fold
date-set disjointness, not chronology.

**Verdict: INVALID** under the batch's own pre-game standard; do not cite
seed-3 `947` (or seed-1 `930`) as signal. Artifacts untouched.

### 1.2 calib — PASS

- Stage 1: test rows scored by a model fit on train dates
  (`exp_calib.py:367`); train rows scored by **inner OOF** over train dates
  only (`exp_calib.py:374-381`). No train row is scored by a model that saw it.
- Stage 2: refit on `train_feats` only (`exp_calib.py:390`, `fit_stage2`
  162-181). Its per-group features are summaries of **stage-1 predictions**,
  not labels (`group_stage_stats`, `exp_calib.py:133-146`), and the pool
  aggregates are same-date cross-sections of pre-game `hgpg`/`gpg`
  (`exp_calib.py:122-131`).
- Swap margin: selected **per fold on train folds only** and applied frozen to
  the held-out block — `exp_calib.py:468-469` (docstring), `493-497`
  (`train_acc` loop), `505-507` (`best_margin` applied to `test`).
- Merge guards: `exp_calib.py:214-218` (row count, suffix columns, all-NaN).
- Caveat, not a leak: the margin is chosen on OOF probabilities whose own
  generating models saw other folds' dates — standard stacking behaviour, same
  as the base ensemble.

### 1.3 group3 — PASS

No new feature columns at all; the experiment changes sample weights and adds a
group-3 model.

- Train excludes the test dates: `exp_group3.py:258`
  (`frame[~frame["date"].isin(test_dates) & scored.notna()]`).
- Weights depend only on `tims` of training rows: `exp_group3.py:279`.
- Group-3 ranker trains on `train_all[tims == 3]` only:
  `exp_group3.py:287-288`.
- All ranks are computed on **predicted** scores within `(date, tims)` on the
  held-out block (`exp_group3.py:274`, `283`, `289`) — no labels enter.
- `fit_g3_model`/`fit_lgbm_w` receive `train`/`train_g3` only
  (`exp_group3.py:136-153`, `104-120`).

### 1.4 pbp — PASS (one vacuous assert)

- Per-shot xG is out-of-fold: dates are cut into 5 challenge blocks + 5
  non-challenge blocks (`exp_pbp.py:310-316`), each classifier is fit on
  `~test_mask` and predicts only its block (`exp_pbp.py:329-346`). Train and
  test shot dates are disjoint by construction; `xg` is assigned only from
  `predict_proba` of the held-out block (`exp_pbp.py:346-349`).
- Downstream player features are strictly prior-game:
  `exp_pbp.py:184-193` (`sort_values(["player_id","date","game_id"])` then
  `s.shift(1).rolling(...)`), `208-216` (same pattern for every share column).
  Min-coverage guards (`min_periods=10/20`) plus missing indicators and 0.0
  imputation (`cover_missing`, `exp_pbp.py:221-233`) — imputation constants are
  fixed, not data-derived.
- Strong empirical guard: perturbing a player-game's own xG to 99 and
  rebuilding all rolling features must not move that game's features —
  `exp_pbp.py:353-363`, `assert ... "same-game leak in rolling features"`.
- Merge is `many_to_one` on `(date, player_id)` with a row-count and
  suffix-column assert (`exp_pbp.py:365-369`).

Caveat: the assert at `exp_pbp.py:333`
(`set(shot_dates[~test_mask]) & set(block_dates)`) is **vacuous** — the
intersection is empty by the definition of `test_mask`. Disjointness is real
(date blocks are disjoint), the assert just proves nothing. Second caveat: the
xG *mapping* is trained on random date blocks, so a shot's xG probability comes
from a model that also saw later shot dates. That is future-label lookahead in
the mapping, standard for OOF stacking, but it is not time-ordered.

### 1.5 interact — PASS

- All 20 columns are products/role splits of columns already on the pre-game
  frame (`exp_interact.py:92-143`): `home × gpg/hgpg/five_gpg/ppg/rank/ewm/
  prev2`, `rest × static F/D`, `rate × opp_gaa/opp_save_pct/opp_wins`.
- `lineup_position_group` is the static F/D letter (`exp_interact.py:107`);
  not the shift-derived unit.
- Opponent strength is the opposing goalie's pre-game ledger row merged on the
  opponent abbreviation (`model.py:325-348`) — same-date, pre-game.
- The date-mean opponent adjustment uses only distinct `(date, opp)` pre-game
  metadata (`exp_interact.py:123-133`), never the row's own game outcome.
- Guards: row count, missing columns, merge-suffix columns
  (`exp_interact.py:135-142`); fold-mismatch check against the no-interaction
  reference OOF, hard-failing on any mismatch
  (`exp_interact.py:330-338`, logs `fold mismatch: 0/92440` for seeds 1 and 3).

### 1.6 exposure — PASS (one documented guard is not implemented)

- `xg_before = cumsum - current`, within `(player, season)`, sorted by date:
  `exp_exposure.py:254-258`. This is strictly prior games — the current row is
  subtracted, and season grouping prevents cross-season carry-over.
- Independent recomputation against `date < row.date` for sample rows:
  `exp_exposure.py:260-280` (raises on mismatch; both seed logs show `OK`).
- The feature is `xg60_prior × r10_toi_pg` (`exp_exposure.py:295`);
  `r10_toi_pg` is the prior-10-game TOI mean (`model.py:427-429`).
- Merge guards: row count, suffix columns (`exp_exposure.py:282-291`).
- Per-shot xG cache is itself OOF: fold column assigned from
  `fold_blocks(challenge_dates, 5, seed)` (`exp_exposure.py:190-198`), model
  trained on `~test_m` with a date-disjointness assert
  (`exp_exposure.py:204-207`).

**Guard gap:** the module docstring and the `shot_xg_player_games` docstring
claim the cached fold column is *verified* against the pick-model fold
assignment (`exp_exposure.py:19-21`, `108-110`), but the function body
(`exp_exposure.py:112-125`) only reads the CSV and groups it — there is **no
such check**. I performed the promised check myself: for both seeds,
challenge-date shots whose cached `fold` disagrees with
`fold_blocks(challenge_dates, 5, seed)` = **0 / 564,522** (seed 1 and seed 3).
So the claim is true today but unenforced; a stale cache from another seed or
fold scheme would pass silently.

---

## 2. Independent re-scoring of OOF CSVs

`model.daily_picks` = per `(date, tims)` NaN-filtered `idxmax`
(`model.py:671-686`); `score_picks` = pooled hits/total (`model.py:689-702`).

**Parser artifact.** The `ensemble`/`base` columns are means of percentile
ranks, so many `(date, tims)` groups are exact or near ties (165–196 groups per
file within 1e-12). pandas' default C float parser perturbs values by 1 ULP and
flips 1–2 picks; `pd.read_csv(..., float_precision="round_trip")` reproduces
the in-memory/log values exactly. Both readings are reported.

| file | column | logged/claimed | default parse | round-trip parse | verdict |
|---|---|---:|---:|---:|---|
| `oof_v16_exposure_seed1.csv` | lgbm (exposure arm) | 922 | **922 / 29.186 %** | 922 | PASS (exact) |
| `oof_v16_exposure_seed1.csv` | ensemble (exposure arm) | 921 / 29.155 % | 922 / 29.186 % | **921 / 29.155 %** | PASS (1 pick, 0.032 pp) |
| `oof_v16_base_seed1.csv` | lgbm (base arm) | 931 / 29.471 % | **931 / 29.471 %** | 931 | PASS (exact) |
| `oof_v16_base_seed1.csv` | ensemble (base arm) | 926 / 29.313 % | — | — | **UNVERIFIED** (column absent) |
| `oof_v16interact_seed3.csv` | lgbm | 932 / 29.503 % | **932 / 29.503 %** | 932 | PASS (exact) |
| `oof_v16interact_seed3.csv` | ensemble | 943 / 29.851 % | 941 / 29.788 % | **943 / 29.851 %** | PASS (2 picks, 0.063 pp) |

Notes:

- `oof_v16_exposure_seed1.csv` contains **one arm only** (the exposure arm,
  16 columns incl. `live/hgb/logreg/lgbm/ensemble/random`). Its base-arm
  counterpart is a *different file*, `oof_v16_base_seed1.csv`, and that file
  carries only `gpg,hgpg,lgbm` — so the claimed base ensemble **926/3159 =
  29.313 %** is reproducible only from the run log
  (`exp_v16_exposure_seed1.log:16-19`), not from any OOF CSV. Baseline gpg and
  hgpg *are* independently reproducible (837 / 886).
- The 943 vs 941 and 921 vs 922 deltas are not wrong scores: recomputing the
  rank-mean from the file's own `hgb/lgbm/logreg` columns reproduces the log
  number, and 196 / 3,159 groups (6.2 %) have a top-1/top-2 gap < 1e-12, so a
  1-ULP parse difference decides 1–2 picks. It is a **tie-break fragility**, and
  it means the `ensemble` column should be rounded (or a `picked` flag stored)
  if the CSVs are to be re-scored deterministically.
- **No mismatch > 0.1 pp was found** (largest = 2 picks = 0.063 pp; 3 picks
  would be 0.095 pp).

**Provenance inconsistency (interact).** `exp_v16_interact_seed3.log:63`
reports ensemble `943/3159 = 29.851 % (folds 29.852 ± 0.366)`, while the
committed `exp_v16_interact_results.csv` row for seed 3 ensemble says
`941 / 29.7879 % (fold_mean 29.7883 ± 0.3922)`. Different fold means ⇒ the log
and the results CSV come from **different runs**. Seed 1 shows the same split
(log 941 ± 2.152 vs CSV 942 ± 2.192). Under the 0.1 pp threshold this is not a
number failure, but the log and the CSV should not be cited as one run.

---

## 3. Baseline integrity and fold pairing

- `gpg = 837 / 3,159 = 26.4957 %` and `hgpg = 886 / 3,159 = 28.0469 %` in
  **all 16** V16 OOF files (every experiment, both seeds), re-scored with
  `daily_picks`. Committed evidence: `exp_v16_exposure_results_seed1.csv` /
  `_seed3.csv` (`837`, `886`), `exp_v16_interact_results.csv` (both seeds), and
  the two logs (`... gpg 837/3159 = 26.496 % ... hgpg 886/3159 = 28.047 %`).
- Every OOF has 92,440 rows / 1,053 challenge dates / 3,159 picks / folds 1–5.
- Same-seed fold pairing: mapping `(date, player_id) → fold` against
  `oof_v16_base_seed{1,3}.csv` gives **0 mismatches / 92,440** for every other
  same-seed OOF (calib, embed, exposure, group3, pbp, interact — all 14 files).
  `fold_blocks` is a seeded shuffle (`model.py:896-901`), so seed 1 and seed 3
  are *not* expected to share folds (827/1,053 dates differ).
- Explicit fold-mismatch guards in logs:
  `exp_v16_interact_seed1.log:74` and `exp_v16_interact_seed3.log:74` both
  `fold mismatch: 0/92440`, and the script hard-fails if non-zero
  (`exp_interact.py:335-338`). `exp_calib.py:402-403` prints the stage-1
  pairing max |diff| vs the saved LGBM column (0 ⇒ identical fold blocks);
  `exp_exposure.py`'s cache check is the documented-but-missing guard in §1.6,
  verified by hand as 0 / 564,522.

---

## 4. Commit hygiene

| check | result |
|---|---|
| `git log --oneline -- smartscore/scripts/backtrack/model.py` | top commit `96f0ff2 Fix refresh pagination ... document V15b`; **no V16 commit touches `model.py`** (`git log v16-audit -- model.py \| grep -i v16` → none). PASS |
| V16 commit range | `493ba96` embed, `3de58b4` calib, `1e94c4a` group3, `e56b159`+`d8b2ef5` pbp, `ef69863` interact, `4f1816c` exposure, `2f5481a` collect — script/notes only |
| `git ls-files data/ml` | **6 files tracked** although `.gitignore:51` ignores `data/ml/`: `oof_v16_base_seed1.csv` (6.8 M), `oof_v16_base_seed3.csv` (18 M), `oof_v16_pbp_seed1.csv` (6.8 M), `oof_v16_pbp_seed3.csv` (6.8 M), `pbp_player_game.csv` (22 M), `pbp_shots.csv` (87 M) ≈ **148 MB** |
| `data/ml/oof_v16_base_seed3.csv` tracked **and modified** | Yes: 18,533,181 B working copy vs 7,111,785 B at HEAD; `git diff --stat` = 92,441 insertions / 92,441 deletions |
| is the modification corruption? | **No.** HEAD is the 8-column pbp base; the working copy is the 16-column exposure base. The six shared value columns (`tims, scored, gpg, hgpg, lgbm, fold`) have **0 differences over 92,440 rows**; only the added columns differ. Cause: `exp_exposure.py:396` writes `oof_v16_{arm}_seed{seed}.csv`, so its `base` arm reuses the pbp run's filename. Same path collision explains the 18 M size. |
| untracked artifacts | `data/moneypuck/` (140 MB) and the other 10 V16 OOF CSVs are untracked (correctly ignored); ~50 `exp_v16_picks_*.pkl` and 5 `exp_v16_perfold_*.csv` are litter in the scripts dir — a `git add -A` would sweep them in |

**Recommendation (NOT executed — read-only audit), exact sequence** to bring
the tracked cache back in line with the `.gitignore` convention:

```bash
cd /home/nathan/code/SmartScore-backtrack
git restore data/ml/oof_v16_base_seed3.csv          # discard the 16-col overwrite
git rm --cached data/ml/oof_v16_base_seed3.csv      # untrack, keep on disk (still ignored)
```

The same two commands apply to the other five tracked `data/ml` files (they are
all ignored-but-force-added); removing them from the index is enough to stop
future churn, but reclaiming the ~148 MB from history needs a rewrite, which is
out of scope here. The root cause to fix in code is the `exp_exposure.py:396`
filename collision (e.g. `oof_v16_exposure_base_seed{seed}.csv` for the base
arm).

---

## 5. V16-collect (`snapshot_projected.py`, `2f5481a`)

### 5.1 Append-only / idempotency / cannot overwrite actuals — PASS

- One row per `(date, player_id)`: `rows` is a dict keyed by that pair
  (`snapshot_projected.py:343-359`).
- `append` uses `upsert(..., on_conflict="date,player_id",
  ignore_duplicates=True)` on pairs *not already stored*
  (`snapshot_projected.py:454-466`) — never merges into an existing row.
- `refresh` PATCHes **only** `lineup_unit, lineup_position_group, pp_unit,
  lineup_status` (`_patch_lineup_fields`, `snapshot_projected.py:401-419`) and
  skips any stored row whose status is in `KEEP_STATUSES =
  ("PROJECTED","CONFIRMED","ACTUAL")` (`snapshot_projected.py:105-107`,
  applied at `474`). No stats/`scored` column is ever in a payload
  (`405-410`), so actuals and stats cannot be overwritten.
- Repeat runs converge: the first refresh stamps `PROJECTED`, after which the
  same rows hit `KEEP_STATUSES` and are skipped (the runbook's "0 appended,
  0 refreshed" re-run is consistent with this).
- Safety gates present: article `datePublished` must equal the requested date
  (`422-428`), explicit `--env prod` (`486`), `--limit` (`489`), `--dry-run`
  (`491`, no writes at `441-444`).

Residual issues (not leaks, but correctness/provenance):

1. **TOCTOU:** the PATCH filters on `date` + `player_id` only
   (`411-417`), not on the status it just read. A row that flips to
   `CONFIRMED`/`ACTUAL` between the `_existing_rows` read (`450`) and the
   PATCH would be written back to `PROJECTED`. The "never touches actuals"
   invariant is therefore not atomic.
2. **No post-game guard / no write timestamp.** Nothing refuses a run after
   the game started, and the payload stores no capture time. Rows written
   post-game would be indistinguishable from pre-game projections later, which
   matters if these rows are ever used as pre-game features in a backtest.
3. **The 3×/day rationale does not hold.** The runbook (`MODEL_NOTES.md`
   V16-collect section) says the 13:00/16:00 runs carry "the freshest RotoWire
   PP units", but after the first run the rows are `PROJECTED` and
   `KEEP_STATUSES` makes refresh skip them — later runs cannot update units on
   already-projected rows.
4. `PAYLOAD_COLUMNS` (`90-100`) is defined but never used; the "never add a
   stats column" invariant is not enforced by that constant, only by the
   hand-built payload dict.

### 5.2 Runbook vs code — partial mismatch

The documented behaviour (append ignore-duplicates, refresh only four columns
on null/`UNKNOWN` rows, article-date gate, explicit `--env`, `--dry-run`,
`--limit`, 10:30/13:00/16:00 ET cron) matches the code, with two exceptions:

- The runbook cron line is `cd /home/nathan/code/SmartScore`, but that checkout
  (`main` @ `990b0b6`) **does not contain** `snapshot_projected.py` (it exists
  only on `backtrack-recon`). The documented command would fail immediately.
- `crontab -l` → `no crontab for nathan`; the schedule is not installed, and no
  `snapshot_projected.log` exists at the documented path.

### 5.3 Did it write prod rows? — **No evidence; the claim is contradicted**

The runbook (`MODEL_NOTES.md` V16-collect section) claims a prod run on
2026-10-20: `UNKNOWN 4,900 → 4,765`, `PROJECTED 255 → 390` (all 2026-10-20),
"collector filled 135 more (incl. defence)".

Live read-only query against both environments for 2026-10-20:

| env | rows | PROJECTED | UNKNOWN | D-unit rows | PP1 rows |
|---|---:|---:|---:|---:|---:|
| prod | 495 | **255** | **240** | **0** | **0** |
| dev | 495 | **390** | **105** | **128** | **22** |

The claimed post-state (390 / 105, defence present) exists on **dev**, not
prod. The collector always writes D1–D3 and normalised PP1/PP2
(`exp_*`-independent: `_article_team_entries` keeps D units at `239-255`,
`_pp_label` at `229-236`), so a 135-row prod write "incl. defence" would have
left D rows and `PP1` labels. Prod has neither; its state is the live
pipeline's own signature (255 PROJECTED, forwards only, verbatim
"POWER PLAY #1"). Conclusion: the runbook's prod verification numbers are the
**dev** post-state, i.e. the prod-write claim is **unverified and contradicted**
by the current prod table. A later pipeline re-publish cannot be fully excluded,
but nothing supports the claimed prod write.

---

## 6. What would need fixing (not done)

1. **embed:** either drop the season-cell ALS block or build it from
   strictly-prior-season cells only; re-run before citing any embed number.
   Seed-3 `+0.57 pp` is leak-inflated → **INVALID**.
2. **pbp/exposure:** if strict causality is required, fit the per-shot xG
   mapping on time-ordered prior blocks only (rolling origin), not random date
   blocks.
3. **exposure:** implement the fold-column verification the docstrings already
   promise (`exp_exposure.py:108-110`); remove the `oof_v16_base_seed*` path
   collision at `exp_exposure.py:396`.
4. **Repo:** `git restore` + `git rm --cached` the six force-added `data/ml`
   files (§4); add the pkl/perfold litter to `.gitignore`.
5. **Collect:** correct the prod verification in `MODEL_NOTES.md`, point the
   cron at the worktree that actually has the script, make the refresh UPDATE
   status-conditional, and add a game-start / capture-time guard before these
   rows are used as pre-game features.

---

## 7. Commands / evidence trail (selected)

```bash
# section 2 — both parsers
.venv/bin/python /tmp/opencode/rescore.py    # default parse
.venv/bin/python /tmp/opencode/rt.py         # float_precision="round_trip"

# section 1.1 — embed future lookahead (per-fold, train dates only)
.venv/bin/python /tmp/opencode/embedleak.py  # 90,450 / 92,440 = 97.85 %

# section 3 — fold-map consistency
.venv/bin/python /tmp/opencode/foldx.py      # seed 1, 0/92,440 each
.venv/bin/python /tmp/opencode/foldx3.py     # seed 3, 0/92,440 each

# section 1.6 — the missing exposure guard, run by hand
.venv/bin/python /tmp/opencode/foldcheck.py  # 0 / 564,522 mismatches, both seeds

# section 4 — tracked-artifact drift
git show HEAD:data/ml/oof_v16_base_seed3.csv > /tmp/opencode/base3_head.csv
.venv/bin/python /tmp/opencode/cmp.py        # 0 diffs in the shared columns

# section 5 — live environment state
.venv/bin/python /tmp/opencode/prodcheck.py  # prod 255/240, 0 D, 0 PP1
```

**Committed file:** `smartscore/scripts/backtrack/notes_v16_audit.md` (this
file). No code, model, or data artifact was changed.
