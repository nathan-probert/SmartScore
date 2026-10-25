"""fidelity_tracker: metric math on small synthetic frames, and the skip paths.

These unit tests run against synthetic rows only - no Supabase call and no
real local store. The skip-path tests drive track_date with an in-memory
fake supabase client and monkeypatched store readers.
"""

import csv

import fidelity_tracker as ft
import pytest
from fidelity_tracker import (
    aggregate_past_log,
    detect_slot_column,
    fidelity_metrics,
    normalize_pp,
    normalize_unit,
    pair_with_actuals,
    season_for_date,
    slot_for_row,
    track_date,
)


def _pair(**overrides):
    base = {
        "date": "2026-10-22",
        "player_id": 1,
        "projected_unit": "F1",
        "projected_pp": "PP1",
        "projected_group": "F",
        "actual_unit": "F1",
        "actual_pp": "PP1",
        "actual_group": "F",
        "actual_game_id": 1,
    }
    base.update(overrides)
    return base


# ------- normalizers -------


def test_normalize_unit_trims_and_uppercases():
    assert normalize_unit(" f1 ") == "F1"
    assert normalize_unit(None) is None
    assert normalize_unit("") is None
    assert normalize_unit(float("nan")) is None


def test_normalize_pp_translates_verbatim_pipeline_labels():
    assert normalize_pp("POWER PLAY #1") == "PP1"
    assert normalize_pp("POWER PLAY #2") == "PP2"
    assert normalize_pp("PP1") == "PP1"
    assert normalize_pp(" PP2 ") == "PP2"
    # PP3/PP4 are proxy noise, kept as themselves - never folded into PP1/PP2.
    assert normalize_pp("POWER PLAY #3") == "PP3"
    assert normalize_pp(None) is None


# ------- season -------


def test_season_boundary_is_july_not_january():
    assert season_for_date("2026-10-22") == "20262027"
    assert season_for_date("2026-01-02") == "20252026"
    assert season_for_date("2024-07-01") == "20242025"
    assert isinstance(season_for_date("2026-10-22"), str)


# ------- slot detection -------


def test_no_slot_column_means_single_slot():
    rows = [{"date": "d", "player_id": 1}]
    assert detect_slot_column(rows) is None
    assert slot_for_row(rows[0], None) == "single"


def test_label_column_wins_over_timestamp():
    rows = [
        {"source_slot": "1030", "snapshot_time": "2026-10-22T10:30:00-04:00", "player_id": 1},
        {"source_slot": "1600", "snapshot_time": "2026-10-22T16:00:00-04:00", "player_id": 2},
    ]
    assert detect_slot_column(rows) == "source_slot"
    assert {slot_for_row(row, "source_slot") for row in rows} == {"1030", "1600"}


def test_timestamp_column_buckets_in_et():
    row = {"updated_at": "2026-10-22T14:00:00-04:00"}
    assert slot_for_row(row, "updated_at") == "14:00"


# ------- pairing -------


def test_pair_joins_on_date_and_player_id():
    projected = [
        {
            "player_id": 5,
            "team_name": "Minnesota",
            "home": False,
            "lineup_unit": "F2",
            "pp_unit": "PP1",
            "lineup_position_group": "F",
            "lineup_status": "PROJECTED",
        }
    ]
    actual = [
        {
            "game_id": 99,
            "player_id": 5,
            "team_abbrev": "MIN",
            "home": 0,
            "game_date": "2026-10-22",
            "lineup_unit": "F2",
            "pp_unit": "PP1",
            "lineup_position_group": "F",
            "lineup_status": "ACTUAL",
        }
    ]
    pairs, breadcrumbs = pair_with_actuals(projected, actual, "2026-10-22")
    assert len(pairs) == 1
    assert breadcrumbs == {}
    assert pairs[0]["projected_unit"] == "F2"
    assert pairs[0]["actual_unit"] == "F2"


def test_ambiguous_place_is_excluded_never_guessed():
    # (New York, home) matching two clubs must be excluded, never guessed.
    projected = [
        {
            "player_id": 5,
            "team_name": "New York",
            "home": True,
            "lineup_unit": "F1",
            "pp_unit": None,
            "lineup_position_group": "F",
            "lineup_status": "PROJECTED",
        }
    ]
    actual = [
        {
            "game_id": 1,
            "player_id": 5,
            "team_abbrev": "NYI",
            "home": 1,
            "game_date": "2026-10-22",
            "lineup_unit": "F1",
            "pp_unit": None,
            "lineup_position_group": "F",
            "lineup_status": "ACTUAL",
        },
        {
            "game_id": 2,
            "player_id": 6,
            "team_abbrev": "NYR",
            "home": 1,
            "game_date": "2026-10-22",
            "lineup_unit": "D1",
            "pp_unit": None,
            "lineup_position_group": "D",
            "lineup_status": "ACTUAL",
        },
    ]
    pairs, breadcrumbs = pair_with_actuals(projected, actual, "2026-10-22")
    assert pairs == []
    assert breadcrumbs["n_team_ambiguous"] == 1


