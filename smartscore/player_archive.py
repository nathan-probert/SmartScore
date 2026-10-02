"""Long-run player snapshot archive in Supabase, replacing MongoDB (#113).

This is the write/read path that used to go through the Cloudflare
``smartscore-api`` worker and the MongoDB collection behind it, both of which
are now retired. Every function here maps one-to-one onto a worker route that
has been removed:

======================================  ====================================
Retired worker route                    Replacement here
======================================  ====================================
``POST /players``                       :func:`save_player_snapshots`
``GET /players?date=``                  :func:`get_players_for_date`
``GET /unscored-dates``                 :func:`get_unscored_dates`
``POST /backfill-scored``               :func:`backfill_scored`
``DELETE /game``                        :func:`delete_game_snapshots`
``GET /all-players``                    :func:`get_all_player_snapshots`
======================================  ====================================

Two behavioural differences from the Mongo write path, both deliberate:

* **Idempotent, not append-only.** The worker used ``insertMany`` with no unique
  constraint, so re-uploading a date appended a second full copy of the roster.
  Every write here is an upsert on the ``(date, player_id)`` primary key, and
  :func:`get_players_for_date` therefore returns exactly one row per player.
* **Keys are ``player_id``, not ``id``.** Mongo documents carried the NHL player
  id in ``id``; Supabase's other tables use a positional ``id`` ordinal, so the
  archive uses ``player_id`` throughout and has no ordinal column at all.

All access uses the service-role client: ``Player-Snapshots`` has RLS enabled and
no policies, so the anon key cannot read the training set.
"""

import time

from aws_lambda_powertools import Logger
from postgrest.exceptions import APIError

from config import ENV, SUPABASE_ADMIN_CLIENT

logger = Logger()

SNAPSHOT_TABLE = f"Player-Snapshots-{ENV}"

MAX_RETRIES = 5
BASE_RETRY_DELAY_SECONDS = 1

# PostgREST caps rows per response (commonly 1000), so the training export pages
# instead of asking for the whole table in one shot. The worker's /all-players
# had no limit at all and base64'd the entire collection into one response.
PAGE_SIZE = 1000

# Columns written by :func:`save_player_snapshots`, in a fixed order. The player
# payload that reaches SaveToDb is assembled by a chain of Lambdas that each bolt
# on fields, so it carries whatever those steps happened to add; PostgREST rejects
# the whole batch on an unknown column, so the payload is projected onto this
# allowlist rather than passed through.
#
# `scored` is deliberately absent. PostgREST's merge-duplicates resolution builds
# `DO UPDATE SET` from the union of keys across the batch, so including `scored`
# here would reset it to null on every re-upload and discard backfill results.
SNAPSHOT_COLUMNS = (
    "name",
    "team_name",
    "home",
    "gpg",
    "hgpg",
    "five_gpg",
    "hppg",
    "tgpg",
    "otga",
    "otshga",
    "injury_status",
    "injury_desc",
    "tims",
    "opp_goalie_name",
    "opp_goalie_team",
    "opp_goalie_status",
    "opp_goalie_confirmed",
    "opp_goalie_nhl_id",
    "opp_goalie_gaa",
    "opp_goalie_save_pct",
    "opp_goalie_record",
    "opp_goalie_shutouts",
    "opp_goalie_games_played",
    "lineup_unit",
    "lineup_position_group",
    "pp_unit",
    "lineup_status",
)

# Columns that must be present and non-null for a row to be storable. The worker
# validated the equivalent three fields (plus that the id was a number).
REQUIRED_FIELDS = ("date", "player_id", "name")


