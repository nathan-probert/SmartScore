import csv
import ctypes
import sys

from smartscore_info_client.schemas.player_info import PlayerDbInfo

sys.path.append("../smartscore")

from service import get_min_max  # noqa: E402
from utility import create_min_max  # noqa: E402


class PlayerInfoC(ctypes.Structure):
    _fields_ = [
        ("gpg", ctypes.c_float),
        ("hgpg", ctypes.c_float),
        ("five_gpg", ctypes.c_float),
        ("tgpg", ctypes.c_float),
        ("otga", ctypes.c_float),
        ("scored", ctypes.c_int),
    ]


# get all players from lib/data.csv
def get_players() -> list[PlayerDbInfo]:
    all_players = []
    with open("../lib/data.csv", "r") as f:
        reader = csv.reader(f)
        next(reader)

        for row in reader:
            all_players.append(
                PlayerDbInfo(
                    _id={},
                    date=row[0],
                    scored=row[1],
                    name=row[2],
                    id=row[3],
                    team_name=row[4],
                    gpg=row[6],
                    five_gpg=row[7],
                    hgpg=row[8],
                    tgpg=row[11],
                    otga=row[12],
                )
            )
    return all_players


def create_player_info_array(players):
    PlayerArrayC = PlayerInfoC * len(players)
    player_array = PlayerArrayC()

    for i, player in enumerate(players):
        if player.scored not in {" ", "null"}:
            player_array[i].gpg = float(player.gpg)
            player_array[i].hgpg = float(player.hgpg)
            player_array[i].five_gpg = float(player.five_gpg)
            player_array[i].tgpg = float(player.tgpg)
            player_array[i].otga = float(player.otga)
            player_array[i].scored = int(player.scored)

    return player_array


# call C function
def call_c_function(all_players: list[PlayerDbInfo]):
    player_array = create_player_info_array(all_players)
    size = len(player_array)

    min_max_c = create_min_max(get_min_max())

    players_lib = ctypes.CDLL("./scripts/compiled_code.so")
    players_lib.process_players(player_array, size, min_max_c)


if __name__ == "__main__":
    all_players = get_players()
    call_c_function(all_players)
