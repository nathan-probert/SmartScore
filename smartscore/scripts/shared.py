import csv
import os
import sys

import pandas as pd

# player_archive pulls in config.py, which builds Supabase clients at import
# time. These scripts already import service (same dependency chain), so the
# Supabase env vars have always been required to run them locally.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from player_archive import get_all_player_snapshots  # noqa: E402

PATH = "smartscore\\lib"
DATA_PATH = f"{PATH}\\data.csv"

# FEATURES = ["gpg", "hgpg", "five_gpg", "tgpg", "otga"]
FEATURES = ["gpg", "hgpg", "five_gpg", "tgpg", "otga", "hppg", "otshga", "home"]


def create_csv():
    # Archive rows key on player_id, not the positional id the Picks tables
    # assign. The columns below are unchanged, so FEATURES and the `scored`
    # label still land where they always did.
    data = get_all_player_snapshots()
    if not data:
        raise SystemExit("No player snapshots found; refusing to overwrite the training CSV with an empty one")

    # Union of every row's columns, in first-seen order. The archive pages by
    # date, and later dates carry columns earlier ones predate (the lineup and
    # opp_goalie_* fields), so a single row is no longer a safe source for the
    # header. Missing values are written as empty cells, which get_data turns
    # into NaN and drops, exactly as a missing field used to.
    all_fields = list(dict.fromkeys(field for entry in data for field in entry))

    os.makedirs(PATH, exist_ok=True)
    with open(DATA_PATH, "w+", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)


def get_data():
    # Comment this to skip ask about downloading data each time
    print("Do you want to download the data from the database? (y/n)")
    choice = input().split()[0].lower()
    if choice == "y":
        create_csv()

    data = pd.read_csv(DATA_PATH, encoding="utf-8", low_memory=False)

    # Clean the data
    for col in [col for col in data.columns if col not in ["date", "name"]]:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=FEATURES + ["scored"])
    labels = data["scored"].astype(int)

    # Display info about the data
    print()
    print(labels.value_counts())
    print(f"Ratio of goal scorers: {labels.mean():.2f}\n")

    return data, labels
