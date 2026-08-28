"""
A stand-in for ``smartscore_info_client.NHLClient`` which serves frozen,
recorded NHL API payloads instead of hitting the live endpoints.

Used when the ``mock-nhl-api`` feature flag is enabled so that deployed
Lambdas (and integration tests / manual dev poking around in the off-season)
can run against realistic data without depending on live NHL games.

The mock mirrors ``NHLClient``'s method interface and reuses the real model
parsing helpers (``PlayerStats.from_landing_payload``,
``TeamStats.from_payloads``) so the parsing pipeline is still exercised.
"""

import json
import os
from pathlib import Path

from smartscore_info_client.api.nhle import NHLClient
from smartscore_info_client.models.player import PlayerStats
from smartscore_info_client.models.team import TeamStats

# Fixtures are staged into the Lambda deployment zip at <root>/fixtures/nhl
# (dev only). In local tests the base directory is always passed explicitly.
_FIXTURES_DIR = Path(os.environ.get("NHL_FIXTURES_DIR", "fixtures/nhl"))

# The mock serves a frozen day of games. ``get_schedule``/``get_score`` ignore
# the caller-provided date (e.g. "today") and always return this fixture, so
# the mock keeps working in the off-season regardless of the runtime date.
_MOCK_DATE = os.environ.get("NHL_MOCK_DATE", "2025-06-11")


class MockNHLClient(NHLClient):
    """NHLClient whose methods return frozen fixtures keyed by path."""

    def __init__(self, fixtures_dir: Path = _FIXTURES_DIR):
        self._fixtures_dir = Path(fixtures_dir)
        self._cache = {}
        super().__init__()

    def _load(self, relative_path: str):
        """Load and cache a JSON fixture by a path relative to the fixtures dir."""
        if relative_path not in self._cache:
            file_path = self._fixtures_dir / relative_path
            with open(file_path, "r", encoding="utf-8") as f:
                self._cache[relative_path] = json.load(f)
        return self._cache[relative_path]

    def get_schedule(self, date):
        return self._load(f"schedule/{_MOCK_DATE}.json")

    def get_score(self, date):
        return self._load(f"score/{_MOCK_DATE}.json")

    def get_roster(self, team_abbr):
        return self._load(f"roster/{team_abbr}.json")

    def get_player_landing(self, player_id):
        return self._load(f"player/{player_id}.json")

    def get_player_stats(self, player_id, years=3) -> PlayerStats:
        return PlayerStats.from_landing_payload(self.get_player_landing(player_id), years=years)

    def get_team_summary(self, season):
        return self._load(f"team/summary_{season}.json")

    def get_team_penalty_kill(self, season):
        return self._load(f"team/penalty_kill_{season}.json")

    def get_team_stats(self, season, team_id, opponent_id) -> TeamStats:
        return TeamStats.from_payloads(
            self.get_team_summary(season),
            self.get_team_penalty_kill(season),
            team_id,
            opponent_id,
        )
