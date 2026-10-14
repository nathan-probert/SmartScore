"""Unit tests for the team-identity backfill script (#113 follow-up).

Everything network- or database-shaped is faked: the parts worth testing are
parsing the NHL payload shapes, the era routing, the era-2 resolution
hierarchy, the gate arithmetic and the column-limited write - none of which
need a server. The real run still needs credentials; see the script docstring.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2] / "smartscore" / "scripts"))

import backfill_team_identity as backfill  # noqa: E402

# --- fixtures ----------------------------------------------------------------

PLACES = {
    "TOR": ("Toronto", "Maple Leafs"),
    "NYR": ("New York", "Rangers"),
    "NYI": ("New York", "Islanders"),
    "PIT": ("Pittsburgh", "Penguins"),
    "BUF": ("Buffalo", "Sabres"),
    "ARI": ("Arizona", "Coyotes"),
}


def schedule_game(game_id, home, away):
    def side(abbr):
        place, common = PLACES[abbr]
        return {"abbrev": abbr, "placeName": {"default": place}, "commonName": {"default": common}}

    return {"id": game_id, "homeTeam": side(home), "awayTeam": side(away)}


def schedule_payload(date, games):
    """A /v1/schedule response with a gameWeek containing only this date."""
    return {"gameWeek": [{"date": date, "games": games}]}


def boxscore_payload(home, away, home_pids, away_pids, goalies=()):
    return {
        "homeTeam": {"abbrev": home},
        "awayTeam": {"abbrev": away},
        "playerByGameStats": {
            "homeTeam": {
                "forwards": [{"playerId": pid} for pid in home_pids],
                "defense": [],
                "goalies": [{"playerId": pid} for pid in goalies],
            },
            "awayTeam": {"forwards": [{"playerId": pid} for pid in away_pids], "defense": [], "goalies": []},
        },
    }


def mongo_doc(date, player_id, name="Player One", **extra):
    return {"date": date, "id": player_id, "name": name, **extra}


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, *_args, **_kwargs):
        return self.documents


class FakeCollection:
    """The slice of pymongo's Collection the script touches."""

    def __init__(self, documents):
        self.documents = documents

    def distinct(self, field, query=None):
        values = []
        for document in self.documents:
            if query and not self._matches(document, query):
                continue
            if field in document and document[field] not in values:
                values.append(document[field])
        return values

    def find(self, query, projection=None):
        return FakeCursor([d for d in self.documents if self._matches(d, query)])

    @staticmethod
    def _matches(document, query):
        for key, condition in query.items():
            value = document.get(key)
            if isinstance(condition, dict):
                if "$type" in condition:
                    actual = "string" if isinstance(value, str) else type(value).__name__
                    if actual != condition["$type"]:
                        return False
                elif "$in" in condition:
                    if value not in condition["$in"]:
                        return False
            elif value != condition:
                return False
        return True


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def no_sleep(monkeypatch):
    monkeypatch.setattr(backfill.time, "sleep", lambda _seconds: None)


# --- parsing -----------------------------------------------------------------


def test_games_for_date_selects_the_matching_day():
    payload = {
        "gameWeek": [
            {"date": "2025-03-14", "games": [schedule_game(1, "TOR", "NYR")]},
            {"date": "2025-03-15", "games": [schedule_game(2, "NYI", "NYR")]},
        ]
    }
    games = backfill.games_for_date(payload, "2025-03-15")
    assert [g["id"] for g in games] == [2]
    assert backfill.games_for_date(payload, "2025-03-16") == []
    assert backfill.games_for_date(None, "2025-03-15") == []


def test_parse_boxscore_maps_both_sides_and_goalies():
    payload = boxscore_payload("TOR", "NYR", [1, 2], [3], goalies=[99])
    mapping = backfill.parse_boxscore(payload)
    assert mapping == {1: "TOR", 2: "TOR", 99: "TOR", 3: "NYR"}