def _retry(operation, description):
    """Run a Supabase operation with bounded exponential backoff.

    Mirrors ``utility.exponential_backoff_supabase_request``: ``APIError`` and
    ``ValueError`` are re-raised rather than retried, because a schema or payload
    rejection will not fix itself and retrying only delays a hard failure.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return operation()
        except (APIError, ValueError):
            logger.error(f"{description} failed with a non-retryable error")
            raise
        except Exception as e:  # noqa: BLE001
            if attempt == MAX_RETRIES - 1:
                logger.error(f"{description} failed after {MAX_RETRIES} attempts: {e}")
                raise
            wait_time = BASE_RETRY_DELAY_SECONDS * (2**attempt)
            logger.warning(f"{description} failed: {e}. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    raise Exception(f"Max retries reached. {description} failed.")


def _table():
    return SUPABASE_ADMIN_CLIENT.table(SNAPSHOT_TABLE)


def _to_snapshot(player, date):
    """Project one pipeline player dict onto the archive columns.

    ``id`` becomes ``player_id``: Mongo stored the NHL id in ``id``, while the
    Supabase tables that consume this data key on ``player_id``. ``stat`` is
    dropped for parity with the worker's ``filterPlayerFields``, so the training
    set keeps the columns it has always had. Missing optional fields are written
    as null rather than omitted, so a re-upload refreshes a stale value instead of
    leaving it behind. Returns ``None`` when a required field is missing, which is
    logged rather than sent as a null the database would reject.
    """
    snapshot = {"date": player.get("date") or date, "player_id": player.get("id")}
    for column in SNAPSHOT_COLUMNS:
        snapshot[column] = player.get(column)

    if any(snapshot.get(field) is None for field in REQUIRED_FIELDS):
        logger.warning(f"Skipping player snapshot missing a required field: {player.get('name')!r}")
        return None

    return snapshot


def save_player_snapshots(players, date=None):
    """Store a date's roster as one snapshot row per player.

    Upserts on the ``(date, player_id)`` primary key, so re-running the pipeline
    for a date refreshes that date's rows in place instead of appending duplicate
    rosters. A new row starts ungraded (``scored`` null);
    :func:`backfill_scored` fills it in once the game is final.

    Replaces the worker's ``POST /players``. Returns the number of rows written.
    """
    if not players:
        logger.info("No players to archive")
        return 0

    snapshots = [snapshot for snapshot in (_to_snapshot(player, date) for player in players) if snapshot]
    if not snapshots:
        logger.warning("Every player was missing a required field; nothing archived")
        return 0

    def _upsert():
        return (
            _table()
            .upsert(
                snapshots,
                on_conflict="date,player_id",
                returning="minimal",
            )
            .execute()
        )

    _retry(_upsert, f"Archiving {len(snapshots)} player snapshot(s) for {date}")

    logger.info(f"Archived {len(snapshots)} player snapshot(s) for {date}")
    return len(snapshots)


def get_players_for_date(date):
    """Return every archived snapshot for a date.

    Replaces the worker's ``GET /players?date=``.
    """
    return _retry(
        lambda: _table().select("*").eq("date", date).execute().data,
        f"Fetching player snapshots for {date}",
    )


def get_unscored_dates():
    """Return dates with at least one ungraded player, as YYYY-MM-DD strings.

    Replaces the worker's ``GET /unscored-dates``, a Mongo
    ``distinct("date", {scored: null})``. PostgREST has no DISTINCT, so the
    distinct-ing happens here; the partial index on ungraded rows keeps the scan
    cheap.
    """
    rows = _retry(
        lambda: _table().select("date").is_("scored", None).execute().data,
        "Fetching ungraded dates",
    )
    return sorted({row["date"] for row in rows if row.get("date")})


def backfill_scored(date, scored_player_ids):
    """Mark every snapshot on a date as scored (1) or not scored (0).

    Covers the whole date, matching the worker's two ``updateMany`` calls: ids in
    the list become 1, everything else on that date becomes 0. Replaces the
    worker's ``POST /backfill-scored``.

    Ids come from the NHL score feed and are coerced to int, since ``player_id``
    is BIGINT and a non-numeric id would fail the whole filter rather than merely
    fail to match a row.
    """
    ids = _coerce_player_ids(scored_player_ids)
    logger.info(f"Backfilling {date}: {len(ids)} scorer(s)")

    def _mark_scored():
        return _table().update({"scored": 1}, returning="minimal").eq("date", date).in_("player_id", ids).execute()

    def _mark_unscored():
        query = _table().update({"scored": 0}, returning="minimal").eq("date", date)
        # An empty id list legitimately means "nobody scored on this date", and
        # PostgREST renders `in.()`/`not.in.()` with no values as invalid SQL.
        # Drop the filter instead of sending it; the whole date becomes 0.
        if ids:
            query = query.not_.in_("player_id", ids)
        return query.execute()

    # Same reason on the other side: with no scorers there is nothing to mark,
    # so the `in.()` filter is never built.
    if ids:
        _retry(_mark_scored, f"Marking scorers for {date}")
    _retry(_mark_unscored, f"Marking non-scorers for {date}")


def delete_game_snapshots(date, team_names):
    """Delete every snapshot on a date belonging to the given teams.

    Used when a game is postponed (PPD): those players should not sit in the
    archive indefinitely as unscored. Replaces the worker's ``DELETE /game``.
    Returns the number of rows deleted.

    Matches on ``team_name``, which is the NHL *schedule* place name ("Toronto")
    - ``merge_players_and_teams`` keeps it but ``TEAM_MERGE_EXCLUDED_FIELDS``
    strips ``team_abbr`` before the payload is built. The worker's delete
    filtered on ``team_abbr``, a column its own upload path never wrote, so it
    matched nothing and postponed players were never removed.

    That also means the abbreviation is *not* recoverable from a snapshot row.
    The score feed, which is the only source ``backfill_dates`` has for a PPD
    game, reports ``homeTeam``/``awayTeam`` abbreviations and no place name, so
    callers must translate them through the date's schedule
    (``service.resolve_team_names``) rather than passing the raw abbreviations.
    """
    team_names = [name for name in team_names if name]
    if not team_names:
        logger.warning(f"No team names given; skipping postponed-game delete for {date}")
        return 0

    response = _retry(
        lambda: _table().delete().eq("date", date).in_("team_name", team_names).count("exact").execute(),
        f"Deleting postponed-game snapshots for {date}",
    )

    deleted = response.count or 0
    if not deleted:
        logger.warning(f"Postponed-game delete for {date} matched no rows for teams {team_names}")
    return deleted


def get_all_player_snapshots():
    """Return every archived snapshot, paged, for training-data export.

    Replaces the worker's ``GET /all-players``. Ordering by the primary key keeps
    paging stable, which an unordered query cannot guarantee.
    """
    snapshots = []
    start = 0

    while True:
        page = _retry(
            lambda offset=start: (
                _table()
                .select("*")
                .order("date")
                .order("player_id")
                .range(offset, offset + PAGE_SIZE - 1)
                .execute()
                .data
            ),
            f"Fetching player snapshots at offset {start}",
        )
        snapshots.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    logger.info(f"Fetched {len(snapshots)} player snapshot(s) from {SNAPSHOT_TABLE}")
    return snapshots


def _coerce_player_ids(player_ids):
    """Coerce NHL goal scorers to ints, dropping anything non-numeric.

    ``player_id`` is BIGINT and PostgREST passes filter values through as
    strings, so a non-numeric id would fail the query outright rather than just
    fail to match a row.
    """
    ids = []
    for player_id in player_ids or []:
        try:
            ids.append(int(player_id))
        except (TypeError, ValueError):
            logger.warning(f"Ignoring non-numeric player id from score feed: {player_id!r}")
    return ids
