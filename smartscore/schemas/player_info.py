from dataclasses import dataclass, field

import requests
from marshmallow import Schema, fields


def get_stats(data):
    pass


@dataclass(frozen=True)
class PlayerInfo:
    name: str
    id: int

    gpg: float = field(init=False, default=0.0)
    hgpg: float = field(init=False, default=0.0)
    five_gpg: float = field(init=False, default=0.0)

    def __post_init__(self):
        URL = f"https://api-web.nhle.com/v1/player/{self.id}/landing"
        _data = requests.get(URL, timeout=10).json()

        object.__setattr__(self, "gpg", get_gpg(_data))
        object.__setattr__(self, "hgpg", get_hgpg(_data))
        object.__setattr__(self, "five_gpg", get_five_gpg(_data))

        # print(f"Player: {self.name}")
        # print(f"Goals per game: {self.gpg}")
        # print(f"Goals per game over the past 3 years: {self.hgpg}")
        # print(f"Goals per game over the past 5 games: {self.five_gpg}")
        # print("")
        # exit()


class PlayerInfoSchema(Schema):
    name = fields.Str()
    id = fields.Int()

    gpg = fields.Float()
    hgpg = fields.Float()
    five_gpg = fields.Float()


PLAYER_INFO_SCHEMA = PlayerInfoSchema()


def get_gpg(_data):
    return get_hgpg(_data, 1)


def get_hgpg(_data, years: int = 3):
    goals = 0
    games = 0

    cur_season = str(_data["seasonTotals"][-1]["season"])

    acceptable_seasons = get_acceptable_seasons(cur_season, years)
    for season_data in _data["seasonTotals"]:
        if (str(season_data["season"]) in acceptable_seasons) and season_data["leagueAbbrev"] == "NHL":
            goals += season_data["goals"]
            games += season_data["gamesPlayed"]

    return goals / games if games != 0 else 0.0


def get_five_gpg(_data):
    goals = 0
    games = 5

    try:
        for game_data in _data["last5Games"]:
            goals += game_data["goals"]
    except KeyError:
        return 0

    return goals / games


# helper functions
def get_acceptable_seasons(current_season: str, years: int) -> list:
    acceptable_seasons = []

    for _ in range(years):
        acceptable_seasons.append(current_season)
        current_season = get_previous_season(current_season)

    return acceptable_seasons


def get_previous_season(current_season: str) -> str:
    year_start = int(current_season[:4])
    year_end = int(current_season[4:])

    if year_end == 0:
        previous_season = f"{year_start - 1}9999"
    else:
        previous_season = f"{year_start - 1}{year_end - 1:02d}"

    return previous_season
