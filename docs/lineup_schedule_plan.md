# Mid-morning lineup / goalie / injury ingestion

Status: implemented on `lineup-schedule` (PR #134). No deploy yet.
Branch: `lineup-schedule` (worktree `/home/nathan/code/SmartScore-lineup-schedule`).

## 1. When game-day data is actually posted (ET)

| Source | What we read | Typical availability | Notes / evidence |
|---|---|---|---|
| NHL.com daily projections article (`/news/nhl-lineup-projections-2026-27-season`) | Forward lines, D pairs, goalies, scratches, injured list | Page rolls to the next slate **the previous evening**; edited in place through the morning | Live page for the 2026-10-20 slate carried JSON-LD `datePublished` / `dateModified` of `2026-10-19T22:03:00Z` / `22:04:07Z` (18:03 ET on 10-19). The page is up before the 03:00/04:00 ET run, but on the morning of the game it is still *projected* and gets corrected during the day. |
| RotoWire projected goalies (`tables/projected-goalies.php?date=`) | `Confirmed` / `Expected` / `Unknown` | Table pre-populated with `Expected`; flips to `Confirmed` after the morning skate / coach availability | At ~10:05 ET on 2026-10-20 the table returned all 11 games, mostly `Expected`, with a couple already `Confirmed`. So the *rows* exist early; the *confirmation* does not. |
| NHL morning skates | Starter confirmation | Skates typically ~10:30 ET (some ~11:30 ET); coach names the starter afterwards | Confirmations are commonly quoted as **11:00–17:00 ET, most by early afternoon**; some teams only reveal at warmup (~60–90 min before puck drop). DailyFaceoff shows per-goalie confirmation timestamps, e.g. `2026-10-01T16:07:53Z` (12:07 ET). Sources: FantasyTeamAdvice starting-lineups FAQ, BettorEdge starting-goalie guide, MayorsManor morning-skate piece, DailyFaceoff starting-goalies page. |
| RotoWire injury report (`tables/injury-report.php`) | Injury status per player | Rolling; updated when teams announce | No fixed publication time; reflects the same day-of announcements as the goalie table. |
| RotoWire lineups (`/hockey/nhl-lineups.php`) | PP units, goalie designation, injuries | Rolling through the day | Complementary to NHL.com; also pre-populated, corrected closer to puck drop. |

Variability that matters:

- **Early games** (weekend 12:30/13:00 ET): often no morning skate, or a very early one; confirmations can land only at warmup.
- **West-coast games** (22:00 ET / 02:00–03:00 UTC): starters are often confirmed mid-afternoon ET, long after a morning email.
- **Back-to-backs**: backup goalies and late/absent confirmations are common; the second night is the least predictable.
- Therefore no single ingestion time gets everything. A mid-morning run gets the projections; the existing game-start runs remain the mechanism that picks up late confirmations for the site.

Bottom line: a 09:30–10:30 ET fetch is the earliest point where the goalie table has meaningful `Confirmed`/`Expected` content beyond the overnight projection, and the NHL.com article has had its morning corrections.

## 2. Current pipeline map

### Triggers (all UTC; EventBridge Rules have no timezone field)

`templates/template.yaml`:

- L521–529 `DailyStepFunctionTrigger`: `cron(0 8 * * ? *)` → `PlayerProcessingPipeline-${ENV}`.
  **08:00 UTC = 04:00 ET in EDT, 03:00 ET in EST** — this is the "3am run".
- L531–539 `DailySendEmailTrigger`: `cron(0 14 * * ? *)` → `NotifyUsers-${ENV}`.
  **14:00 UTC = 10:00 ET in EDT, 09:00 ET in EST**. The inline comment says "9 AM EST", which is only true in winter.

Per-game triggers are **created at runtime**, not in CloudFormation:

- `smartscore/service.py` L75 `get_teams()` reads today's NHL schedule, collects `startTimeUTC` (L79–81) and calls `schedule_run(start_times)` at L114.
- `smartscore/utility.py` L160 `schedule_run()`: deletes every `TriggerStateMachineAt_*-${ENV}` rule (`delete_expired_rules`, L146), then creates one one-shot rule per distinct start time at **start + 5 minutes** (L167), with `Input {"source": "eventBridge", "last_game": <bool>}` (L189–202). `create_cron_schedule` (L138) emits `cron(M H D M ? YYYY)`.
- `handle_get_teams` runs inside the `GetPlayers` sub-machine, so these rules are created by the day's `first_run` (03:00/04:00 ET) and fire **during the evening games**, not in the morning.

### Run order (`templates/player_processing_pipeline.asl.json`)

```
GetDate -> CheckCompletion
  first_run  -> Backfill -> GetPlayers -> EnsureFirstRunStatus -> GetInjuries -> GetGoalies
                 -> GetLineups -> MakePredictions -> GetTims -> SaveToDb -> UpdateHistory -> PublishDb
  normal_run -> LoadWorkingSet -> GetInjuries -> GetGoalies -> GetLineups
                 -> GetTims -> SaveToDb -> PublishDb
  last_run   -> LoadWorkingSet -> GetInjuries -> GetGoalies -> GetLineups -> GetTims -> PublishToDb
```

- `CheckCompletion` L10–28: `first_run` if no Picks rows for today, `normal_run` if published, `last_run` when the EventBridge input has `last_game: true`.
- `EnsureFirstRunStatus` is a Pass state after the `GetPlayers` sub-machine: that machine returns only `players`/`teams`, so the branch injects `status: first_run` before `GetInjuries`.
- `MakePredictions` runs only on the `first_run` path, **after** `GetInjuries/GetGoalies/GetLineups`.
- `SaveToDb` runs on `first_run` and `normal_run`; `CheckSaveHistory` keeps `UpdateHistory` on `first_run` only. `last_run` goes straight to `PublishToDb`.
- `NotifyUsers` (`templates/notify_users.asl.json`) is just `GetDate -> SendEmails`. `SendEmails` re-reads today's Picks inside the Lambda (`handle_emails`), so the email content is whatever the last `PublishDb` wrote.

### Where the data is fetched and persisted

| Step | Handler | Fetch | Persist |
|---|---|---|---|
| `GetInjuries` | `event_handler.handle_get_injuries` (L372) | RotoWire injury report (`get_injury_data`) | merges into `players` in state; written later by `PublishDb` (Picks) / `SaveToDb` (Player-Snapshots) |
| `GetGoalies` | `event_handler.handle_get_goalies` (L403) | RotoWire projected goalies + NHL stats (`enrich_starting_goalies`) | `goalies` list in state; denormalized into rows at `PublishDb` / `SaveToDb` |
| `GetLineups` | `event_handler.handle_get_lineups` (L435) | NHL.com article + RotoWire lineups (`merge_lineup_data`) | `lineup_unit` / `lineup_position_group` / `pp_unit` / `lineup_status` on each player; written at `PublishDb` / `SaveToDb` |
| `SaveToDb` | `event_handler.handle_save_players` (L278) | — | upserts `Player-Snapshots` on `(date, player_id)` and saves the `Working-*` slices |
| `PublishDb` | `event_handler.handle_publish_db` (L248) | — | wipes/rewrites today's `Picks` rows |

Timezone/date: `service.get_date()` (L55) uses `America/Toronto`, so the date rolls at midnight ET; the Lambda event times are UTC.

## 3. Assessment of the owner's claim

**Mostly scheduling, but not purely.** Since PR #131 (`e6f3500`) `normal_run` no longer skips the data steps: `LoadWorkingSet -> GetInjuries -> GetGoalies -> GetLineups -> GetTims -> PublishToDb`. The pre-#131 README graph (`normal_run -> GetTims`) was stale and has been replaced.

What is genuinely broken by scheduling:

1. The only morning execution is the 03:00/04:00 ET `first_run`. It fetches injuries/goalies/lineups, but at that hour RotoWire is still the overnight projection and confirmations do not exist. Those values are then archived (SaveToDb) and published.
2. The next executions are 5 minutes after each evening puck drop. They do refresh Picks, but the 09:00–10:00 ET email has already gone out using the 03:00 data.

What is **not** scheduling and still needs code changes:

3. `SaveToDb` (Player-Snapshots archive) is `first_run`-only, so a new mid-morning `normal_run` would refresh the website/email but **not** the training archive. Training would keep the 03:00 `lineup_status: UNKNOWN` snapshot.
4. `MakePredictions` runs only on `first_run` and before `GetLineups`. The model does not read lineup fields today (README L129–133), so predictions would not change, but the documented ordering is wrong and blocks any future lineup feature.
5. `handle_load_working_set` returns no `date`, so reusing `SaveToDb` on a `normal_run` would call `save_player_snapshots(date=None)`.

## 4. Proposed design

### Two executions, not one long Wait

Keep two independent EventBridge Rules. A single state machine with a `Wait` until mid-morning would hold a Standard execution for ~10 hours, couple the deterministic phase's failure to the projections phase, and pay for idle transitions. The pipeline is already idempotent and re-entrant (`CheckCompleted` + `Working-*` tables), which is exactly what a second execution needs.

### New schedule (UTC, with ET mapping)

| UTC cron | EDT (Mar–Nov) | EST (Nov–Mar) | Purpose |
|---|---|---|---|
| `cron(0 8 * * ? *)` (existing) | 04:00 | 03:00 | Deterministic phase: backfill, roster/stats, predictions, scoring, archive + publish. Guarantees a slate exists even if the mid-morning run fails. |
| `cron(30 14 * * ? *)` (new) | 10:30 | 09:30 | Projections phase: `normal_run` refresh of injuries, goalies, lineups → archive + publish. |
| `cron(30 15 * * ? *)` (new) | 11:15 | 10:15 | Confirmation phase: second `normal_run` for late morning-skate news. |
| `cron(0 17 * * ? *)` (new) | 13:00 | 12:00 | `LineupRefreshStepFunctionTrigger`: `normal_run` once the article catches up. Archive/site only, no email. |
| `cron(0 20 * * ? *)` (new) | 16:00 | 15:00 | `EveningRefreshStepFunctionTrigger`: final `normal_run` for late lines/PP/goalies. Archive/site only, no email. |
| `cron(45 15 * * ? *)` (moved from 14:00) | 11:45 | 10:45 | `NotifyUsers`, after both morning refreshes. |

Rationale: 09:30–10:30 ET is after the overnight article refresh and at/after most morning skates in summer. In winter the phase lands at 09:30 ET, before a 10:30 skate, but RotoWire's `Expected` projections are already populated; the site still gets `Confirmed` values from the game-start runs. The 13:00/16:00 ET refreshes exist because the NHL.com article can lag a full slate (observed still serving the previous day at 11:24 ET); they correct the archive/site but do not change the email. If exact local time is required year-round, switch these rules to **EventBridge Scheduler** `AWS::Scheduler::Schedule` with `ScheduleExpressionTimezone: America/Toronto` (follow-up, larger change: new resource type + role). EventBridge **Rules** cron is always UTC — do not add a timezone field to a Rule.

Do **not** pass `last_game` on any of the added rules; pass `{"source": "eventBridge"}` so `CheckCompleted` cannot classify them as `last_run`. Repeated `normal_run`s are idempotent: Picks replacement is date-scoped, the archive/Working tables upsert, `UpdateHistory` is skipped, and the stale-slate guard skips forward lines instead of writing yesterday's.

A second cheap `normal_run` at `cron(30 15 * * ? *)` (11:15 EDT / 10:15 EST) picks up starters named after the morning skate; the email at `cron(45 15 * * ? *)` runs 30 minutes later.

### State machine changes (`templates/player_processing_pipeline.asl.json`)

1. **Reorder predictions after lineups on `first_run`:**
   `GetPlayers -> GetInjuries -> GetGoalies -> GetLineups -> MakePredictions -> GetTims`.
   Concretely: `GetPlayersStateMachine`/`CheckIfPlayersOutputIsEmpty` default → `GetInjuries`; `GetLineups.Next` → `MakePredictions`; `MakePredictions.Next` → `GetTims`.
   `MakePredictions` consumes only skater + team stats, so the model output is unchanged; this makes the order match the docs and lets a future lineup feature use the merged data.
2. **Archive on the projections phase:** change `CheckInitialRun` so `normal_run` goes to `SaveToDb` instead of `PublishToDb`.
   Add a small `CheckSaveHistory` Choice after `SaveToDb`:
   - `$.status == "first_run"` → `UpdateHistory`
   - default (`normal_run`) → `PublishToDb`
   This keeps `Player-Snapshots` fresh with the mid-morning lines while keeping history/metrics writes on the deterministic path (`write_historic_db` is already a no-op for a date already present, but skipping it is cheaper and avoids touching metrics twice).
   `last_run` can stay on `PublishToDb`; routing it through `SaveToDb` too would capture the post-warmup lineup and is a reasonable later change, but it is not needed for this goal.
3. `SaveToDb` already calls `save_working_set`, which is an idempotent upsert; re-running it on `normal_run` is harmless and refreshes the working slices.

### Handler change (`smartscore/event_handler.py`)

`handle_load_working_set` currently returns `status`, `players`, `teams`. Add `"date": get_date()` to its output (it already imports `get_date`), so `handle_save_players` receives the date on a `normal_run`. Defensively also change `handle_save_players` to `date = event.get("date") or get_date()`.

### Idempotency

- **Run classification:** `CheckCompleted` uses whether today's Picks exist. 03:00 publishes → 10:30 is `normal_run`. If 03:00 failed, 10:30 becomes `first_run` and runs the full chain (including `GetTeams`/`schedule_run`), which is a valid fallback.
- **Archive:** `save_player_snapshots` upserts on `(date, player_id)`; `scored` is deliberately omitted so re-writes do not erase labels.
- **Picks:** `save_to_db` wipes and rewrites today's rows, so repeated `PublishDb` is a full replace, not a duplicate.
- **Working tables:** upsert.
- **Game rules:** `schedule_run` deletes all `TriggerStateMachineAt_*` rules for the env then recreates deterministic names (`%Y%m%d%H%M`), so a fallback `first_run` at 10:30 does not duplicate the evening triggers. The projections phase itself does not call `schedule_run`.
- **Notifications:** unchanged one-shot cron; the pipeline does not trigger `NotifyUsers`, so the evening `normal_run`/`last_run` executions cannot re-send email.

### Ordering of predictions and notifications

- Predictions are recomputed only on `first_run` (03:00) and now after the data merge; they remain valid all day because the model does not use lineups/injuries. No need to re-run `MakePredictions` at 10:30 (and re-running it would require reloading stats).
- The 10:30 projections execution must finish before `NotifyUsers` at 15:45 UTC. The 75-minute window covers the ~1–3 minute pipeline. If the phase is slow, fails, or Picks are empty, `handle_emails` logs and skips the send (`{"skipped": true}`) rather than raising; alarm on both failed executions and skipped sends.
- `PublishDb` rewrites only today's Picks (delete scoped to the date); the 75-minute gap avoids overlapping the email read with the rewrite.

## 5. Concrete file/line change list (implemented)

| File | Change |
|---|---|
| `templates/template.yaml` L521–610 | Add `ProjectionsStepFunctionTrigger` `cron(30 14 * * ? *)`, `ConfirmationStepFunctionTrigger` `cron(30 15 * * ? *)`, `LineupRefreshStepFunctionTrigger` `cron(0 17 * * ? *)` and `EveningRefreshStepFunctionTrigger` `cron(0 20 * * ? *)` → `PlayerProcessingPipeline-${ENV}`, Input `{"source":"eventBridge"}`, same `EventBridgeInvokeRole`. Move `DailySendEmailTrigger` to `cron(45 15 * * ? *)`. Update comments with the EDT/EST mapping. |
| `templates/player_processing_pipeline.asl.json` | Reorder `MakePredictions` after `GetLineups`; add `EnsureFirstRunStatus` after the `GetPlayers` sub-machine; route `normal_run` to `SaveToDb`; add `CheckSaveHistory` (`first_run → UpdateHistory`, else → `PublishToDb`). |
| `smartscore/event_handler.py` | `LoadWorkingSet` returns `date`; `Backfill`/`ParseData`/data handlers carry `status`; `GetTims`/`SavePlayers` require it; failed lineup/injury/goalie fetches preserve prior values; empty picks skip the email. |
| `smartscore/player_archive.py` | Omit `opp_goalie_*` from the snapshot payload when the row carries none, so a failed goalie fetch cannot null the archive. |
| `smartscore/utility.py` | `save_to_db(players, date)` scopes the Picks delete to that date. |
| `README.md` L91–113, L129–133 | Replace the stale "Known gap" graph with the post-#131 graph + the two-phase schedule; drop the "MakePredictions runs before GetLineups" caveat. |
| `tests/unit/test_pipeline_schedule.py` | Rule hours, ASL trace per status, handler-chain status propagation, first-run entry, data preservation. |

No DB migration is needed (no schema change).

## 6. Risks and fallbacks

| Risk | Mitigation |
|---|---|
| Projections still preliminary at 09:30–10:30 ET | The 03:00 deterministic run guarantees a published slate. A fetch failure now preserves the previous lineup/injury/goalie fields instead of stamping UNKNOWN/HEALTHY. Log coverage and retry at the 15:15 confirmation phase. |
| Early games (weekend 12:30/13:00 ET) | Email at 11:45/10:45 ET precedes a 12:30 ET puck drop by ~45–105 min, and those teams often skip the morning skate. Moving the pair earlier for early slates is a follow-up (derive the cron from the first `startTimeUTC`, as `schedule_run` already does). |
| West-coast confirmations after the email | Out of scope: the game-start `normal_run` still refreshes Picks; the morning email is projection-based by design. |
| Back-to-backs / backup goalies | Same as above; `Expected` values are what RotoWire publishes. Do not treat a missing confirmation as an error. |
| 03:00 run fails → 10:30 run becomes `first_run` | Full chain runs, including `GetTeams`/`schedule_run`; `NotifyUsers` may race it. Mitigation: 90-minute gap to the email; alarm on failures. Also archive/status propagation is now covered by chain-level tests. |
| DST: fixed UTC cron drifts by 1 h | Documented; 09:30–10:45 ET band still satisfies "mid-morning". Use EventBridge Scheduler with `America/Toronto` if exactness is required. |
| `schedule_run` deletes all `TriggerStateMachineAt_*` rules for the env | Pre-existing behavior; the projections phase does not call it. A concurrent fallback `first_run` could re-create rules while a game trigger fires — low risk, unchanged by this work. |
| `PublishDb` rewrite vs. email read | The Picks delete is now scoped to the current date, and `handle_emails` logs and skips instead of raising when Picks are unexpectedly empty, so a failed publish cannot empty another date or fail the notification run. |

## 7. Verification performed

1. `uv run pytest -q --ignore=tests/unit/test_rust.py` → 292 passed (Rust extension is built by CI; locally a test-only stub is used because the host has no C linker).
2. `statelint` on all ASL files → exit 0.
3. `cfn-lint templates/template.yaml` → only the pre-existing `W1020` at L158.
4. `ruff check` / `ruff format --check` → clean.
5. Tests cover the ASL routing per status, the real first-run entry (CheckCompleted → Backfill → ParseData → EnsureFirstRunStatus), handler-chain status propagation, and lineup/goalie/injury data preservation on failure.
6. Remaining manual step: deploy to **dev** only, then `aws events list-rules` and a manual `PlayerProcessingPipeline-dev` execution after 10:00 ET to confirm `normal_run`, refreshed `lineup_status` on Picks, and updated `Player-Snapshots` rows.