def test_parse_boxscore_ignores_entries_without_an_id():
    payload = boxscore_payload("TOR", "NYR", [1], [])
    payload["playerByGameStats"]["awayTeam"]["forwards"] = [{"playerId": None}, {"name": "no id"}]
    assert backfill.parse_boxscore(payload) == {1: "TOR"}


def test_parse_boxscore_handles_absent_payload():
    assert backfill.parse_boxscore(None) == {}
    assert backfill.parse_boxscore({}) == {}


def test_season_for_uses_september_as_the_boundary():
    assert backfill.season_for("2025-01-25") == "20242025"
    assert backfill.season_for("2025-06-17") == "20242025"
    assert backfill.season_for("2025-11-15") == "20252026"
    assert backfill.season_for("2026-09-26") == "20262027"
    assert backfill.season_for("2026-10-02") == "20262027"


def test_parse_landing_stints_groups_by_season_and_skips_minor_leagues():
    payload = {
        "seasonTotals": [
            {"season": 20242025, "leagueAbbrev": "NHL", "teamName": {"default": "Pittsburgh Penguins"}},
            {"season": 20242025, "leagueAbbrev": "NHL", "teamName": {"default": "Buffalo Sabres"}},
            {"season": 20242025, "leagueAbbrev": "AHL", "teamName": {"default": "Rochester Americans"}},
            {"season": 20252026, "leagueAbbrev": "NHL", "teamName": {"default": "Buffalo Sabres"}},
        ]
    }
    stints = backfill.parse_landing_stints(payload)
    assert stints["20242025"] == {"Pittsburgh Penguins", "Buffalo Sabres"}
    assert stints["20252026"] == {"Buffalo Sabres"}
    assert backfill.parse_landing_stints(None) == {}


def test_parse_roster_player_ids_covers_all_groups():
    payload = {
        "forwards": [{"id": 1}],
        "defensemen": [{"id": 2}, {"id": "3"}],
        "goalies": [{"id": 4}, {"playerId": 5}],
    }
    assert backfill.parse_roster_player_ids(payload) == {1, 2, 3, 4}
    assert backfill.parse_roster_player_ids(None) == set()


# --- schedule index ----------------------------------------------------------


def test_build_schedule_index_builds_per_date_and_global_maps():
    date = "2025-03-15"
    payload = schedule_payload(date, [schedule_game(10, "NYR", "NYI")])
    games, places, teamnames = backfill.build_schedule_index({date: payload})

    assert games[date]["NYR"] == ("New York", "NYI", "New York")
    assert games[date]["NYI"] == ("New York", "NYR", "New York")
    assert places["NYR"] == "New York"
    assert teamnames["New York Rangers"] == "NYR"
    assert teamnames["New York Islanders"] == "NYI"


def test_build_schedule_index_place_map_is_global_across_dates():
    early = schedule_payload("2024-02-16", [schedule_game(1, "ARI", "TOR")])
    late = schedule_payload("2024-10-09", [schedule_game(2, "TOR", "NYR")])
    _, places, _ = backfill.build_schedule_index({"2024-02-16": early, "2024-10-09": late})
    assert places["ARI"] == "Arizona"  # idle-team rows can still find a place


# --- era 2 resolution --------------------------------------------------------


@pytest.fixture
def resolver_maps():
    _, places, teamnames = backfill.build_schedule_index(
        {"2025-03-15": schedule_payload("2025-03-15", [schedule_game(10, "PIT", "BUF")])}
    )
    return places, teamnames


def test_resolve_prefers_the_same_day_boxscore(resolver_maps):
    _, teamnames = resolver_maps
    timeline = {7: {"2025-03-15": "BUF"}}
    abbr, method = backfill.resolve_player(7, "2025-03-15", timeline, {}, {}, teamnames)
    assert (abbr, method) == ("BUF", "boxscore")


