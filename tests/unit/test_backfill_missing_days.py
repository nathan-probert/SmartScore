"""Unit tests for the stage-2 gap-fill helper (backfill_missing_days.py):
write semantics (raw-columns-only top-up, minimal-row creation, marker
idempotency, dry = zero writes), the FINAL-state / fresh-cache guards, and
the exact rolling/career recompute's parity against the local-store path.
"""

import datetime
import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

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
DAY_BEFORE = "2026-10-24"

# One precomputed rolling column with a sentinel value on the published rows:
# if any upsert payload ever carried it, the merge-duplicates apply below
# would overwrite the sentinel and the test fails.
PRECOMPUTED_SENTINEL = 9.9


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
            "points": 2,
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
            "points": 0,
            "shots": 0,
            "toi": None,
        },
        {
            "game_id": GAME_ID,
            "game_date": DAY,
            "player_id": 3,
            "name": "C. Newcomer",
            "team_abbrev": "MTL",
            "position": "W",
            "goals": 0,
            "points": 1,
            "shots": 2,
            "toi": "12:00",
        },
        {
            "game_id": GAME_ID,
            "game_date": DAY,
            "player_id": 99,
            "name": "G. Goalie",
            "team_abbrev": "TOR",
            "position": "G",
            "goals": 0,
            "points": 0,
            "shots": 0,
            "toi": "60:00",
        },
    ]


def fake_schedule(states=("FINAL", "OFF", "LIVE", "FUT", "PPD")):
    """A fetch_schedule payload for one week with DAY's games (one per state)."""
    games = []
    for index, state in enumerate(states):
        games.append(
            {
                "id": GAME_ID + index,
                "gameType": 2,
                "gameState": state,
                "gameDate": DAY,
                "startTimeUTC": f"{DAY}T23:00:00Z",
                "homeTeam": {"abbrev": "TOR"},
                "awayTeam": {"abbrev": "MTL"},
            }
        )
    return {"gameWeek": [{"date": DAY, "games": games}]}


class FakeSnapshotTable:
    """Player-Snapshots-backtrack fake: merge-duplicates upsert semantics -
    an existing (date, player_id) row is updated ONLY in the payload's keys
    (a missing key survives untouched), a missing row is appended."""

    def __init__(self, frame):
        self._frame = frame
        self.upserts = []
        self._eq = None
        self._low = None
        self._desc_sort = False

    def select(self, *_a, **_k):
        self._eq = None
        self._low = None
        self._desc_sort = False
        if hasattr(self, "_page"):
            del self._page  # a new query chain starts fresh (real PostgREST builders do)
        return self

    def order(self, _col, desc=False):
        self._desc_sort = bool(desc)
        return self

    def limit(self, _n):
        return self

    def eq(self, _col, value):
        self._eq = value
        return self

    def gte(self, _col, value):
        self._low = value
        return self

    def range(self, lo, hi):
        self._page = (lo, hi)
        return self

    def _view(self):
        rows = self._frame
        if self._eq is not None:
            rows = rows[rows["date"].astype(str) == str(self._eq)]
        if self._low is not None:
            rows = rows[rows["date"].astype(str) >= str(self._low)]
        if self._desc_sort:
            rows = rows.sort_values("date", ascending=False)
        return rows

    def execute(self):
        rows = self._view()
        if hasattr(self, "_page"):
            lo, hi = self._page
            rows = rows.iloc[lo : min(hi + 1, len(rows))]
        return MagicMock(data=rows.to_dict("records"))

    def upsert(self, rows, on_conflict=None, returning=None):
        assert on_conflict == "date,player_id"
        assert _is_json_safe(rows)
        self.upserts.append(list(rows))
        return self

    def apply_upserts(self):
        """Merge-duplicates: existing keys update provided keys ONLY."""
        for batch in self.upserts:
            for row in batch:
                mask = (self._frame["date"].astype(str) == str(row["date"])) & (
                    self._frame["player_id"].astype(int) == int(row["player_id"])
                )
                if mask.any():
                    for key, value in row.items():
                        self._frame.loc[mask, key] = value
                else:
                    self._frame = pd.concat([self._frame, pd.DataFrame([row])], ignore_index=True)


