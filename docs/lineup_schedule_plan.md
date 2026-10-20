# Mid-morning lineup / goalie / injury ingestion

Status: research + design only. No production code or deploy in this change.
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
  first_run  -> Backfill -> GetPlayers -> MakePredictions -> GetInjuries -> GetGoalies
                 -> GetLineups -> GetTims -> SaveToDb -> UpdateHistory -> PublishDb
  normal_run -> LoadWorkingSet -> GetInjuries -> GetGoalies -> GetLineups
                 -> GetTims -> PublishDb
  last_run   -> LoadWorkingSet -> ... same ... -> PublishDb
```

- `CheckCompletion` L10–28: `first_run` if no Picks rows for today, `normal_run` if published, `last_run` when the EventBridge input has `last_game: true`.
- `MakePredictions` L77–80 runs only on the `first_run` path and **before** `GetInjuries/GetGoalies/GetLineups`.
- `SaveToDb` L122–125 runs only on `first_run`; `CheckInitialRun` L102–120 sends `normal_run` straight to `PublishDb`.
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

**Mostly scheduling, but not purely.** Since PR #131 (`e6f3500`) `normal_run` no longer skips the data steps: `LoadWorkingSet -> GetInjuries -> GetGoalies -> GetLineups -> GetTims -> PublishDb`. The README's "Known gap" block (L91–113) still describes the old graph (`normal_run -> GetTims`) and is **stale**.

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
| `cron(30 15 * * ? *)` (moved from 14:00) | 11:30 | 10:30 | `NotifyUsers`, one hour after the projections phase. |

Rationale: 09:30–10:30 ET is after the overnight article refresh and at/after most morning skates in summer. In winter the phase lands at 09:30 ET, before a 10:30 skate, but RotoWire's `Expected` projections are already populated; the site still gets `Confirmed` values from the game-start runs. If exact local time is required year-round, switch these two rules to **EventBridge Scheduler** `AWS::Scheduler::Schedule` with `ScheduleExpressionTimezone: America/Toronto` (follow-up, larger change: new resource type + role). EventBridge **Rules** cron is always UTC — do not add a timezone field to a Rule.

Do **not** pass `last_game` on the new rule; pass `{"source": "eventBridge"}` so `CheckCompleted` cannot classify it as `last_run`.

Optional (not required by this goal): a second cheap `normal_run` at `cron(30 16 * * ? *)` (12:30 EDT / 11:30 EST) to pick up late confirmations before the site traffic peak. It would reuse the same rule target; no new code.

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
- The 10:30 projections execution must finish before `NotifyUsers`. A 60-minute window covers the ~1–3 minute pipeline, but if the phase is slow or retried the email still sends from the 03:00 Picks (stale, not empty). Add a CloudWatch alarm on failed executions as the guard (follow-up).
- `PublishDb` is a wipe+rewrite of today's Picks; do not run `NotifyUsers` concurrently. The 60-minute gap avoids this.

## 5. Concrete file/line change list (next turn)

| File | Change |
|---|---|
| `templates/template.yaml` L521–539 | Add `ProjectionsStepFunctionTrigger` `cron(30 14 * * ? *)` → `PlayerProcessingPipeline-${ENV}`, Input `{"source":"eventBridge"}`, same `EventBridgeInvokeRole`. Move `DailySendEmailTrigger` to `cron(30 15 * * ? *)`. Update both comments with the EDT/EST mapping. |
| `templates/player_processing_pipeline.asl.json` | Reorder `MakePredictions` after `GetLineups`; route `normal_run` to `SaveToDb`; add `CheckSaveHistory` (`first_run → UpdateHistory`, else → `PublishToDb`). |
| `smartscore/event_handler.py` L89–120 | `handle_load_working_set` returns `"date": get_date()`. |
| `smartscore/event_handler.py` L278–320 | `handle_save_players`: `date = event.get("date") or get_date()` fallback. |
| `README.md` L91–113, L129–133 | Replace the stale "Known gap" graph with the post-#131 graph + the two-phase schedule; drop the "MakePredictions runs before GetLineups" caveat. |
| `tests/unit/test_notify_users_state_size.py` L75–110 | Update `CheckInitialRun` expectations (`normal_run` → `SaveToDb`) and assert the new `CheckSaveHistory` routing. |
| `tests/unit/test_template_env_vars.py` (or a new ASL/template test) | Assert the two cron expressions and that `GetLineups` precedes `MakePredictions`. |
| `tests/unit/test_event_handler.py` | Assert `LoadWorkingSet` output carries `date`. |

No DB migration is needed (no schema change).

## 6. Risks and fallbacks

| Risk | Mitigation |
|---|---|
| Projections still preliminary at 09:30–10:30 ET | The 03:00 deterministic run guarantees a published slate; the merge already degrades to `lineup_status: UNKNOWN` rather than failing. Log coverage and consider a 16:30 UTC refresh (optional). |
| Early games (weekend 12:30/13:00 ET) | Email at 11:30/10:30 ET still precedes puck drop by ~1–2 h; if it is too tight, move the projections/notify pair earlier **only** when the slate's first `startTimeUTC` is before ~17:00 UTC (follow-up: derive the cron from the schedule, as `schedule_run` already does). |
| West-coast confirmations after the email | Out of scope: the game-start `normal_run` still refreshes Picks; the morning email is projection-based by design. |
| Back-to-backs / backup goalies | Same as above; `Expected` values are what RotoWire publishes. Do not treat a missing confirmation as an error. |
| 03:00 run fails → 10:30 run becomes `first_run` | Full chain runs, including `GetTeams`/`schedule_run`; `NotifyUsers` may race it. Mitigation: 60-minute gap; alarm on failures; optionally move notify to 16:00 UTC if this proves common. |
| DST: fixed UTC cron drifts by 1 h | Documented; 09:30–10:30 ET band still satisfies "mid-morning". Use EventBridge Scheduler with `America/Toronto` if exactness is required. |
| `schedule_run` deletes all `TriggerStateMachineAt_*` rules for the env | Pre-existing behavior; the projections phase does not call it. A concurrent fallback `first_run` could re-create rules while a game trigger fires — low risk, unchanged by this work. |
| `PublishDb` wipe+rewrite vs. email read | 60-minute separation; no concurrent execution expected on a weekday morning. |

## 7. Verification plan for the implementation turn

1. `uv run make test` and `uv run make lint` (CI also runs these).
2. Parse both ASL files (`json.load`) and deploy to **dev** only; confirm `aws stepfunctions describe-state-machine` shows the new graph.
3. `aws events list-rules` in dev: exactly one `ProjectionsStepFunctionTrigger`, one daily trigger, one email trigger; no duplicate `TriggerStateMachineAt_*` rules after a manual `first_run`.
4. Manually start `PlayerProcessingPipeline-dev` after 10:00 ET: expect `normal_run`, `Player-Snapshots-dev` rows for today refreshed with non-null `lineup_unit` for a majority of skaters, and `Picks-dev` carrying the same.
5. Run `NotifyUsers-dev` and confirm the email contains the refreshed lines/goalie fields.
6. Re-run the same execution to confirm idempotency: row counts identical, `scored` labels preserved.
