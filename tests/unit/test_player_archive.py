"""Unit tests for the Supabase player snapshot archive.

``SUPABASE_ADMIN_CLIENT`` is a MagicMock from ``conftest``, so these tests drive
the module through a recording stand-in for the postgrest query builder. That
is what lets them assert the part that actually matters here: the *filters*
each function builds. A snapshot bug that is invisible in a happy-path return
value - an ``in.()`` with no values, a delete that never runs - shows up in the
recorded call chain.
"""

from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

import player_archive
from player_archive import (
    PAGE_SIZE,
    SNAPSHOT_COLUMNS,
    SNAPSHOT_TABLE,
    backfill_scored,
    delete_game_snapshots,
    get_all_player_snapshots,
    get_players_for_date,
    get_unscored_dates,
    save_player_snapshots,
)


class FakeResponse:
    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class _Recorder:
    """Callable that records one builder method and returns the query again.

    postgrest-py exposes chained filters as properties (``.not_.in_(...)``), so a
    recorded method has to be reachable both as an attribute and as a callable.
    """

    def __init__(self, query, name):
        self._query = query
        self._name = name

    def __call__(self, *args, **kwargs):
        self._query.calls.append((self._name, args, kwargs))
        return self._query

    def __getattr__(self, name):
        return _Recorder(self._query, name)


class FakeQuery:
    """Records the builder chain and hands back queued responses per execute()."""

    def __init__(self, responses=None):
        self.calls = []
        self.accesses = []
        self.executions = 0
        self._responses = list(responses or [])

    def __getattr__(self, name):
        self.accesses.append(name)
        return _Recorder(self, name)

    def execute(self):
        self.calls.append(("execute", (), {}))
        self.executions += 1
        if not self._responses:
            return FakeResponse()
        return self._responses.pop(0)

    def call_names(self):
        return [name for name, _, _ in self.calls]

    def args_for(self, name):
        return [args for called, args, _ in self.calls if called == name]


def install_query(monkeypatch, responses=None):
    """Point the module at a fresh recording query and return it."""
    query = FakeQuery(responses)
    client = MagicMock()
    client.table.return_value = query
    monkeypatch.setattr(player_archive, "SUPABASE_ADMIN_CLIENT", client)
    return query


def call_kwargs(query, name):
    matches = [kwargs for called, _, kwargs in query.calls if called == name]
    assert matches, f"{name} was never called; chain was {query.call_names()}"
    return matches[0]


def upserted_rows(query, call=0):
    """The row list handed to the Nth ``upsert`` on this query."""
    return query.args_for("upsert")[call][0]


def test_save_upserts_on_the_primary_key(monkeypatch):
    """Rows are upserted on (date, player_id), so re-uploading cannot duplicate."""
    query = install_query(monkeypatch)
    players = [{"id": 8478402, "name": "Auston Matthews", "team_name": "Toronto", "gpg": 0.5}]

    assert save_player_snapshots(players, date="2026-04-15") == 1
    assert call_kwargs(query, "upsert")["on_conflict"] == "date,player_id"

    save_player_snapshots(players, date="2026-04-15")

    # Same input again produces the identical row, which the primary key
    # collapses in place - the behaviour Mongo's insertMany could not offer.
    first_rows, second_rows = upserted_rows(query, 0), upserted_rows(query, 1)
    assert first_rows == second_rows
    assert len(first_rows) == 1


def test_save_maps_id_to_player_id_and_drops_unknown_fields(monkeypatch):
    """Mongo's `id` becomes `player_id`, `stat` never reaches PostgREST."""
    query = install_query(monkeypatch)
    players = [
        {
            "id": 8478402,
            "name": "Auston Matthews",
            "team_name": "Toronto",
            "date": "2026-04-15",
            "stat": 0.61,
            "unexpected": "boom",
        }
    ]

    save_player_snapshots(players)

    (row,) = upserted_rows(query)
    assert row["player_id"] == 8478402
    assert row["date"] == "2026-04-15"
    assert "stat" not in row
    assert "unexpected" not in row
    assert set(row) == {"date", "player_id", *SNAPSHOT_COLUMNS}