def test_resolve_brackets_a_scratch_between_two_appearances(resolver_maps):
    _, teamnames = resolver_maps
    timeline = {7: {"2025-03-13": "PIT", "2025-03-17": "PIT"}}
    abbr, method = backfill.resolve_player(7, "2025-03-15", timeline, {}, {}, teamnames)
    assert (abbr, method) == ("PIT", "bracket")


def test_resolve_refuses_to_guess_a_trade_window(resolver_maps):
    _, teamnames = resolver_maps
    timeline = {7: {"2025-03-13": "PIT", "2025-03-17": "BUF"}}
    abbr, method = backfill.resolve_player(7, "2025-03-15", timeline, {}, {}, teamnames)
    assert abbr is None
    assert method == "trade-window"


def test_resolve_lands_on_a_single_season_stint(resolver_maps):
    _, teamnames = resolver_maps
    landings = {7: {"20242025": {"Pittsburgh Penguins"}}}
    abbr, method = backfill.resolve_player(7, "2025-03-15", {}, landings, {}, teamnames)
    assert (abbr, method) == ("PIT", "landing")


def test_resolve_never_lets_a_landing_stint_overrule_a_game(resolver_maps):
    """A sighting on another date is game-day truth too."""
    _, teamnames = resolver_maps
    timeline = {7: {"2025-03-13": "BUF"}}
    landings = {7: {"20242025": {"Pittsburgh Penguins"}}}
    abbr, method = backfill.resolve_player(7, "2025-03-15", timeline, landings, {}, teamnames)
    assert abbr is None
    assert method == "one-sided-bracket"


def test_resolve_uses_a_unique_season_roster_as_last_resort(resolver_maps):
    _, teamnames = resolver_maps
    rosters = {"20242025": {7: {"PIT"}}}
    abbr, method = backfill.resolve_player(7, "2025-03-15", {}, {}, rosters, teamnames)
    assert (abbr, method) == ("PIT", "roster")


def test_resolve_treats_a_two_team_roster_as_ambiguous(resolver_maps):
    _, teamnames = resolver_maps
    rosters = {"20242025": {7: {"PIT", "BUF"}}}
    abbr, method = backfill.resolve_player(7, "2025-03-15", {}, {}, rosters, teamnames)
    assert abbr is None
    assert method in ("no-timeline-no-landing", "landing-multi-or-mismatch")


def test_resolve_one_sided_bracket_stays_unresolved_without_evidence(resolver_maps):
    _, teamnames = resolver_maps
    timeline = {7: {"2025-03-13": "PIT"}}
    abbr, method = backfill.resolve_player(7, "2025-03-15", timeline, {}, {}, teamnames)
    assert abbr is None
    assert method == "one-sided-bracket"


# --- build: era routing ------------------------------------------------------


def make_build_inputs(date="2025-03-15"):
    schedules = {date: schedule_payload(date, [schedule_game(10, "PIT", "BUF")])}
    return schedules


def test_build_era1_takes_the_abbr_from_mongo_and_names_from_schedule():
    date = "2024-02-16"
    collection = FakeCollection([mongo_doc(date, 1, team_abbr="ARI")])
    schedules = {date: schedule_payload(date, [schedule_game(1, "ARI", "TOR")])}
    stats = backfill.BackfillStats()

    rows = backfill.build_payloads(collection, {}, schedules, {}, {}, {}, None, stats)

    row = rows[(date, 1)]
    assert row == {
        "date": date,
        "player_id": 1,
        "team_name": "Arizona",
        "team_abbr": "ARI",
        "opponent_abbr": "TOR",
        "opponent_name": "Toronto",
    }
    assert stats.per_era[backfill.ERA_MONGO_ABBR] == 1
    assert stats.resolved_by["mongo"] == 1