def test_new_york_resolves_through_the_home_split():
    # NYI hosts, NYR plays away: (New York, home=True) resolves to NYI only,
    # so the same ambiguous place now joins cleanly through the scheduled split.
    projected = [
        {
            "player_id": 5,
            "team_name": "New York",
            "home": True,
            "lineup_unit": "F1",
            "pp_unit": None,
            "lineup_position_group": "F",
            "lineup_status": "PROJECTED",
        }
    ]
    actual = [
        {
            "game_id": 1,
            "player_id": 5,
            "team_abbrev": "NYI",
            "home": 1,
            "game_date": "2026-10-22",
            "lineup_unit": "F1",
            "pp_unit": None,
            "lineup_position_group": "F",
            "lineup_status": "ACTUAL",
        },
        {
            "game_id": 2,
            "player_id": 6,
            "team_abbrev": "NYR",
            "home": 0,
            "game_date": "2026-10-22",
            "lineup_unit": "D1",
            "pp_unit": None,
            "lineup_position_group": "D",
            "lineup_status": "ACTUAL",
        },
    ]
    pairs, breadcrumbs = pair_with_actuals(projected, actual, "2026-10-22")
    assert len(pairs) == 1
    assert pairs[0]["actual_unit"] == "F1"
    assert breadcrumbs == {}


def test_projected_player_absent_from_actuals_is_counted():
    projected = [
        {
            "player_id": 5,
            "team_name": "Boston",
            "home": True,
            "lineup_unit": "F1",
            "pp_unit": None,
            "lineup_position_group": "F",
            "lineup_status": "PROJECTED",
        }
    ]
    pairs, breadcrumbs = pair_with_actuals(projected, [], "2026-10-22")
    assert pairs == []
    assert breadcrumbs["n_unknown_players"] == 1


def test_non_actual_status_carries_group_but_no_unit():
    # An actual row without the ACTUAL stamp (missing-unit pass) may join but
    # contributes no unit/pp value - an absence, not a match.
    projected = [
        {
            "player_id": 5,
            "team_name": "Boston",
            "home": True,
            "lineup_unit": "F1",
            "pp_unit": "PP1",
            "lineup_position_group": "F",
            "lineup_status": "PROJECTED",
        }
    ]
    actual = [
        {
            "game_id": 1,
            "player_id": 5,
            "team_abbrev": "BOS",
            "home": 1,
            "game_date": "2026-10-22",
            "lineup_unit": None,
            "pp_unit": None,
            "lineup_position_group": "F",
            "lineup_status": "REJECTED",
        }
    ]
    pairs, _ = pair_with_actuals(projected, actual, "2026-10-22")
    assert pairs[0]["actual_unit"] is None
    assert pairs[0]["actual_pp"] is None
    assert pairs[0]["actual_group"] == "F"


# ------- metric math -------


def test_metrics_perfect_when_every_pair_matches():
    pairs = [_pair(player_id=i) for i in range(10)]
    metrics = fidelity_metrics(pairs)
    assert metrics["unit_match"] == {"value": 1.0, "matches": 10, "n": 10}
    assert metrics["pp_match"] == {"value": 1.0, "matches": 10, "n": 10}
    assert metrics["presence_match"] == {"value": 1.0, "matches": 10, "n": 10}


def test_metrics_partial_and_denominators_independent():
    pairs = [
        _pair(player_id=1, projected_unit="F1", actual_unit="F1", projected_pp="PP1", actual_pp="PP1"),
        _pair(player_id=2, projected_unit="F2", actual_unit="D1", projected_pp=None),  # unit wrong, no projected PP
        _pair(player_id=3, projected_unit="F3", actual_unit=None, actual_pp=None),  # unit missing
        _pair(player_id=4, projected_unit="F1", actual_unit="F1", projected_pp="PP2", actual_pp="PP1"),
    ]
    metrics = fidelity_metrics(pairs)
    # unit: 2 of 3 comparable; PP: 1 of 2 comparable; presence: all 4 are F-F pairs.
    assert metrics["unit_match"]["value"] == pytest.approx(2 / 3)
    assert metrics["unit_match"]["n"] == 3
    assert metrics["pp_match"] == {"value": 0.5, "matches": 1, "n": 2}
    assert metrics["presence_match"] == {"value": 1.0, "matches": 4, "n": 4}


def test_metrics_empty_input_yields_null_rates():
    metrics = fidelity_metrics([])
    assert all(entry == {"value": None, "matches": 0, "n": 0} for entry in metrics.values())


# ------- logging -------