class FakeMarkerTable(FakeSnapshotTable):
    """Player-Game-Raw-Markers fake: PK (date,) upserts, readable back."""

    def upsert(self, rows, on_conflict=None, returning=None):
        assert on_conflict == "date"
        assert _is_json_safe(rows)
        self.upserts.append(list(rows))
        self.apply_upserts()  # markers are readable by the very next read
        return self

    def apply_upserts(self):
        for batch in self.upserts:
            for row in batch:
                mask = self._frame["date"].astype(str) == str(row["date"])
                if mask.any():
                    for key, value in row.items():
                        self._frame.loc[mask, key] = value
                else:
                    self._frame = pd.concat([self._frame, pd.DataFrame([row])], ignore_index=True)


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


def published_frame(with_day_rows=False):
    """Rows the publish already owns: dates strictly before DAY (so
    latest_published_date < through and the window opens), each carrying a
    sentinel precomputed rolling column the backfill must never touch.
    ``with_day_rows`` adds DAY rows (the two-lane test patches
    latest_published_date to keep the window open)."""
    rows = []
    for day in ("2026-10-20", DAY_BEFORE):
        for player in (1, 2):
            rows.append(
                {
                    "date": day,
                    "player_id": player,
                    "name": f"p{player}",
                    "r10_shots_pg": PRECOMPUTED_SENTINEL,
                }
            )
    if with_day_rows:
        for player in (1, 2):
            rows.append({"date": DAY, "player_id": player, "name": f"p{player}", "r10_shots_pg": PRECOMPUTED_SENTINEL})
    return pd.DataFrame(rows)


@pytest.fixture
def wired(monkeypatch):
    """Latest published date = DAY_BEFORE; DAY's players 1+2 already
    published (with the sentinel precomputed column), player 3 absent."""
    snapshots = FakeSnapshotTable(published_frame())
    markers = FakeMarkerTable(pd.DataFrame(columns=["date", "games", "player_games", "backfilled_at"]))
    fake_client = MagicMock()

    def _table(name):
        if name == bf.player_table("prod"):
            return snapshots
        if name == bf.marker_table("prod"):
            return markers
        raise AssertionError(f"unexpected table {name}")

    fake_client.table.side_effect = _table
    monkeypatch.setattr(bf, "SUPABASE_ADMIN_CLIENT", fake_client)
    monkeypatch.setattr(bf, "fetch_schedule", lambda date, delay_seconds=None: fake_schedule())
    monkeypatch.setattr(bf, "fetch_boxscore", lambda game_id, delay_seconds=None, **_k: fake_boxscore_records())
    return snapshots, markers


# ----------------------------------------------------------- write semantics


def test_backfill_writes_only_missing_dates_rows(wired):
    """One missing day (DAY); the writes carry only that day's rows - the
    CREATE lane (identity + raw) for every boxscore skater the table lacks,
    goalies excluded."""
    snapshots, _markers = wired
    summary = bf.backfill("prod", through_date=DAY, dry=False)
    assert summary["games"] == 2  # FINAL + OFF are terminal; LIVE/FUT/PPD skipped
    upsert_dates = {row["date"] for batch in snapshots.upserts for row in batch}
    assert upsert_dates <= {DAY}
    keys = [(row["date"], row["player_id"]) for batch in snapshots.upserts for row in batch]
    assert (DAY, 1) in keys and (DAY, 2) in keys and (DAY, 3) in keys
    assert not any(key == (DAY, 99) for key in keys)  # goalies excluded
    raw_keys = {"date", "player_id", "game_goals", "game_shots", "game_points", "game_toi_min"}
    create_rows = [row for batch in snapshots.upserts for row in batch if set(row) != raw_keys]
    assert {row["player_id"] for row in create_rows} == {1, 2, 3}
    newcomer = next(row for row in create_rows if row["player_id"] == 3)
    assert newcomer["team_abbrev"] == "MTL"
    assert newcomer["opponent_abbrev"] == "TOR" and newcomer["scored"] == 0
    # the backtracking table carries NO season column (run_daily_picks derives
    # the season label from the date) - a season key would be a PGRST204 400
    assert "season" not in newcomer


