import ctypes
import os
import sys
from dataclasses import dataclass, field

from marshmallow import Schema, fields

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from shared import get_data

from service import get_min_max  # noqa: E402
from utility import create_min_max  # noqa: E402

class ExtendedPlayerInfoC(ctypes.Structure):
    _fields_ = [
        ("gpg", ctypes.c_float),
        ("hgpg", ctypes.c_float),
        ("five_gpg", ctypes.c_float),
        ("tgpg", ctypes.c_float),
        ("otga", ctypes.c_float),
        ("hppg", ctypes.c_float),
        ("otshga", ctypes.c_float),
        ("is_home", ctypes.c_float),
        ("hppg_otshga", ctypes.c_float)
    ]


class ExtendedTestingPlayerInfoC(ctypes.Structure):
    _fields_ = [
        ("gpg", ctypes.c_float),
        ("hgpg", ctypes.c_float),
        ("five_gpg", ctypes.c_float),
        ("tgpg", ctypes.c_float),
        ("otga", ctypes.c_float),
        ("hppg", ctypes.c_float),
        ("otshga", ctypes.c_float),
        ("is_home", ctypes.c_float),
        ("hppg_otshga", ctypes.c_float),
        ("scored", ctypes.c_float),
        ("tims", ctypes.c_float),
        ("date", ctypes.c_char_p),
    ]


@dataclass(frozen=True)
class ExtendedPlayerInfo:
    name: str
    date: str

    gpg: float
    hgpg: float
    five_gpg: float
    hppg: float

    scored: int = field(default=None)
    tims: int = field(default=0)

    tgpg: float = field(default=0.0)
    otga: float = field(default=0.0)
    otshga: float = field(default=0.0)
    is_home: float = field(default=False)

    hppg_otshga: float = field(default=0.0)


class ExtendedPlayerInfoSchema(Schema):
    name = fields.Str()
    date = fields.Str()

    gpg = fields.Float()
    hgpg = fields.Float()
    five_gpg = fields.Float()
    hppg = fields.Float()

    scored = fields.Int(allow_none=True)
    tims = fields.Int(allow_none=True)

    tgpg = fields.Float(allow_none=True)
    otga = fields.Float(allow_none=True)
    otshga = fields.Float(allow_none=True)
    is_home = fields.Float(allow_none=True)

    hppg_otshga = fields.Float(allow_none=True)


EXTENDED_PLAYER_INFO_SCHEMA = ExtendedPlayerInfoSchema()


def get_players():
    data, labels = get_data()

    all_players = []
    for index, row in data.iterrows():
        if row["tims"] not in {0, 1, 2, 3}:
            row["tims"] = 0.0

        all_players.append(
            ExtendedPlayerInfo(
                name=row["name"],
                date=row["date"],
                gpg=row["gpg"],
                hgpg=row["hgpg"],
                five_gpg=row["five_gpg"],
                hppg=row["hppg"],
                scored=row.get("scored", None),
                tgpg=row.get("tgpg", 0.0),
                otga=row.get("otga", 0.0),
                otshga=row.get("otshga", 0.0),
                is_home=row.get("home", 0.0),
                hppg_otshga=0.0,
                tims=row.get("tims", 0.0),
            )
        )
    return all_players


def create_player_info_array(players):
    ExtendedTestingPlayerArrayC = ExtendedTestingPlayerInfoC * len(players)
    player_array = ExtendedTestingPlayerArrayC()

    for i, player in enumerate(players):
        if player.scored not in {" ", "null"}:
            player_array[i].gpg = float(player.gpg)
            player_array[i].hgpg = float(player.hgpg)
            player_array[i].five_gpg = float(player.five_gpg)
            player_array[i].tgpg = float(player.tgpg)
            player_array[i].otga = float(player.otga)
            player_array[i].is_home = float(player.is_home)
            player_array[i].hppg = float(player.hppg)
            player_array[i].otshga = float(player.otshga)
            player_array[i].hppg_otshga = 0.0

    return player_array


# call C function
def call_c_function(all_players):
    player_array = create_player_info_array(all_players)
    size = len(player_array)
    probabilities = (ctypes.c_float * size)()
    print(f"Size: {size}")

    min_max_c = create_min_max(get_min_max())

    num_tims_dates = len(set(player.date for player in all_players if player.tims in {1, 2, 3}))
    players_lib = ctypes.CDLL("./smartscore/compiled_code.so")
    players_lib.test_weights(player_array, size, min_max_c, probabilities, num_tims_dates)


    exit()
    return probabilities


def check_probabilities(all_players, probabilities):
    correct = 0
    total = 0
    highest_prob_players = {}
    highest_prob_values = {}

    for player in all_players:
        if player.tims in {1, 2, 3}:
            date = player.date
            if date not in highest_prob_players:
                highest_prob_players[date] = {1: None, 2: None, 3: None}
                highest_prob_values[date] = {1: -1, 2: -1, 3: -1}

            if probabilities[all_players.index(player)] > highest_prob_values[date][player.tims]:
                highest_prob_values[date][player.tims] = probabilities[all_players.index(player)]
                highest_prob_players[date][player.tims] = player

    for date, players in highest_prob_players.items():
        for tims_group, player in players.items():
            if player:
                if player.scored:
                    correct += 1
                total += 1

    return total, correct


if __name__ == "__main__":
    all_players = get_players()
    probabilities = call_c_function(all_players)
    total, correct = check_probabilities(all_players, probabilities)
    print(f"Total: {total}, Correct: {correct}")
    print(f"Accuracy: {correct / total:.2f}")