def test_aggregate_reweights_by_exposure(tmp_path):
    log = tmp_path / "fidelity_log.csv"
    with open(log, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(ft.LOG_COLUMNS)
        # A weak slot and a strong slot: the pooled rate must be exposure-weighted.
        writer.writerow(["t", "prod", "20262027", "date", "2026-10-20", "single", "unit_match", "0.000000", 0, 2])
        writer.writerow(["t", "prod", "20262027", "date", "2026-10-21", "single", "unit_match", "0.900000", 9, 10])

    aggregate_past_log("prod", "20262027", log_path=log)
    with open(log, newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["scope"] == "season"]
    unit = next(row for row in rows if row["metric"] == "unit_match")
    assert int(unit["matches"]) == 9
    assert int(unit["n"]) == 12
    assert float(unit["value"]) == pytest.approx(9 / 12)


# ------- fake supabase and store doubles -------


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def execute(self):
        result = type("R", (), {})()
        result.data = self._rows
        return result


class _FakeClient:
    """Minimal supabase table client: .table(T).select(...).eq(...).execute()."""

    def __init__(self, rows):
        self._rows = rows

    def table(self, _table):
        return _Query(self._rows)


@pytest.fixture
def log_and_db(tmp_path):
    return tmp_path / "fidelity_log.csv", tmp_path / "raw_nhl.sqlite"


# ------- skip paths -------


def test_skip_when_no_actual_rows_logged_or_compared(log_and_db):
    # The backtracking actuals are the concurrent crawler's backfill: until a
    # date's shift charts land, the tracker must skip with a clear message,
    # log nothing, and exit 0.
    log_path, _ = log_and_db
    projected = [
        {
            "date": "2026-10-22",
            "player_id": 5,
            "team_name": "Boston",
            "home": True,
            "lineup_unit": "F1",
            "pp_unit": "PP1",
            "lineup_position_group": "F",
            "lineup_status": "PROJECTED",
        }
    ]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ft, "actuals_for_date", lambda date_str, db_path=None: [])  # store exists, no rows yet
        status, message = track_date(
            "prod", "2026-10-22", supabase=_FakeClient(projected), db_path=log_and_db[1], log_path=log_path
        )
    assert status == "skip"
    assert "no ACTUAL lineups" in message
    assert "crawled" in message
    assert not log_path.exists(), "a skip must not log"


def test_skip_when_store_file_missing(log_and_db):
    # A missing store file is a distinct, clearer skip than a store with no rows.
    log_path, db = log_and_db
    projected = [
        {
            "date": "2026-10-22",
            "player_id": 5,
            "team_name": "Boston",
            "home": True,
            "lineup_unit": "F1",
            "pp_unit": "PP1",
            "lineup_position_group": "F",
            "lineup_status": "PROJECTED",
        }
    ]
    status, message = track_date("prod", "2026-10-22", supabase=_FakeClient(projected), db_path=db, log_path=log_path)
    assert status == "skip"
    assert "local store not found" in message


def test_skip_when_no_projected_rows(log_and_db):
    log_path, _ = log_and_db
    status, message = track_date(
        "dev", "2026-10-22", supabase=_FakeClient([]), db_path=log_path.with_suffix(".sqlite"), log_path=log_path
    )
    assert status == "skip"
    assert "nothing to compare" in message
    assert not log_path.exists(), "a skip must not log"


# ------- happy path, fully faked -------


def test_track_date_happy_path_writes_log(tmp_path):
    projected = [
        {
            "date": "2026-10-22",
            "player_id": i,
            "team_name": "Boston",
            "home": True,
            "lineup_unit": "F1",
            "pp_unit": "PP2",
            "lineup_position_group": "F",
            "lineup_status": "PROJECTED",
        }
        for i in (1, 2, 3, 4)
    ]
    actual = [
        {
            "game_id": 9,
            "player_id": i,
            "team_abbrev": "BOS",
            "home": 1,
            "game_date": "2026-10-22",
            "lineup_unit": "F1" if i % 2 else "F3",
            "pp_unit": "PP2",
            "lineup_position_group": "F",
            "lineup_status": "ACTUAL",
        }
        for i in (1, 2, 3, 4)
    ]
    log_path, db_path = tmp_path / "fidelity_log.csv", tmp_path / "store.sqlite"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ft, "actuals_for_date", lambda date_str, db_path=None: actual)
        status, message = track_date(
            "prod", "2026-10-22", supabase=_FakeClient(projected), db_path=db_path, log_path=log_path
        )

    assert status == "tracked", message
    # Unit: F1 for players 1,3 and F3 for 2,4 -> 2/4. PP: PP1-PP2 everywhere -> 4/4.
    assert "2/4 = 50.0%" in message
    assert "4/4 = 100.0%" in message

    with open(log_path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows, "a tracked run must append log rows"
    date_rows = [row for row in rows if row["scope"] == "date" and row["date"] == "2026-10-22"]
    unit_row = next(row for row in date_rows if row["metric"] == "unit_match" and row["source_slot"] == "single")
    assert int(unit_row["matches"]) == 2 and int(unit_row["n"]) == 4
    assert any(row["scope"] == "season" for row in rows), "season-scope aggregate rows are appended per run"
    # nothing below came from anywhere real: the fake client saw only synthetic rows