def test_save_omits_scored_so_a_reupload_cannot_wipe_a_backfill(monkeypatch):
    """`scored` is left out of the payload, so PostgREST does not reset it to null."""
    query = install_query(monkeypatch)

    save_player_snapshots([{"id": 1, "name": "Player One", "scored": 1}], date="2026-04-15")

    (row,) = upserted_rows(query)
    assert "scored" not in row


def test_save_skips_players_missing_a_required_field(monkeypatch):
    """A row missing date/player_id/name is dropped, not sent as a null."""
    query = install_query(monkeypatch)
    players = [
        {"id": 1, "name": "Player One"},
        {"name": "No Id"},
        {"id": 2},
    ]

    assert save_player_snapshots(players, date="2026-04-15") == 1
    (row,) = upserted_rows(query)
    assert row["player_id"] == 1


def test_save_skips_when_nothing_is_storable(monkeypatch):
    """Every player missing a required field must not issue an empty upsert."""
    query = install_query(monkeypatch)

    assert save_player_snapshots([{"name": "No Id"}], date="2026-04-15") == 0
    assert "upsert" not in query.call_names()


def test_save_ignores_an_empty_roster(monkeypatch):
    query = install_query(monkeypatch)

    assert save_player_snapshots([], date="2026-04-15") == 0
    assert "upsert" not in query.call_names()


def test_coerce_player_ids_drops_non_numerics():
    """A non-numeric id would fail the whole filter, not just fail to match."""
    assert player_archive._coerce_player_ids(["8478402", "unknown", None, 8478403, ""]) == [8478402, 8478403]
    assert player_archive._coerce_player_ids(None) == []


def test_get_players_for_date_filters_on_date(monkeypatch):
    query = install_query(monkeypatch, [FakeResponse(data=[{"player_id": 1, "scored": None}])])

    assert get_players_for_date("2026-04-15") == [{"player_id": 1, "scored": None}]
    assert query.args_for("eq") == [("date", "2026-04-15")]


def test_get_unscored_dates_deduplicates(monkeypatch):
    """One date with many ungraded players must be reported once."""
    query = install_query(
        monkeypatch,
        [
            FakeResponse(
                data=[
                    {"date": "2026-04-15"},
                    {"date": "2026-04-14"},
                    {"date": "2026-04-15"},
                    {"date": None},
                ]
            )
        ],
    )

    assert get_unscored_dates() == ["2026-04-14", "2026-04-15"]
    assert query.args_for("is_") == [("scored", None)]


def test_backfill_scored_marks_scorers_and_everyone_else(monkeypatch):
    query = install_query(monkeypatch)

    backfill_scored("2026-04-15", ["1", "2"])

    updates = query.args_for("update")
    assert updates[0][0] == {"scored": 1}
    assert updates[1][0] == {"scored": 0}
    # `in_(...)` twice: once to mark the scorers, once negated for everyone else
    # - the worker's two updateMany calls.
    assert query.args_for("in_") == [("player_id", [1, 2]), ("player_id", [1, 2])]
    assert "not_" in query.accesses


def test_backfill_scored_with_empty_ids_never_builds_an_empty_in_filter(monkeypatch):
    """`in.()` and `not.in.()` with no values are invalid SQL."""
    query = install_query(monkeypatch)

    backfill_scored("2026-04-15", [])

    assert "in_" not in query.call_names()
    assert "not_" not in query.accesses
    # Nobody scored, so the whole date is still graded - just as 0.
    assert [args[0] for args in query.args_for("update")] == [{"scored": 0}]
    assert query.args_for("eq") == [("date", "2026-04-15")]