def test_backfill_two_lanes_top_up_existing_create_absent(wired, monkeypatch):
    """The two-lane split: a player the table already published for DAY gets
    ONLY the raw block (update lane); an absent player gets the minimal
    identity+raw shape (create lane). latest_published_date is patched so the
    window stays open despite the DAY rows."""
    snapshots, _markers = wired
    day_rows = published_frame(with_day_rows=True)
    snapshots._frame = pd.concat([snapshots._frame, day_rows[day_rows["date"] == DAY]], ignore_index=True)
    monkeypatch.setattr(bf, "latest_published_date", lambda env: DAY_BEFORE)
    summary = bf.backfill("prod", through_date=DAY, dry=False)
    assert summary["games"] == 2
    raw_keys = {"date", "player_id", "game_goals", "game_shots", "game_points", "game_toi_min"}
    update_rows = [row for batch in snapshots.upserts for row in batch if set(row) == raw_keys]
    create_rows = [row for batch in snapshots.upserts for row in batch if set(row) != raw_keys]
    assert {row["player_id"] for row in update_rows} == {1, 2}
    assert {row["player_id"] for row in create_rows} == {3}


def test_backfill_never_overwrites_precomputed_columns_or_duplicates(wired, monkeypatch):
    """(test c) The first-run backfill on rows the publish already wrote:
    merge-duplicates applies ONLY the raw keys - the 16 precomputed rolling
    columns keep their published values, and no (date, player_id) row is
    duplicated."""
    snapshots, _markers = wired
    day_rows = published_frame(with_day_rows=True)
    snapshots._frame = pd.concat([snapshots._frame, day_rows[day_rows["date"] == DAY]], ignore_index=True)
    monkeypatch.setattr(bf, "latest_published_date", lambda env: DAY_BEFORE)
    before_rows = len(snapshots._frame)
    bf.backfill("prod", through_date=DAY, dry=False)
    snapshots.apply_upserts()
    preexisting = snapshots._frame.iloc[:before_rows]
    assert (preexisting["r10_shots_pg"] == PRECOMPUTED_SENTINEL).all()
    assert len(snapshots._frame) == before_rows + 1  # exactly one created row (player 3)
    keys = list(zip(snapshots._frame["date"].astype(str), snapshots._frame["player_id"].astype(int)))
    assert len(keys) == len(set(keys))


def test_backfill_marker_makes_second_run_a_pure_skip(wired, monkeypatch):
    """(test e) First-run-of-day semantics, both idempotency paths:

    * the created rows advance the table's latest published date, so the
      next pass finds latest >= through and stops before any request;
    * when NO row was created (all boxscore players already published), the
      latest date stays behind - the MARKER then settles it: the marked date
      is skipped with zero NHL requests and zero writes.
    """
    snapshots, markers = wired
    first = bf.backfill("prod", through_date=DAY, dry=False)
    assert first["backfilled"] == [DAY] and len(markers.upserts) == 1
    snapshots.apply_upserts()  # the created row advances latest_published_date

    fetches = []

    def _track_schedule(date, delay_seconds=None):
        fetches.append(date)
        return fake_schedule()

    monkeypatch.setattr(bf, "fetch_schedule", _track_schedule)
    batches_before = len(snapshots.upserts)
    second = bf.backfill("prod", through_date=DAY, dry=False)
    assert second["status"] == "up-to-date" and second["games"] == 0
    assert len(snapshots.upserts) == batches_before  # no new writes
    assert len(markers.upserts) == 1  # no second marker write
    assert fetches == []  # no NHL request at all

    # the marker-only path: drop every DAY row so latest falls back behind
    # DAY, keep the marker, re-run - the marked date is skipped, silently.
    snapshots._frame = snapshots._frame[snapshots._frame["date"].astype(str) != DAY].reset_index(drop=True)
    snapshots.upserts.clear()
    third = bf.backfill("prod", through_date=DAY, dry=False)
    assert third["skipped"] == [DAY] and third["games"] == 0
    assert snapshots.upserts == [] and fetches == []


