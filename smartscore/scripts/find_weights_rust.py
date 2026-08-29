import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import make_predictions_rust
from shared import get_data

from service import get_min_max  # noqa: E402


def test_chunk(filtered_players, min_max_obj, weight_chunk):
    return make_predictions_rust.test_weights(filtered_players, min_max_obj, weight_chunk)


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


def get_players():
    data, labels = get_data()

    all_players = []
    for index, row in data.iterrows():
        if row["tims"] not in {0, 1, 2, 3}:
            row["tims"] = 0.0

        all_players.append(
            make_predictions_rust.PlayerInfo(
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
        )
    return all_players


def call_rust_function(all_players):
    # Filter players who have scoring data and were tims picks
    filtered_players = [player for player in all_players if player.scored in {0.0, 1.0} and player.tims in {1, 2, 3}]

    # Sort players by date to ensure proper grouping
    filtered_players.sort(key=lambda p: p.date)

    # Create min_max object
    min_max = create_min_max_dict(get_min_max())
    min_max_obj = make_predictions_rust.MinMax(**min_max)

    # Generate all possible weight combinations using Rust implementation
    print("Generating weight permutations...")
    weight_combinations = make_predictions_rust.generate_weight_permutations(5)

    # Split weights and test in parallel
    logical_cores = os.cpu_count() or 4
    print(f"Testing {len(weight_combinations) // logical_cores} weight combinations using {logical_cores} CPU cores...")
    num_processes = min(logical_cores, len(weight_combinations))
    weight_chunks = [weight_combinations[i::num_processes] for i in range(num_processes)]

    start_time_rust = time.time()
    with ThreadPoolExecutor(max_workers=num_processes) as executor:
        results = list(executor.map(lambda wc: test_chunk(filtered_players, min_max_obj, wc), weight_chunks))
    rust_duration = time.time() - start_time_rust

    # Find the best among results
    best_result = max(results, key=lambda x: x[1])
    best_weights, max_correct, total_predictions = best_result

    print(f"Rust function took {rust_duration:.2f} seconds")
    print(f"Best weights: {best_weights}")
    print(f"Max correct: {max_correct}")
    print(f"Total predictions: {total_predictions}")
    print(f"Accuracy: {max_correct / total_predictions:.1%}")

    return best_weights, max_correct


if __name__ == "__main__":
    all_players = get_players()
    best_weights, max_correct = call_rust_function(all_players)
