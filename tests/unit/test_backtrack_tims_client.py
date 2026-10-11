"""tims_client: history fetch caching and season-wide group mapping."""

import json
from types import SimpleNamespace

import pytest
import requests
import tims_client


def _history_payload(group_players):
    return {
        "date": "2024-02-20T00:00:00",
        "playerLists": [
            {"id": index + 1, "players": [{"nhlPlayerId": pid} for pid in pids]}
            for index, pids in enumerate(group_players)
        ],
    }


def test_tims_map_reads_group_ids_per_date(monkeypatch):
    monkeypatch.setattr(
        tims_client,
        "season_games",
        lambda season, **kwargs: [
            {"id": 1, "date": "2024-02-20"},
            {"id": 2, "date": "2024-02-21"},
        ],
    )

    def fake_fetch(date_str, **kwargs):
        if date_str == "2024-02-20":
            return _history_payload([[111, 112], [221], [331, 332]])
        return {"date": date_str, "playerLists": []}

    monkeypatch.setattr(tims_client, "fetch_history", fake_fetch)
    result = tims_client.tims_map("20232024")

    assert result["2024-02-20"] == {111: 1, 112: 1, 221: 2, 331: 3, 332: 3}
    # A date with no challenge data contributes an empty map, not a crash.
    assert result["2024-02-21"] == {}


def test_fetch_history_caches_to_disk(tmp_path, monkeypatch):
    calls = []
    payload = _history_payload([[111]])

    monkeypatch.setattr(tims_client, "_get_json", lambda *a, **kw: calls.append(a) or payload)

    first = tims_client.fetch_history("2024-02-20", cache_dir=tmp_path)
    second = tims_client.fetch_history("2024-02-20", cache_dir=tmp_path)

    assert first == payload
    assert second == payload
    assert len(calls) == 1
    cached = json.loads((tmp_path / "tims-2024-02-20.json").read_text(encoding="utf-8"))
    assert cached == payload


def test_fetch_history_404_is_an_empty_day_not_an_error(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.HTTPError(response=SimpleNamespace(status_code=404))

    monkeypatch.setattr(tims_client, "_get_json", boom)
    result = tims_client.fetch_history("2024-07-15", cache_dir=tmp_path)

    assert result == {"date": "2024-07-15", "playerLists": []}


def test_fetch_history_other_http_errors_still_raise(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.HTTPError(response=SimpleNamespace(status_code=403))

    monkeypatch.setattr(tims_client, "_get_json", boom)

    with pytest.raises(requests.HTTPError):
        tims_client.fetch_history("2024-02-20", cache_dir=tmp_path)