def test_build_era2_unresolved_rows_are_excluded_and_listed():
    date = "2025-03-15"
    collection = FakeCollection([mongo_doc(date, 1), mongo_doc(date, 2)])
    schedules = make_build_inputs(date)
    landings = {2: {"20242025": {"Pittsburgh Penguins"}}}  # only player 2 resolves
    stats = backfill.BackfillStats()

    rows = backfill.build_payloads(collection, {}, schedules, {}, landings, {}, None, stats)

    assert set(rows) == {(date, 2)}
    assert stats.unresolved_by_era[backfill.ERA_GAP] == 1
    assert stats.unresolved_detail[0]["reason"] in (
        "no-timeline-no-landing",
        "one-sided-bracket",
        "landing-multi-or-mismatch",
    )
    assert rows[(date, 2)]["team_name"] == "Pittsburgh"
    assert rows[(date, 2)]["opponent_abbr"] == "BUF"


def test_build_era3_keeps_the_stored_team_name_even_if_place_disagrees():
    date = "2026-10-02"
    collection = FakeCollection([mongo_doc(date, 1, team_name="Detroit")])
    schedules = {date: schedule_payload(date, [schedule_game(1, "TOR", "NYR")])}
    box = {date: {1: "TOR"}}  # boxscore says Toronto; stored says Detroit
    stats = backfill.BackfillStats()

    rows = backfill.build_payloads(collection, {(date, 1): "Detroit"}, schedules, box, {}, {}, None, stats)

    row = rows[(date, 1)]
    assert row["team_name"] == "Detroit"  # preserved, not overwritten
    assert row["team_abbr"] == "TOR"
    assert stats.era3_name_mismatch == 1
    assert stats.per_era[backfill.ERA_STORED_NAME] == 1


def test_build_dedupes_identical_copies_on_the_primary_key():
    date = "2024-02-16"
    collection = FakeCollection([mongo_doc(date, 1, team_abbr="ARI"), mongo_doc(date, 1, team_abbr="ARI", name="Copy")])
    schedules = {date: schedule_payload(date, [schedule_game(1, "ARI", "TOR")])}
    stats = backfill.BackfillStats()

    rows = backfill.build_payloads(collection, {}, schedules, {}, {}, {}, None, stats)

    assert len(rows) == 1
    assert stats.rows_seen == 2
    assert stats.rows_payload == 1


def test_build_payload_columns_are_exactly_the_identity_set():
    date = "2024-02-16"
    collection = FakeCollection([mongo_doc(date, 1, team_abbr="ARI")])
    schedules = {date: schedule_payload(date, [schedule_game(1, "ARI", "TOR")])}
    stats = backfill.BackfillStats()

    rows = backfill.build_payloads(collection, {}, schedules, {}, {}, {}, None, stats)

    assert set(rows[(date, 1)]) == set(backfill.APPLY_COLUMNS)


def test_build_counts_rows_whose_team_did_not_play_that_date():
    """Idle-team rows still get a team_name (global place map), no opponent."""
    date = "2025-03-15"
    collection = FakeCollection([mongo_doc(date, 1, team_abbr="ARI")])  # ARI not on the slate
    schedules = {
        # ARI played earlier in the window, so the global place map knows it;
        # it just has no game on this date.
        "2025-01-10": schedule_payload("2025-01-10", [schedule_game(9, "ARI", "TOR")]),
        **make_build_inputs(date),
    }
    stats = backfill.BackfillStats()

    rows = backfill.build_payloads(collection, {}, schedules, {}, {}, {}, None, stats)

    row = rows[(date, 1)]
    assert row["team_name"] == "Arizona"
    assert row["opponent_abbr"] is None
    assert stats.opponent_missing[backfill.ERA_MONGO_ABBR] == 1


# --- gates -------------------------------------------------------------------


def stats_with(**overrides):
    stats = backfill.BackfillStats()
    stats.per_era.update({backfill.ERA_MONGO_ABBR: 100, backfill.ERA_GAP: 1000, backfill.ERA_STORED_NAME: 50})
    for key, value in overrides.items():
        setattr(stats, key, value)
    return stats


def test_gates_pass_on_a_clean_build():
    assert backfill.evaluate_gates(stats_with()) == []


