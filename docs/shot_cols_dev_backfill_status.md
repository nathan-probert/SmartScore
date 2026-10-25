# Shot/TOI + club columns: dev-only backfill status (2026-10-24)

Status of the 16-column migration/backfill on **Player-Snapshots-backtrack-dev**
only. The spec lives on `feat/supabase-spec` / PR #138
(`docs/supabase_shot_columns.md`) and is **not duplicated here**; prod is NOT
touched this run.

## What ran

1. **DDL (Step 1)** — `supabase/migrations/20261024_add_shot_columns_player_snapshots_backtrack.sql`
   (16 columns, `__ENV__` placeholder) was applied to **dev** by the
   `migrate-adhoc` GitHub Actions workflow (run 40122024730, ENV=dev): all 16
   columns verified present on the dev table.
2. **Backfill (Step 2)** — `smartscore/scripts/backtrack/publish_shot_cols.py --env dev`:
   617,310 rows upserted in 1,235 batches of 500 with
   `on_conflict="date,player_id", returning="minimal"`, 0 errors, 669s of
   upload time (13m19s wall including local `shot_features()` compute over the
   692,634-row store). NaN→None via `astype(object)` before upsert; `name`
   rides along because PostgREST enforces NOT NULL even on PK-conflict
   sparse upserts (keys-only 400s with `null value in column "name"`).
3. **Spot check (Step 3)** — one-shot QA tool `spot_check_shot_cols.py`
   (deleted after use; see it by sha in git history): 1,000 random
   (date, player_id) rows read back from Supabase and compared against a
   fresh verbatim `shot_features()` recompute. **0 mismatches across all 16
   columns** — retained here as the final QA evidence; worst relative float
   diff 4.6e-15 (double round-trip noise, tolerance 1e-9);
   `career_games`/`team_abbrev`/`opponent_abbrev` exact. Row count re-proven
   617,310 via full PK key dump (no rows added/deleted).

## Non-null coverage (from the backfill dry run)

`career_games`/`team_abbrev` 100%; shot/TOI rolling block 98.1% (12,387
season-first-of-season rows NaN by design); `career_goals_pg`/`career_shots_pg`/
r-windows/ewm 99.6% (career-debut rows NaN); `opponent_abbrev` 99.995% (32
store rows the source game log lacked the field for — published NULL, mirrors
`club_map()`'s local null-club behavior); `sd_sh_pct` 97.2% (also
shots_before=0 rows).

## Divergences from the spec doc (PR #138)

* The spec's `~447k non-null` estimate for the `sd_*` block was low — actual
  605,393 non-null (98.1%); the NaN policy (season-first + career-first) is
  as specified, the estimate just miscounted season boundaries.
* Batch size is 500 (spec §5 / publish() convention), not the 1,000–5,000
  mentioned in the run request — PostgREST's hard cap is 1,000 rows/request;
  500 keeps request sizes equal to the existing publish path.
* `count=exact` on the table times out under PostgREST's statement timeout
  shortly after a bulk write; the authoritative row-count check is the full
  PK key enumeration (PK guarantees one row per key). Script falls back
  automatically.

## Prod gate

`Player-Snapshots-backtrack-prod` is untouched. Promotion when the owner
authorizes: apply the same migration through the standing `deploy.yml`
migrate job (the transient `migrate-adhoc` workflow that applied it to dev
has been deleted, superseded by the standing job), then
`uv run python smartscore/scripts/backtrack/publish_shot_cols.py --env prod`.
Backfill is idempotent (upsert on the PK; safe to re-run/reeinterrupt).
