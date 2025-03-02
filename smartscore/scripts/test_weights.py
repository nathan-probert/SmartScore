import ctypes
import os
import sys

from smartscore_info_client.schemas.db_player_info import PlayerDbInfo
from smartscore_info_client.schemas.player_info import PlayerInfoC

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from shared import get_data

from service import get_min_max  # noqa: E402
from utility import create_min_max  # noqa: E402


# get all players from lib/data.csv
def get_players() -> list[PlayerDbInfo]:
    data, labels = get_data()

    all_players = []
    for index, row in data.iterrows():
        all_players.append(
            PlayerDbInfo(
                _id={},
                date=row["date"],
                scored=row["scored"],
                name=row["name"],
                id={},
                team_name={},
                gpg=row["gpg"],
                five_gpg=row["five_gpg"],
                hgpg=row["hgpg"],
                tgpg=row["tgpg"],
                otga=row["otga"],
                tims=row["tims"],  # add this to class
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

    return player_array


# call C function
def call_c_function(all_players: list[PlayerDbInfo]):
    player_array = create_player_info_array(all_players)
    size = len(player_array)
    probabilities = (ctypes.c_float * size)()
    print(f"Size: {size}")

    min_max_c = create_min_max(get_min_max())

    players_lib = ctypes.CDLL("./smartscore/compiled_code.so")
    players_lib.process_players(player_array, size, min_max_c, probabilities)

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