def test_gates_fail_era1_below_perfect():
    stats = stats_with()
    stats.unresolved_by_era[backfill.ERA_MONGO_ABBR] = 1
    failures = backfill.evaluate_gates(stats)
    assert len(failures) == 1
    assert "era 1" in failures[0] and "100%" in failures[0]


def test_gates_fail_era2_below_the_coverage_floor():
    stats = stats_with()
    stats.unresolved_by_era[backfill.ERA_GAP] = 5  # 99.5% < 99.9%
    failures = backfill.evaluate_gates(stats)
    assert len(failures) == 1
    assert "era 2" in failures[0]


def test_gates_allow_the_era2_floor_of_unresolved_rows():
    stats = stats_with()
    stats.unresolved_by_era[backfill.ERA_GAP] = 1  # 99.9% exactly
    assert backfill.evaluate_gates(stats) == []


def test_gates_fail_era3_unresolved_or_name_mismatch():
    stats = stats_with()
    stats.unresolved_by_era[backfill.ERA_STORED_NAME] = 1
    stats.era3_name_mismatch = 2
    failures = backfill.evaluate_gates(stats)
    assert len(failures) == 2
    assert "era 3: 1/50" in failures[0]
    assert "disagrees" in failures[1]


# --- cache -------------------------------------------------------------------


def test_fetch_json_short_circuits_on_a_cache_hit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        backfill.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("cache hit must not hit the network"),
    )
    cache = tmp_path / "schedule" / "2025-03-15.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"gameWeek": []}), encoding="utf-8")

    assert backfill.fetch_json("https://example.invalid", cache) == {"gameWeek": []}


def test_fetch_json_writes_the_cache_on_a_miss(monkeypatch, tmp_path):
    no_sleep(monkeypatch)
    monkeypatch.setattr(backfill.requests, "get", lambda *_args, **_kwargs: FakeResponse(200, {"ok": True}))
    cache = tmp_path / "x.json"

    assert backfill.fetch_json("https://example.invalid", cache) == {"ok": True}
    assert json.loads(cache.read_text(encoding="utf-8")) == {"ok": True}


def test_fetch_json_treats_404_as_none_without_retry(monkeypatch, tmp_path):
    no_sleep(monkeypatch)
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        return FakeResponse(404)

    monkeypatch.setattr(backfill.requests, "get", fake_get)
    assert backfill.fetch_json("https://example.invalid", tmp_path / "none.json") is None
    assert len(calls) == 1


# --- write path --------------------------------------------------------------


def test_post_batch_sends_the_column_limited_upsert(monkeypatch):
    captured = {}

    def fake_post(url, params=None, headers=None, json=None, timeout=None):
        captured.update(url=url, params=params, headers=headers, rows=json, timeout=timeout)
        return FakeResponse(201)

    monkeypatch.setattr(backfill.requests, "post", fake_post)
    rows = [
        {
            "date": "2025-03-15",
            "player_id": 1,
            "team_name": "Pittsburgh",
            "team_abbr": "PIT",
            "opponent_abbr": "BUF",
            "opponent_name": "Buffalo",
        }
    ]

    backfill.post_batch(rows)

    assert captured["params"]["on_conflict"] == "date,player_id"
    assert captured["params"]["columns"] == ",".join(backfill.APPLY_COLUMNS)
    assert captured["headers"]["Prefer"] == "resolution=merge-duplicates,return=minimal"
    assert captured["rows"] == rows
    assert "Player-Snapshots" in captured["url"]


def test_post_batch_raises_immediately_on_a_schema_rejection(monkeypatch):
    no_sleep(monkeypatch)
    calls = []

    def fake_post(*_args, **_kwargs):
        calls.append(1)
        return FakeResponse(400, text="column name does not exist")

    monkeypatch.setattr(backfill.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="400"):
        backfill.post_batch([{}])
    assert len(calls) == 1  # a 4xx is not retried


