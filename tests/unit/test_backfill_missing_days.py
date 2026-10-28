"""Unit tests for the stage-2 gap-fill helper (backfill_missing_days.py) and
the exact rolling/career recompute's parity against the local-store path.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "smartscore"))
sys.path.insert(1, str(ROOT / "smartscore" / "scripts" / "backtrack"))

_spec = importlib.util.spec_from_file_location(
    "backfill", ROOT / "smartscore" / "scripts" / "backtrack" / "backfill_missing_days.py"
)
bf = importlib.util.module_from_spec(_spec)
sys.modules["backfill"] = bf
_spec.loader.exec_module(bf)

model = sys.modules["model"] if "model" in sys.modules else __import__("model")

# ------------------------------------------------------------------ fixture

GAME_ID = 2026010001
DAY = "2026-10-25"
YP = "2026-2025-or-whatever".split("-", maxsplit=1)[0]  # unused guard


def fake_boxscore_records():
    """fetch_boxscore's normalised shape (subset of fields the wrapper reads)."""
    return [
        {
            "game_id": GAME_ID,
            "game_date": DAY,
            "player_id": 1,
            "name": "A. Forward",
            "team_abbrev": "TOR",
            "position": "C",
            "goals": 1,
            "shots": 3,
            "toi": "18:31",
        },
        {
            "game_id": GAME_ID,
            "game_date": DAY,
            "player_id": 2,
            "name": "B. Defence",
            "team_abbrev": "TOR",
            "position": "D",
            "goals": 0,
            "shots": 0,
            "toi": None,
        },
        {
            "game_id": GAME_ID,
            "game_date": DAY,
            "player_id": 99,
            "name": "G. Goalie",
            "team_abbrev": "TOR",
            "position": "G",
            "goals": 0,
            "shots": 0,
            "toi": "60:00",
        },
    ]


def fake_schedule(final_states=("FINAL",)):
    """A fetch_schedule payload for one week with DAY's games."""
    games = []
    for index, state in enumerate(("FINAL", "OFF", "LIVE", "FUT", "PPD")):
        games.append({"id": GAME_ID + index, "gameType": 2, "gameState": state, "gameDate": DAY})
    week = {"date": DAY, "games": games}
    payload = {"gameWeek": [week]}
    return payload


class FakeBacktrackTable:
    """Upsert capture: merge-duplicates update of the provided keys only."""

    def __init__(self, frame):
        self._frame = frame
        self.upserts = []

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return MagicMock(data=self._frame.to_dict("records"))

    def upsert(self, rows, on_conflict=None, returning=None):
        assert on_conflict == "date,player_id"
        assert _is_json_safe(rows)
        self.upserts.append(list(rows))
        return self


def _is_json_safe(rows):
    import math  # noqa: PLC0415

    for row in rows:
        for value in row.values():
            if value is None:
                continue
            if isinstance(value, float) and math.isnan(value):
                return False
            if not isinstance(value, (str, int, float, bool)):
                return False
    return True


