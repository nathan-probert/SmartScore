#!/usr/bin/env python3

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import make_predictions_rust
from shared import get_data
from service import get_min_max


def create_min_max_dict(min_max):
    return {
        "min_gpg": min_max.get("gpg", {}).get("min"),
        "max_gpg": min_max.get("gpg", {}).get("max"),
        "min_hgpg": min_max.get("hgpg", {}).get("min"),
        "max_hgpg": min_max.get("hgpg", {}).get("max"),
        "min_five_gpg": min_max.get("five_gpg", {}).get("min"),
        "max_five_gpg": min_max.get("five_gpg", {}).get("max"),
        "min_tgpg": min_max.get("tgpg", {}).get("min"),
        "max_tgpg": min_max.get("tgpg", {}).get("max"),
        "min_otga": min_max.get("otga", {}).get("min"),
        "max_otga": min_max.get("otga", {}).get("max"),
        "min_hppg": min_max.get("hppg", {}).get("min"),
        "max_hppg": min_max.get("hppg", {}).get("max"),
        "min_otshga": min_max.get("otshga", {}).get("min"),
        "max_otshga": min_max.get("otshga", {}).get("max"),
    }


def get_players_with_names():
    data, labels = get_data()

    all_players = []
    for index, row in data.iterrows():
        if row["tims"] not in {0, 1, 2, 3}:
            row["tims"] = 0.0

        player = make_predictions_rust.PlayerInfo(
            gpg=row["gpg"],
            hgpg=row["hgpg"],
            five_gpg=row["five_gpg"],
            tgpg=row.get("tgpg", 0.0),
            otga=row.get("otga", 0.0),
            hppg=row["hppg"],
            otshga=row.get("otshga", 0.0),
            is_home=row.get("home", 0.0),
            hppg_otshga=0.0,
            scored=row.get("scored", 0.0),
            tims=int(row.get("tims", 0.0)),
            date=row["date"],
        )
        name = row["name"]
        all_players.append((player, name))
    return all_players


def main():
    # Create Weights object
    weights = make_predictions_rust.Weights(
        gpg=0.76,
        hgpg=0.06,
        five_gpg=0.0,
        tgpg=0.0,
        otga=0.04,
        is_home=0.06,
        hppg_otshga=0.08,
    )
    # weights = make_predictions_rust.Weights(
    #     gpg=0.3,
    #     five_gpg=0.4,
    #     hgpg=0.3,
    #     tgpg=0.0,
    #     otga=0.0,
    #     hppg_otshga=0.0,
    #     is_home=0.0,
    # )


    # Get players with names
    all_players_with_names = get_players_with_names()

    # Filter players who have scoring data
    filtered_players_with_names = [
        (player, name) for player, name in all_players_with_names
        if player.scored in {0.0, 1.0}
    ]

    # Create min_max object
    min_max = create_min_max_dict(get_min_max())
    min_max_obj = make_predictions_rust.MinMax(**min_max)

    # Separate players and names
    filtered_players = [player for player, name in filtered_players_with_names]
    names = [name for player, name in filtered_players_with_names]

    # Get probabilities for all players at once
    probabilities = make_predictions_rust.predict(filtered_players, min_max_obj, weights)

    # Group players by date and tims group
    from collections import defaultdict
    date_tims_groups = defaultdict(lambda: defaultdict(list))
    
    for (player, name), probability in zip(filtered_players_with_names, probabilities):
        if player.tims in {1, 2, 3}:
            date_tims_groups[player.date][player.tims].append((player, name, probability))

    # Print header
    print(f"{'Date':<12} {'Name':<20} {'Probability':<12} {'Scored (T/F)':<12} {'Tims':<8}")
    print("-" * 80)

    # For each date, get top player from each tims group (1, 2, 3)
    all_selected_players = []
    for date in sorted(date_tims_groups.keys()):
        tims_groups = date_tims_groups[date]
        
        # For each tims group, get the player with highest probability
        selected_players = []
        for tims_group in [1, 2, 3]:
            if tims_group in tims_groups:
                # Sort by probability descending and take the top one
                top_player = max(tims_groups[tims_group], key=lambda x: x[2])
                selected_players.append(top_player)
        
        # Sort selected players by probability descending
        selected_players.sort(key=lambda x: x[2], reverse=True)
        
        for player, name, probability in selected_players:
            scored_tf = "T" if player.scored == 1.0 else "F"
            print(f"{date:<12} {name:<20} {probability:<12.2f} {scored_tf:<12} {player.tims:<8}")
            all_selected_players.append((player, name, probability))

    # Calculate statistics
    total_picks = len(all_selected_players)
    correct_picks = sum(1 for player, _, _ in all_selected_players if player.scored == 1.0)
    correct_percentage = (correct_picks / total_picks * 100) if total_picks > 0 else 0
    
    print(f"\n{'='*80}")
    print(f"FINAL RESULTS:")
    print(f"Total picks: {total_picks}")
    print(f"Correct picks: {correct_picks}")
    print(f"Correct pick percentage: {correct_percentage:.1f}%")
    print(f"{'='*80}")

    print(f"\nHardcoded weights used: gpg={weights.gpg:.2f}, hgpg={weights.hgpg:.2f}, five_gpg={weights.five_gpg:.2f}, tgpg={weights.tgpg:.2f}, otga={weights.otga:.2f}, is_home={weights.is_home:.2f}, hppg_otshga={weights.hppg_otshga:.2f}")


if __name__ == "__main__":
    main()