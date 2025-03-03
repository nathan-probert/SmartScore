import ctypes
import os
import sys
import time

from smartscore_info_client.schemas.player_info import PlayerInfo, TestingPlayerInfoC

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from shared import get_data

from service import get_min_max  # noqa: E402
from utility import create_min_max  # noqa: E402


def get_players():
    data, labels = get_data()

    all_players = []
    for index, row in data.iterrows():
        if row["tims"] not in {0, 1, 2, 3}:
            row["tims"] = 0.0

        all_players.append(
            PlayerInfo(
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
                tims=row.get("tims", 0.0),
            )
        )
    return all_players


def create_player_info_array(players):
    # remove players who don't have scoring data or weren't a tims pick
    players = [player for player in players if player.scored not in {" ", "null"} and player.tims in {1, 2, 3}]

    ExtendedTestingPlayerArrayC = TestingPlayerInfoC * len(players)
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

            player_array[i].scored = float(player.scored)
            player_array[i].tims = float(player.tims)
            player_array[i].date = player.date.encode("utf-8")

    return player_array


# call C function
def call_c_function(all_players):
    player_array = create_player_info_array(all_players)
    size = len(player_array)
    probabilities = (ctypes.c_float * size)()
    print(f"Size: {size}")

    min_max_c = create_min_max(get_min_max())

    num_tims_dates = len(set(player.date for player in player_array))                
    players_lib = ctypes.CDLL("./smartscore/compiled_code.so")

    start_time = time.time()
    players_lib.test_weights(player_array, size, min_max_c, probabilities, num_tims_dates)
    print(f"C function took {time.time() - start_time} seconds")


if __name__ == "__main__":
    all_players = get_players()
    probabilities = call_c_function(all_players)