@pytest.fixture
def wired(monkeypatch):
    """Latest published date = the day before DAY; capture upserts."""
    day_before = (pd.Timestamp(DAY) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    published = pd.DataFrame({"date": [day_before, day_before, "2026-10-20"], "player_id": [1, 2, 1]})
    table = FakeBacktrackTable(published)
    fake_client = MagicMock()

    def _table(name):
        assert name == "Player-Snapshots-backtrack-prod"
        return table

    fake_client.table.side_effect = _table
    monkeypatch.setattr(bf, "SUPABASE_ADMIN_CLIENT", fake_client)
    monkeypatch.setattr(bf, "fetch_schedule", lambda date, delay_seconds=None: fake_schedule())
    monkeypatch.setattr(bf, "fetch_boxscore", lambda game_id, delay_seconds=None, **_k: fake_boxscore_records())
    monkeypatch.setattr(bf, "MAX_MISSING_DAYS", 14, raising=False if False else None)
    return table


# ----------------------------------------------------------- write semantics


def test_backfill_writes_only_missing_dates_rows(wired):
    """One missing day (DAY); the writes carry only that day's RAW-column
    rows - no rolling columns, no other dates."""
    games, rows = bf.backfill("prod", through_date=DAY, dry=False)
    assert games == 2  # FINAL + OFF are terminal; LIVE/FUT/PPD skipped
    upsert_dates = {row["date"] for batch in wired.upserts for row in batch}
    assert upsert_dates <= {DAY}
    keys = [(row["date"], row["player_id"]) for batch in wired.upserts for row in batch]
    assert (DAY, 1) in keys and (DAY, 2) in keys
    assert not any(key == (DAY, 99) for key in keys)  # goalies excluded
    payload_keys = {k for row in wired.upserts[0] for k in row}
    assert payload_keys <= {"date", "player_id", "name", "game_goals", "game_shots", "game_toi_min"}
    assert "game_toi_min" in payload_keys  # the rolling columns are NOT here


def test_backfill_continues_past_live_games(wired, monkeypatch):
    """The FINAL-only guard: a LIVE box score is NEVER fetched (the cache-
    hostage hazard), so 3 of the 5 slate games are skipped with a note;
    boxscore requests exactly 2 (one per terminal game)."""
    fetches = []

    def _track(game_id, delay_seconds=None, **_k):
        fetches.append(game_id)
        return fake_boxscore_records()

    monkeypatch.setattr(bf, "fetch_boxscore", _track)
    _, _rows = bf.backfill("prod", through_date=DAY, dry=False)
    assert sorted(fetches) == [GAME_ID, GAME_ID + 1]
    assert _rows_total(wired.upserts) == 2 * len(fake_boxscore_records()) - 2  # 2 skaters x 2 games (goalies cut)


def _rows_total(upserts):
    return sum(len(batch) for batch in upserts)


def test_backfill_dry_prevents_all_writes(wired, capsys):
    """dry=True: the reads happen (schedule + boxscore), the upserts DO NOT -
    zero Supabase writes."""  # noqa: D403
    games, rows = bf.backfill("prod", through_date=DAY, dry=True)
    out = capsys.readouterr().out
    assert wired.upserts == []
    assert rows > 0  # the dry pass still counts what it WOULD write
    assert "dry-read" in out and "(dry)" in out


def test_backfill_tops_up_existing_rows_without_overwrite(wired):
    """A row the publish already wrote for DAY (idempotent replay) receives
    ONLY the raw columns: the payload carries no other keys, so any existing
    column (the FakeBacktrackTable contract) would survive untouched."""
    bf.backfill("prod", through_date=DAY, dry=False)
    for row in wired.upserts[0]:
        assert set(row) <= {"date", "player_id", "name", "game_goals", "game_shots", "game_toi_min"}


def test_days_between_bounds_the_sweep():
    """A >14-day gap is capped: the helper never sweeps a season."""
    days = bf.days_between("2026-09-01", "2026-10-01")
    assert len(days) == 14
    assert days[0] == "2026-09-02" and days[-1] == "2026-09-15"


def test_raw_column_payload_toi_parsing():
    rows = bf._record_rows(GAME_ID, fake_boxscore_records())
    forward = next(row for row in rows if row["player_id"] == 1)
    assert forward["game_toi_min"] == pytest.approx(18 + 31 / 60)
    assert forward["game_goals"] == 1 and forward["game_shots"] == 3
    # missing TOI stays an honest None (the core drops + warns downstream)
    missing = next(row for row in rows if row["player_id"] == 2)
    assert missing["game_toi_min"] is None
    assert missing["game_shots"] == 0


# ------------------------------------- exact parity vs the local-store path


def test_stage2_recompute_matches_local_store_exactly(tmp_path, monkeypatch, capsys):
    """THE parity anchor (deliverable 6b): one historical date, the
    local-store path (model.shot_features() reading a REAL sqlite player_games
    store) vs the published-record path (run_daily_picks.
    shot_block_from_history) - same per-game lines -> identical 14 features,
    max abs diff < 1e-9 (observed 0.0)."""

    run_daily_picks = _load_run_daily_picks()
    days = ["2026-01-01", "2026-01-03", "2026-01-05", "2026-01-08"]
    rows = []
    for index, day in enumerate(days):
        for offset, player in enumerate([1, 2]):
            rows.append(
                {
                    "player_id": player,
                    "game_id": 1000 + index * 10 + offset,
                    "game_date": day,
                    "season": 20252026,
                    "position": "C",
                    "name": f"p{player}",
                    "team_abbrev": "TOR",
                    "home": 1,
                    "goals": (index + offset) % 2,
                    "shots": 1 + ((index + offset) % 4),
                    "toi": f"{10 + index:02d}:{(15 * offset) % 60:02d}",
                }
            )
    db = tmp_path / "raw.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE player_games (player_id INTEGER, game_id INTEGER, game_date TEXT, season INTEGER,"
        " position TEXT, name TEXT, team_abbrev TEXT, home INTEGER, goals INTEGER, shots INTEGER, toi TEXT)"
    )
    con.executemany(
        f"INSERT INTO player_games VALUES ({','.join('?' * 11)})",
        [
            tuple(
                r[c]
                for c in (
                    "player_id",
                    "game_id",
                    "game_date",
                    "season",
                    "position",
                    "name",
                    "team_abbrev",
                    "home",
                    "goals",
                    "shots",
                    "toi",
                )
            )
            for r in rows
        ],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(model, "RAW_DB", db)
    from model import shot_features  # noqa: PLC0415 - reads model.RAW_DB at call time

    x_date = days[-1]
    training = shot_features()
    training_rows = training[training["date"] == x_date].sort_values("player_id").reset_index(drop=True)

    # the published record: the same lines under the stage-2 column names
    history = pd.DataFrame(rows).rename(columns={"game_date": "date"})
    history["game_goals"] = history["goals"]
    history["game_shots"] = history["shots"]
    history["game_toi_min"] = model._toi_minutes(history["toi"])

    block = (
        run_daily_picks.shot_block_from_history(x_date, [1, 2], history).sort_values("player_id").reset_index(drop=True)
    )
    diffs = (training_rows[model.SHOT_FEATURES] - block[model.SHOT_FEATURES]).abs()
    observed = float(diffs.to_numpy().max())
    anchor = training_rows[["player_id", "sd_shots_pg", "career_goals_pg", "career_games"]].to_string(index=False)
    print(f"\nparity anchor {x_date}: max abs diff {observed!r}\n{anchor}")
    assert observed < 1e-9


def _load_run_daily_picks():
    spec = importlib.util.spec_from_file_location(
        "run_daily_picks_stage2_test", ROOT / "smartscore" / "scripts" / "backtrack" / "run_daily_picks.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
