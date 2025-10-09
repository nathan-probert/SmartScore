import os
import sys
import time

from smartscore_info_client.schemas.player_info import PlayerInfo

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from shared import get_data

from service import get_min_max  # noqa: E402

# Import the Rust module
import test_weights_rust


def create_rust_min_max(min_max):
    """Create a Rust MinMax object from the min_max dictionary"""
    return test_weights_rust.MinMax(
        min_max.get("gpg", {}).get("min", 0.0),
        min_max.get("gpg", {}).get("max", 1.0),
        min_max.get("hgpg", {}).get("min", 0.0),
        min_max.get("hgpg", {}).get("max", 1.0),
        min_max.get("five_gpg", {}).get("min", 0.0),
        min_max.get("five_gpg", {}).get("max", 1.0),
        min_max.get("tgpg", {}).get("min", 0.0),
        min_max.get("tgpg", {}).get("max", 1.0),
        min_max.get("otga", {}).get("min", 0.0),
        min_max.get("otga", {}).get("max", 1.0),
        min_max.get("hppg", {}).get("min", 0.0),
        min_max.get("hppg", {}).get("max", 1.0),
        min_max.get("otshga", {}).get("min", 0.0),
        min_max.get("otshga", {}).get("max", 1.0),
    )


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


def create_rust_player_list(players):
    """Create a list of Rust PlayerInfo objects"""
    # Filter players who have scoring data and were tims picks
    filtered_players = [player for player in players if player.scored not in {" ", "null"} and player.tims in {1, 2, 3}]
    
    rust_players = []
    for player in filtered_players:
        # Convert player data to Rust PlayerInfo object
        rust_player = test_weights_rust.PlayerInfo(
            float(player.gpg),
            float(player.hgpg),
            float(player.five_gpg),
            float(player.tgpg),
            float(player.otga),
            float(player.hppg),
            float(player.otshga),
            float(player.is_home),
            0.0,  # hppg_otshga will be calculated in Rust
            float(player.scored),
            float(player.tims),
            str(player.date)
        )
        rust_players.append(rust_player)
    
    return rust_players


# Call Rust function to test weights
def test_weights_with_rust(all_players, top_n=10):
    """Test different weight combinations using Rust and return the best ones"""
    rust_players = create_rust_player_list(all_players)
    rust_min_max = create_rust_min_max(get_min_max())
    
    print(f"Testing weights with {len(rust_players)} players")
    
    start_time = time.time()
    results = test_weights_rust.test_weights(rust_players, rust_min_max, top_n)
    elapsed_time = time.time() - start_time
    
    print(f"Rust weight testing took {elapsed_time:.2f} seconds")
    print(f"Found {len(results)} weight combinations")
    
    # Display results
    for i, result in enumerate(results):
        weights = result.weights
        accuracy = result.accuracy
        print(f"Rank {i+1}: Accuracy = {accuracy:.4f}")
        print(f"  Weights: gpg={weights.gpg:.3f}, hgpg={weights.hgpg:.3f}, five_gpg={weights.five_gpg:.3f}")
        print(f"          tgpg={weights.tgpg:.3f}, otga={weights.otga:.3f}, is_home={weights.is_home:.3f}")
        print(f"          hppg_otshga={weights.hppg_otshga:.3f}")
        print()
    
    return results


if __name__ == "__main__":
    all_players = get_players()
    
    # Test different weight combinations and get the best ones
    best_weights = test_weights_with_rust(all_players, top_n=10)
    
    if best_weights:
        print("\n" + "="*50)
        print("BEST WEIGHT COMBINATION:")
        print("="*50)
        best_result = best_weights[0]
        best_weights_obj = best_result.weights
        print(f"Accuracy: {best_result.accuracy:.4f}")
        print(f"Weights:")
        print(f"  gpg: {best_weights_obj.gpg:.3f}")
        print(f"  hgpg: {best_weights_obj.hgpg:.3f}")
        print(f"  five_gpg: {best_weights_obj.five_gpg:.3f}")
        print(f"  tgpg: {best_weights_obj.tgpg:.3f}")
        print(f"  otga: {best_weights_obj.otga:.3f}")
        print(f"  is_home: {best_weights_obj.is_home:.3f}")
        print(f"  hppg_otshga: {best_weights_obj.hppg_otshga:.3f}")
        
        # Test prediction with the best weights
        print("\n" + "="*50)
        print("TESTING PREDICTION WITH BEST WEIGHTS:")
        print("="*50)
        rust_players = create_rust_player_list(all_players)
        rust_min_max = create_rust_min_max(get_min_max())
        
        probabilities = test_weights_rust.predict_with_weights(
            rust_players, rust_min_max, best_weights_obj
        )
        
        print(f"Generated {len(probabilities)} predictions")
        print(f"Sample predictions: {probabilities[:5]}")
    else:
        print("No weight combinations found!")
