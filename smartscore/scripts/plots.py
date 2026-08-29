#!/usr/bin/env python3

import os
import sys
from collections import defaultdict

import matplotlib.pyplot as plt
import numpy as np

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import make_predictions_rust
from shared import get_data

from service import get_min_max


# -----------------------------
# Helpers
# -----------------------------
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
    data, _ = get_data()

    players = []
    for _, row in data.iterrows():
        if row["tims"] not in {0, 1, 2, 3}:
            row["tims"] = 0.0
        players.append(
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

    return [p for p in players if p.scored in {0.0, 1.0}]


# -----------------------------
# Core
# -----------------------------
def main():
    weights = make_predictions_rust.Weights(
        gpg=0.190,
        five_gpg=0.060,
        hgpg=0.600,
        tgpg=0.110,
        otga=0.040,
        hppg_otshga=0.000,
        is_home=0.000,
    )

    players = get_players()

    min_max = create_min_max_dict(get_min_max())
    min_max_obj = make_predictions_rust.MinMax(**min_max)

    probs = make_predictions_rust.predict(players, min_max_obj, weights)

    y_true = np.array([p.scored for p in players])
    y_prob = np.array(probs)

    # -----------------------------
    # 1. Calibration curve
    # -----------------------------
    bins = np.linspace(0, 1, 11)
    bin_idx = np.digitize(y_prob, bins) - 1
    bin_idx = np.clip(bin_idx, 0, 9)

    bin_totals = np.bincount(bin_idx, minlength=10)
    bin_correct = np.bincount(bin_idx, weights=y_true, minlength=10)

    actual = np.divide(bin_correct, bin_totals, out=np.zeros_like(bin_correct), where=bin_totals != 0)
    predicted = (bins[:-1] + bins[1:]) / 2

    plt.figure()
    plt.plot([0, 1], [0, 1], linestyle="--")
    plt.plot(predicted, actual, marker="o")
    plt.title("Calibration Curve")
    plt.xlabel("Predicted")
    plt.ylabel("Actual")
    plt.grid()

    # table output
    print("Calibration Table:")
    print("Bin Range\tPredicted\tActual\tCount")
    for i in range(len(bins) - 1):
        print(f"{bins[i]:.2f}-{bins[i+1]:.2f}\t{predicted[i]:.2f}\t{actual[i]:.2f}\t{bin_totals[i]}")

    # -----------------------------
    # 2–4. Threshold metrics
    # -----------------------------
    thresholds = np.linspace(0, 1, 50)

    acc = []
    prec = []
    rec = []

    for t in thresholds:
        preds = (y_prob >= t).astype(int)

        tp = np.sum((preds == 1) & (y_true == 1))
        fp = np.sum((preds == 1) & (y_true == 0))
        tn = np.sum((preds == 0) & (y_true == 0))
        fn = np.sum((preds == 0) & (y_true == 1))

        acc.append((tp + tn) / len(y_true))
        prec.append(tp / (tp + fp) if (tp + fp) > 0 else 0)
        rec.append(tp / (tp + fn) if (tp + fn) > 0 else 0)

    plt.figure()
    plt.plot(thresholds, acc)
    plt.title("Accuracy vs Threshold")
    plt.xlabel("Threshold")
    plt.ylabel("Accuracy")
    plt.grid()

    plt.figure()
    plt.plot(thresholds, prec)
    plt.title("Precision vs Threshold")
    plt.xlabel("Threshold")
    plt.ylabel("Precision")
    plt.grid()

    plt.figure()
    plt.plot(thresholds, rec)
    plt.title("Recall vs Threshold")
    plt.xlabel("Threshold")
    plt.ylabel("Recall")
    plt.grid()

    # -----------------------------
    # 5. Top-N per day (VERY important)
    # -----------------------------
    grouped = defaultdict(list)
    for p, prob in zip(players, y_prob):
        grouped[p.date].append((p, prob))

    top1_acc = []
    top3_acc = []

    for date, items in grouped.items():
        items.sort(key=lambda x: x[1], reverse=True)

        top1 = items[:1]
        top3 = items[:3]

        top1_acc.append(np.mean([p.scored for p, _ in top1]))
        top3_acc.append(np.mean([p.scored for p, _ in top3]))

    plt.figure()
    plt.plot(top1_acc, label="Top 1")
    plt.plot(top3_acc, label="Top 3")
    plt.title("Top-N Accuracy per Day")
    plt.xlabel("Game Day Index")
    plt.ylabel("Hit Rate")
    plt.legend()
    plt.grid()

    # -----------------------------
    # 6. Per-tims group accuracy
    # -----------------------------
    tims_stats = defaultdict(lambda: {"correct": 0, "total": 0})

    for p, prob in zip(players, y_prob):
        group = p.tims
        tims_stats[group]["total"] += 1
        if p.scored == 1.0:
            tims_stats[group]["correct"] += 1

    groups = sorted(tims_stats.keys())
    accs = [tims_stats[g]["correct"] / tims_stats[g]["total"] if tims_stats[g]["total"] > 0 else 0 for g in groups]

    plt.figure()
    plt.bar([str(g) for g in groups], accs)
    plt.title("Accuracy by TIMS Group")
    plt.xlabel("TIMS Group")
    plt.ylabel("Accuracy")
    plt.grid()

    # -----------------------------
    # Show all
    # -----------------------------
    plt.show()


if __name__ == "__main__":
    main()