def test_backfill_dry_prevents_all_writes(wired, capsys):
    """(test d) dry=True: the reads happen (schedule + boxscore), but ZERO
    Supabase writes of any kind - no player-row upsert, no marker row."""
    snapshots, markers = wired
    summary = bf.backfill("prod", through_date=DAY, dry=True)
    out = capsys.readouterr().out
    assert snapshots.upserts == [] and markers.upserts == []
    assert summary["rows"] > 0  # the dry pass still counts what it WOULD write
    assert "dry-read" in out and "(dry)" in out


def test_backfill_partial_failure_continues_and_reports(wired, monkeypatch):
    """One failed date stays UNMARKED and the run continues to the next -
    the summary names both (the picks pass proceeds either way)."""
    snapshots, markers = wired
    monkeypatch.setattr(bf, "latest_published_date", lambda env: "2026-10-23")  # window: 10-24 + DAY

    def _schedule(date, delay_seconds=None):
        if date == "2026-10-24":
            raise RuntimeError("schedule endpoint down")
        return fake_schedule()

    monkeypatch.setattr(bf, "fetch_schedule", _schedule)
    summary = bf.backfill("prod", through_date=DAY, dry=False)
    assert summary["status"] == "partial"
    assert summary["failed_days"] == ["2026-10-24"] and summary["backfilled"] == [DAY]
    assert markers._frame["date"].astype(str).tolist() == [DAY]  # the failed date is unmarked


def test_backfill_continues_past_live_games(wired, monkeypatch):
    """The FINAL-only guard: a LIVE box score is NEVER fetched (the cache-
    hostage hazard), so 3 of the 5 slate games are skipped with a note;
    boxscore requests exactly 2 (one per terminal game)."""
    snapshots, _markers = wired
    fetches = []

    def _track(game_id, delay_seconds=None, **_k):
        fetches.append(game_id)
        return fake_boxscore_records()

    monkeypatch.setattr(bf, "fetch_boxscore", _track)
    summary = bf.backfill("prod", through_date=DAY, dry=False)
    assert sorted(fetches) == [GAME_ID, GAME_ID + 1]
    assert summary["pending"] == [DAY]
    assert _rows_total(snapshots.upserts) == 2 * 3  # 3 skaters x 2 terminal games (goalie cut)


def _rows_total(upserts):
    return sum(len(batch) for batch in upserts)


