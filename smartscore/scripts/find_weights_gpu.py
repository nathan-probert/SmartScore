import os
import sys
import time

import numpy as np
import warp as wp

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import make_predictions_rust
from shared import get_data

from service import get_min_max  # noqa: E402

# Define the number of weights and step size (should match Rust logic)
NUM_WEIGHTS = 7
STEP_SIZE = 0.01
MAX_PLAYERS = 120000  # Maximum number of players to process
CHUNK_SIZE = 150_000  # Number of weight combinations per batch
LABELS = ["gpg", "five_gpg", "hgpg", "tgpg", "otga", "hppg_otshga", "is_home"]


# Warp kernel to evaluate weights
@wp.kernel
def evaluate_weights(
    gpg: wp.array(dtype=wp.float32),
    hgpg: wp.array(dtype=wp.float32),
    five_gpg: wp.array(dtype=wp.float32),
    tgpg: wp.array(dtype=wp.float32),
    otga: wp.array(dtype=wp.float32),
    hppg_otshga: wp.array(dtype=wp.float32),
    is_home: wp.array(dtype=wp.float32),
    scored: wp.array(dtype=wp.float32),
    tims: wp.array(dtype=wp.int32),
    date: wp.array(dtype=wp.int32),
    weights: wp.array(dtype=wp.float32),
    num_players: int,
    num_combinations: int,
    best_scores: wp.array(dtype=wp.int32),
    best_totals: wp.array(dtype=wp.int32),
):
    tid = wp.tid()
    if tid >= num_combinations:
        return
    # Each thread evaluates one weight combination
    offset = tid * NUM_WEIGHTS
    w0 = weights[offset + 0]
    w1 = weights[offset + 1]
    w2 = weights[offset + 2]
    w3 = weights[offset + 3]
    w4 = weights[offset + 4]
    w5 = weights[offset + 5]
    w6 = weights[offset + 6]
    # Evaluate correctness: group by date, pick best per TIMS group (1,2,3) per date
    correct = float(0.0)
    total = float(0.0)
    if num_players > 0:
        current_date = date[0]
        best_idx0 = float(-1.0)
        best_idx1 = float(-1.0)
        best_idx2 = float(-1.0)
        best_prob0 = float(-1e20)
        best_prob1 = float(-1e20)
        best_prob2 = float(-1e20)
        for i in range(num_players):
            if date[i] != current_date:
                # Process previous date
                if best_idx0 != -1.0:
                    if scored[int(best_idx0)] > 0.0:
                        correct = correct + 1.0
                    total = total + 1.0
                if best_idx1 != -1.0:
                    if scored[int(best_idx1)] > 0.0:
                        correct = correct + 1.0
                    total = total + 1.0
                if best_idx2 != -1.0:
                    if scored[int(best_idx2)] > 0.0:
                        correct = correct + 1.0
                    total = total + 1.0
                # Reset for new date
                best_idx0 = float(-1.0)
                best_idx1 = float(-1.0)
                best_idx2 = float(-1.0)
                best_prob0 = float(-1e20)
                best_prob1 = float(-1e20)
                best_prob2 = float(-1e20)
                current_date = date[i]
            # Compute probability for this player
            prob = (
                gpg[i] * w0
                + five_gpg[i] * w1
                + hgpg[i] * w2
                + tgpg[i] * w3
                + otga[i] * w4
                + hppg_otshga[i] * w5
                + is_home[i] * w6
            )
            t = tims[i]
            if t == 1:
                if prob > best_prob0:
                    best_prob0 = prob
                    best_idx0 = float(i)
            elif t == 2:
                if prob > best_prob1:
                    best_prob1 = prob
                    best_idx1 = float(i)
            elif t == 3:
                if prob > best_prob2:
                    best_prob2 = prob
                    best_idx2 = float(i)
        # Process the last date
        if best_idx0 != -1.0:
            if scored[int(best_idx0)] > 0.0:
                correct = correct + 1.0
            total = total + 1.0
        if best_idx1 != -1.0:
            if scored[int(best_idx1)] > 0.0:
                correct = correct + 1.0
            total = total + 1.0
        if best_idx2 != -1.0:
            if scored[int(best_idx2)] > 0.0:
                correct = correct + 1.0
            total = total + 1.0

    best_scores[tid] = int(correct)
    best_totals[tid] = int(total)


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
        all_players.append(row)
    return all_players


def normalize_stats(players, min_max):
    def norm(val, minv, maxv):
        return (val - minv) / (maxv - minv) if maxv > minv else 0.0

    for p in players:
        p["gpg"] = norm(p["gpg"], min_max["min_gpg"], min_max["max_gpg"])
        p["hgpg"] = norm(p["hgpg"], min_max["min_hgpg"], min_max["max_hgpg"])
        p["five_gpg"] = norm(p["five_gpg"], min_max["min_five_gpg"], min_max["max_five_gpg"])
        p["tgpg"] = norm(p.get("tgpg", 0.0), min_max["min_tgpg"], min_max["max_tgpg"])
        p["otga"] = norm(p.get("otga", 0.0), min_max["min_otga"], min_max["max_otga"])
        p["hppg"] = norm(p["hppg"], min_max["min_hppg"], min_max["max_hppg"])
        p["otshga"] = norm(p.get("otshga", 0.0), min_max["min_otshga"], min_max["max_otshga"])
        p["hppg_otshga"] = p["hppg"] * p["otshga"]


