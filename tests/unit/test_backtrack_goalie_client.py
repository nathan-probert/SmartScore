"""goalie_client: projected-goalie fetch caching and season-wide mapping."""

import json

import goalie_client
import pytest


def _game(home_team, home_player, home_status, visit_team, visit_player, visit_status):
    return {
        "hometeam": home_team,
        "homePlayer": home_player,
        "homeStatus": home_status,
        "visitteam": visit_team,
        "visitPlayer": visit_player,
        "visitStatus": visit_status,
    }


def test_goalies_map_reads_starters_per_team_and_date(monkeypatch):
    monkeypatch.setattr(
        goalie_client,
        "season_games",
        lambda season, **kwargs: [
            {"id": 1, "date": "2024-02-20"},
            {"id": 2, "date": "2024-02-21"},
        ],
    )

    def fake_fetch(date_str, **kwargs):
        if date_str == "2024-02-20":
            return [
                _game("FLA", "Sergei Bobrovsky", "Confirmed", "OTT", "Joonas Korpisalo", "Expected"),
                # Rotowire's MON must normalize to the NHL API's MTL.
                _game("MON", "Sam Montembeault", "", "TOR", "Ilya Samsonov", None),
            ]
        return []

    monkeypatch.setattr(goalie_client, "fetch_goalies", fake_fetch)
    result = goalie_client.goalies_map("20232024")

    assert result["2024-02-20"]["FLA"] == {"name": "Sergei Bobrovsky", "status": "Confirmed"}
    assert result["2024-02-20"]["OTT"] == {"name": "Joonas Korpisalo", "status": "Expected"}
    assert result["2024-02-20"]["MTL"] == {"name": "Sam Montembeault", "status": ""}
    assert result["2024-02-20"]["TOR"] == {"name": "Ilya Samsonov", "status": ""}
    # A date with no games contributes an empty map, not a crash.
    assert result["2024-02-21"] == {}


def test_fetch_goalies_caches_to_disk(tmp_path, monkeypatch):
    calls = []
    payload = [_game("FLA", "Sergei Bobrovsky", "Confirmed", "OTT", "Joonas Korpisalo", "Expected")]

    monkeypatch.setattr(goalie_client, "_get_json", lambda *a, **kw: calls.append(a) or payload)

    first = goalie_client.fetch_goalies("2024-02-20", cache_dir=tmp_path)
    second = goalie_client.fetch_goalies("2024-02-20", cache_dir=tmp_path)

    assert first == payload
    assert second == payload
    assert len(calls) == 1
    cached = json.loads((tmp_path / "goalies-2024-02-20.json").read_text(encoding="utf-8"))
    assert cached == payload


def test_fetch_goalies_offseason_empty_list_is_cached_as_empty(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(goalie_client, "_get_json", lambda *a, **kw: calls.append(a) or [])

    assert goalie_client.fetch_goalies("2024-07-15", cache_dir=tmp_path) == []
    assert goalie_client.fetch_goalies("2024-07-15", cache_dir=tmp_path) == []
    assert len(calls) == 1


def test_fetch_goalies_rejects_a_shape_change(tmp_path, monkeypatch):
    # A non-list payload means the table endpoint changed shape; caching it
    # would poison every later publish that reads the file.
    monkeypatch.setattr(goalie_client, "_get_json", lambda *a, **kw: {"error": "not a list"})

    with pytest.raises(ValueError, match="unexpected goalies payload type"):
        goalie_client.fetch_goalies("2024-02-20", cache_dir=tmp_path)

    assert not (tmp_path / "goalies-2024-02-20.json").exists()