def test_backfill_scored_with_only_non_numeric_ids_still_grades_the_date(monkeypatch):
    """Ids dropped as non-numeric must not degrade into marking scorers."""
    query = install_query(monkeypatch)

    backfill_scored("2026-04-15", ["unknown"])

    assert "in_" not in query.call_names()
    assert [args[0] for args in query.args_for("update")] == [{"scored": 0}]


def test_delete_game_snapshots_matches_on_team_name(monkeypatch):
    query = install_query(monkeypatch, [FakeResponse(count=45)])

    assert delete_game_snapshots("2026-04-15", ["Toronto", "Montreal"]) == 45
    assert query.args_for("eq") == [("date", "2026-04-15")]
    assert query.args_for("in_") == [("team_name", ["Toronto", "Montreal"])]


def test_delete_game_snapshots_without_team_names_deletes_nothing(monkeypatch):
    """No names would render as an empty in_() filter, so nothing is issued."""
    query = install_query(monkeypatch)

    assert delete_game_snapshots("2026-04-15", []) == 0
    assert delete_game_snapshots("2026-04-15", [None, ""]) == 0
    assert "delete" not in query.call_names()
    assert query.executions == 0


def test_delete_game_snapshots_warns_when_nothing_matched(monkeypatch):
    install_query(monkeypatch, [FakeResponse(count=0)])

    assert delete_game_snapshots("2026-04-15", ["Toronto"]) == 0


def test_get_all_player_snapshots_pages_until_a_short_page(monkeypatch):
    """Paging stops on the first short page rather than looping forever."""
    full_page = [{"date": "2026-04-15", "player_id": index} for index in range(PAGE_SIZE)]
    tail = [{"date": "2026-04-15", "player_id": PAGE_SIZE}]
    query = install_query(monkeypatch, [FakeResponse(data=full_page), FakeResponse(data=tail)])

    snapshots = get_all_player_snapshots()

    assert len(snapshots) == PAGE_SIZE + 1
    assert snapshots[-1] is tail[0]
    # Two executes, not a third: the short page is the termination signal.
    assert query.call_names().count("execute") == 2
    assert query.args_for("range") == [(0, PAGE_SIZE - 1), (PAGE_SIZE, 2 * PAGE_SIZE - 1)]


def test_get_all_player_snapshots_stops_on_an_exactly_full_last_page(monkeypatch):
    """A full page is ambiguous, so it must be followed by one confirming read."""
    full_page = [{"date": "2026-04-15", "player_id": index} for index in range(PAGE_SIZE)]
    query = install_query(monkeypatch, [FakeResponse(data=full_page), FakeResponse(data=[])])

    assert len(get_all_player_snapshots()) == PAGE_SIZE
    assert query.call_names().count("execute") == 2


def test_get_all_player_snapshots_orders_by_the_primary_key(monkeypatch):
    """Without a stable order the paging offsets could skip or repeat rows."""
    query = install_query(monkeypatch, [FakeResponse(data=[])])

    get_all_player_snapshots()

    assert query.args_for("order") == [("date",), ("player_id",)]


def test_table_name_is_environment_scoped(monkeypatch):
    """Player-Snapshots-{ENV} is per-environment, like every other table here."""
    assert SNAPSHOT_TABLE == f"Player-Snapshots-{player_archive.ENV}"


def test_schema_error_is_not_retried(monkeypatch):
    """A rejected payload will not fix itself, so _retry re-raises immediately."""

    class Failing(FakeQuery):
        def execute(self):
            self.executions += 1
            raise APIError({"message": "unknown column"})

    query = Failing()
    client = MagicMock()
    client.table.return_value = query
    monkeypatch.setattr(player_archive, "SUPABASE_ADMIN_CLIENT", client)
    sleeps = []
    monkeypatch.setattr(player_archive.time, "sleep", sleeps.append)

    with pytest.raises(APIError):
        save_player_snapshots([{"id": 1, "name": "Player One"}], date="2026-04-15")

    assert query.executions == 1  # no retry
    assert sleeps == []