def test_upsert_retry_envelope(wired, monkeypatch):
    """Three tries per batch with backoff (the repo's envelope): two
    transient failures then success still lands the batch."""
    snapshots, _markers = wired
    attempts = []
    sleeps = []
    real_upsert = snapshots.upsert

    def _flaky(rows, on_conflict=None, returning=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient 5xx")
        return real_upsert(rows, on_conflict=on_conflict, returning=returning)

    def _track_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(snapshots, "upsert", _flaky)
    monkeypatch.setattr(bf.time, "sleep", _track_sleep)
    written = bf._upsert_rows(bf.player_table("prod"), [{"date": DAY, "player_id": 7, "game_shots": 1}])
    assert written == 1 and len(attempts) == 3 and sleeps == [2.0, 4.0]


def test_fresh_cache_override_deletes_stale_boxscore_cache(wired, monkeypatch, tmp_path):
    """The cached-LIVE hazard, overridden: an existing boxscore-N.json cache
    file is DELETED before the fetch (the fresh GET re-caches final data);
    nhl_client itself is untouched."""
    snapshots, _markers = wired
    cache = tmp_path / "boxscore-2026010001.json"
    cache.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(bf, "CACHE_DIR", tmp_path)
    bf.backfill("prod", through_date=DAY, dry=False)
    assert not cache.exists()  # the stale file did not survive the pass


def test_backfill_skips_gracefully_pre_migration(monkeypatch, capsys):
    """A table without the 20261027 raw columns (pre-merge prod): the probe
    gates the helper - ONE loud warning, ZERO writes, no schedule request."""
    fake_client = MagicMock()
    fake_client.table.return_value.select.return_value.limit.return_value.execute.side_effect = RuntimeError(
        "column game_shots does not exist"
    )
    monkeypatch.setattr(bf, "SUPABASE_ADMIN_CLIENT", fake_client)
    monkeypatch.setattr(bf, "_RAW_SCHEMA_PROBE", {})
    fetches = []
    monkeypatch.setattr(bf, "fetch_schedule", lambda date, delay_seconds=None: fetches.append(date) or fake_schedule())
    summary = bf.backfill("prod", through_date=DAY, dry=False)
    out = capsys.readouterr().out
    assert summary["status"] == "skipped-columns" and summary["rows"] == 0
    assert fetches == []  # no NHL request either - the gate precedes the crawl
    assert "not landed" in out and "20261027" in out


def test_days_between_bounds_the_sweep():
    """A >14-day gap is capped: the helper never sweeps a season."""
    days = bf.days_between("2026-09-01", "2026-10-01")
    assert len(days) == 14
    assert days[0] == "2026-09-02" and days[-1] == "2026-09-15"


def test_raw_column_payload_toi_parsing():
    game = fake_schedule()["gameWeek"][0]["games"][0]
    update_rows, create_rows = bf._record_rows(game, fake_boxscore_records())
    forward = next(row for row in update_rows if row["player_id"] == 1)
    assert forward["game_toi_min"] == pytest.approx(18 + 31 / 60)
    assert forward["game_goals"] == 1 and forward["game_shots"] == 3
    assert forward["game_points"] == 2
    # missing TOI stays an honest None (the core drops + warns downstream)
    missing = next(row for row in update_rows if row["player_id"] == 2)
    assert missing["game_toi_min"] is None
    assert missing["game_shots"] == 0
    # the create lane carries the identity columns the minimal row needs
    newcomer = next(row for row in create_rows if row["player_id"] == 3)
    assert newcomer["team_abbrev"] == "MTL" and newcomer["opponent_abbrev"] == "TOR"


def test_yesterday_et_default():
    """--through-date defaults to ET-yesterday (not host-local blindly)."""
    stamp = datetime.datetime(2026, 10, 28, 1, 0, tzinfo=ZoneInfo("America/Toronto"))
    assert bf.yesterday_et(now=stamp) == "2026-10-27"


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


def test_null_raw_rows_warn_and_recompute_over_partial_prefix(capsys):
    """(test b) Published rows with NO per-game raw stat (pre-top-up
    lateness) are dropped from the ledger with a LOUD warning, and the
    rolling/ewm/career math runs over the available raw rows - verified
    against the shared core over the dropped frame (partial prefix)."""
    run_daily_picks = _load_run_daily_picks()
    days = ["2026-01-01", "2026-01-03", "2026-01-05", "2026-01-08"]
    rows = []
    for index, day in enumerate(days):
        rows.append(
            {
                "date": day,
                "player_id": 1,
                "game_goals": index % 2,
                "game_shots": 1 + (index % 4),
                "game_toi_min": 10.0 + index,
            }
        )
    history = pd.DataFrame(rows)
    history.loc[history["date"] == "2026-01-05", ["game_goals", "game_shots", "game_toi_min"]] = None

    block = run_daily_picks.shot_block_from_history("2026-01-08", [1], history)
    out = capsys.readouterr().out
    assert "WARNING" in out and "partial-prefix" in out and "1 published row" in out

    # the partial prefix IS what the shared core computes over the kept rows
    # (the kept frame already carries the on_date row, so no synthetic line)
    kept = history.dropna(subset=["game_shots"]).rename(
        columns={"game_goals": "goals", "game_shots": "shots", "game_toi_min": "toi"}
    )
    kept["season"] = "20252026"
    expected = model.shot_features_from_games(kept)
    expected = expected[expected["date"] == "2026-01-08"].reset_index(drop=True)
    diffs = (expected[model.SHOT_FEATURES] - block[model.SHOT_FEATURES]).abs().to_numpy().max()
    assert diffs < 1e-9


def _load_run_daily_picks():
    spec = importlib.util.spec_from_file_location(
        "run_daily_picks_stage2_test", ROOT / "smartscore" / "scripts" / "backtrack" / "run_daily_picks.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
