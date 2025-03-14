import os
import sys
import time

import make_predictions_rust  # noqa: E402
from smartscore_info_client.schemas.player_info import PlayerInfo  # adjust as needed

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from shared import get_data

from service import get_min_max  # noqa: E402


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


def create_testing_player_list(players):
    # Filter out players with no scoring data or not a tims pick.
    filtered = [p for p in players if p.scored not in {" ", "null"} and p.tims in {1, 2, 3}]
    testing_players = []
    for player in filtered:
        testing_players.append(
            make_predictions_rust.TestingPlayerInfo(
                gpg=float(player.gpg),
                hgpg=float(player.hgpg),
                five_gpg=float(player.five_gpg),
                tgpg=float(player.tgpg),
                otga=float(player.otga),
                hppg=float(player.hppg),
                otshga=float(player.otshga),
                is_home=float(player.is_home),
                hppg_otshga=0.0,
                scored=float(player.scored),
                tims=float(player.tims),
                date=player.date,  # pass the date as a string
            )
        )
    return testing_players


def call_rust_function(all_players):
    testing_players = create_testing_player_list(all_players)
    min_max_vals = get_min_max()
    min_max = make_predictions_rust.MinMax(
        min_gpg=min_max_vals["gpg"]["min"],
        max_gpg=min_max_vals["gpg"]["max"],
        min_hgpg=min_max_vals["hgpg"]["min"],
        max_hgpg=min_max_vals["hgpg"]["max"],
        min_five_gpg=min_max_vals["five_gpg"]["min"],
        max_five_gpg=min_max_vals["five_gpg"]["max"],
        min_tgpg=min_max_vals["tgpg"]["min"],
        max_tgpg=min_max_vals["tgpg"]["max"],
        min_otga=min_max_vals["otga"]["min"],
        max_otga=min_max_vals["otga"]["max"],
        min_hppg=min_max_vals["hppg"]["min"],
        max_hppg=min_max_vals["hppg"]["max"],
        min_otshga=min_max_vals["otshga"]["min"],
        max_otshga=min_max_vals["otshga"]["max"],
    )

    print(f"Number of players: {len(testing_players)}")
    start_time = time.time()
    result = make_predictions_rust.test_weights(testing_players, min_max)
    print(f"Rust function took {time.time() - start_time:.2f} seconds")
    print("Result from Rust function:", result)


if __name__ == "__main__":
    players = get_players()
    call_rust_function(players)