def test_post_batch_retries_server_errors(monkeypatch):
    no_sleep(monkeypatch)
    calls = []

    def fake_post(*_args, **_kwargs):
        calls.append(1)
        return FakeResponse(503) if len(calls) < 3 else FakeResponse(201)

    monkeypatch.setattr(backfill.requests, "post", fake_post)
    backfill.post_batch([{}])
    assert len(calls) == 3


class FakeReadBackTable:
    """Enough of a Supabase table for the canary read-back."""

    def __init__(self, row):
        self.row = row

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self):
        class Response:
            data = [self.row]

        return Response()


def test_apply_payload_runs_a_canary_then_the_bulk(monkeypatch):
    canary_row = {
        "date": "2024-02-16",
        "player_id": 1,
        "team_name": "Arizona",
        "team_abbr": "ARI",
        "opponent_abbr": "TOR",
        "opponent_name": "Toronto",
        "name": "Some Player",
        "scored": 1,
    }

    class CanaryTable:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def execute(self):
            class Response:
                data = [canary_row]

            return Response()

    canary_client = type("C", (), {"table": lambda _self, _name: CanaryTable()})()
    monkeypatch.setattr(backfill, "SUPABASE_ADMIN_CLIENT", canary_client)
    batches = []
    monkeypatch.setattr(backfill, "post_batch", lambda rows: batches.append(list(rows)))

    rows = {
        ("2024-02-16", 1): {k: v for k, v in canary_row.items() if k in backfill.APPLY_COLUMNS},
        ("2024-02-16", 2): {**{k: v for k, v in canary_row.items() if k in backfill.APPLY_COLUMNS}, "player_id": 2},
    }
    written = backfill.apply_payload(rows, batch_size=1)

    assert written == 2
    assert batches[0][0]["player_id"] == 1  # canary first
    assert [len(batch) for batch in batches] == [1, 1]


def test_apply_payload_aborts_when_the_canary_read_back_is_empty(monkeypatch):
    monkeypatch.setattr(backfill, "post_batch", lambda rows: None)

    class EmptyTable:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def execute(self):
            class Response:
                data = []

            return Response()

    empty_client = type("C", (), {"table": lambda _self, _name: EmptyTable()})()
    monkeypatch.setattr(backfill, "SUPABASE_ADMIN_CLIENT", empty_client)
    rows = {("2024-02-16", 1): {"date": "2024-02-16", "player_id": 1}}
    with pytest.raises(RuntimeError, match="Canary row not found"):
        backfill.apply_payload(rows, batch_size=10)


def test_apply_payload_with_nothing_to_write_is_a_no_op(monkeypatch):
    monkeypatch.setattr(backfill, "post_batch", lambda rows: pytest.fail("empty apply must not post"))
    assert backfill.apply_payload({}, batch_size=10) == 0


# --- report / CLI ------------------------------------------------------------


def test_report_reads_as_the_operator_signal(capsys):
    stats = backfill.BackfillStats()
    stats.rows_seen = 3
    stats.per_era[backfill.ERA_MONGO_ABBR] = 3

    backfill.report(stats, ["era 1: 1/3 did not resolve"], [{"date": "2024-02-16"}], applied=False)

    out = capsys.readouterr().out
    assert "Would apply" in out
    assert "GATES FAILED" in out
    assert "era 1: 1/3" in out


def test_main_requires_a_mongo_uri(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    with pytest.raises(SystemExit):
        backfill.main([])


def test_main_scrape_delegates_and_exits_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.invalid")
    calls = []

    class ClosingClient:
        def __init__(self, _uri):
            pass

        def __getitem__(self, _name):
            return self

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(backfill, "MongoClient", ClosingClient)
    monkeypatch.setattr(backfill, "scrape", lambda collection, cache_dir: calls.append(f"scrape:{cache_dir}"))

    assert backfill.main(["--scrape", "--cache-dir", str(tmp_path)]) == 0
    assert calls[0].startswith("scrape:")
    assert calls[-1] == "closed"