def generate_weight_combinations(step=STEP_SIZE):
    print("Generating weight permutations...")
    start_time = time.time()
    # weight_combinations = make_predictions_rust.generate_permutations(int(step * 100))
    weight_combinations = make_predictions_rust.generate_permutations(int(step * 100))
    print(f"Time to generate permutations: {time.time() - start_time:.2f} seconds")
    print(f"Testing {len(weight_combinations) // 7} weight combinations")

    # weight_combinations is now a flat list of floats, just reshape
    start_time = time.time()
    combos = np.array(weight_combinations, dtype=np.float32).reshape(-1, 7)
    print(f"Time to convert to numpy array: {time.time() - start_time:.2f} seconds")
    return combos


def call_warp_function(all_players):
    # ------------------------------
    # Prepare player data (unchanged)
    # ------------------------------
    filtered = [p for p in all_players if p["scored"] in {0.0, 1.0} and p["tims"] in {1, 2, 3}]
    filtered.sort(key=lambda p: p["date"])

    min_max = create_min_max_dict(get_min_max())
    normalize_stats(filtered, min_max)

    num_players = len(filtered)

    gpg = np.array([p["gpg"] for p in filtered], dtype=np.float32)
    hgpg = np.array([p["hgpg"] for p in filtered], dtype=np.float32)
    five_gpg = np.array([p["five_gpg"] for p in filtered], dtype=np.float32)
    tgpg = np.array([p.get("tgpg", 0.0) for p in filtered], dtype=np.float32)
    otga = np.array([p.get("otga", 0.0) for p in filtered], dtype=np.float32)
    hppg_otshga = np.array([p["hppg_otshga"] for p in filtered], dtype=np.float32)
    is_home = np.array([p.get("home", 0.0) for p in filtered], dtype=np.float32)
    scored = np.array([p.get("scored", 0.0) for p in filtered], dtype=np.float32)
    tims = np.array([int(p.get("tims", 0)) for p in filtered], dtype=np.int32)
    date = np.array([int(str(p["date"]).replace("-", "")) for p in filtered], dtype=np.int32)

    # ------------------------------
    # Move static data to GPU ONCE
    # ------------------------------
    device = wp.get_preferred_device()

    gpg_d = wp.array(gpg, dtype=wp.float32, device=device)
    hgpg_d = wp.array(hgpg, dtype=wp.float32, device=device)
    five_gpg_d = wp.array(five_gpg, dtype=wp.float32, device=device)
    tgpg_d = wp.array(tgpg, dtype=wp.float32, device=device)
    otga_d = wp.array(otga, dtype=wp.float32, device=device)
    hppg_otshga_d = wp.array(hppg_otshga, dtype=wp.float32, device=device)
    is_home_d = wp.array(is_home, dtype=wp.float32, device=device)
    scored_d = wp.array(scored, dtype=wp.float32, device=device)
    tims_d = wp.array(tims, dtype=wp.int32, device=device)
    date_d = wp.array(date, dtype=wp.int32, device=device)

    # ------------------------------
    # Initialize Rust generator
    # ------------------------------
    gen = make_predictions_rust.WeightGenerator(int(STEP_SIZE * 100))

    best_score = -1
    best_weights = None
    best_total = 0

    total_batches = 0
    start_all = time.time()

    while True:
        flat = gen.next_chunk(CHUNK_SIZE)
        if not flat:
            break

        combos = np.array(flat, dtype=np.float32).reshape(-1, 7)
        num_combos = combos.shape[0]

        weights_d = wp.array(combos.flatten(), dtype=wp.float32, device=device)
        best_scores_d = wp.zeros(num_combos, dtype=wp.int32, device=device)
        best_totals_d = wp.zeros(num_combos, dtype=wp.int32, device=device)

        wp.launch(
            kernel=evaluate_weights,
            dim=num_combos,
            inputs=[
                gpg_d,
                hgpg_d,
                five_gpg_d,
                tgpg_d,
                otga_d,
                hppg_otshga_d,
                is_home_d,
                scored_d,
                tims_d,
                date_d,
                weights_d,
                num_players,
                num_combos,
                best_scores_d,
                best_totals_d,
            ],
            device=device,
        )
        wp.synchronize()

        scores = best_scores_d.numpy()
        totals = best_totals_d.numpy()
        del weights_d, best_scores_d, best_totals_d
        wp.synchronize()

        idx = np.argmax(scores)
        if scores[idx] > best_score:
            best_score = scores[idx]
            best_total = totals[idx]
            best_weights = combos[idx].copy()

        total_batches += 1

    duration = time.time() - start_all

    print(f"\nProcessed {total_batches} batches")
    print(f"Total time: {duration:.2f}s")

    print("\nBest weights:")
    for l, v in zip(LABELS, best_weights):
        print(f"  {l}: {v:.3f}")

    print(f"\nMax correct: {best_score}")
    print(f"Total predictions: {best_total}")
    print(f"Accuracy: {best_score / best_total:.1%}")

    return best_weights, best_score


if __name__ == "__main__":
    all_players = get_players()
    best_weights, max_correct = call_warp_function(all_players)
