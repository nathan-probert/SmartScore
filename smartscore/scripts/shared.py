import csv
import os
import sys

import pandas as pd

# cloudflare_client pulls in config.py, which builds Supabase clients at import
# time. These scripts already import service (same dependency chain), so the
# Supabase env vars have always been required to run them locally.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cloudflare_client import get_all_players  # noqa: E402

PATH = "smartscore\\lib"
DATA_PATH = f"{PATH}\\data.csv"

# FEATURES = ["gpg", "hgpg", "five_gpg", "tgpg", "otga"]
FEATURES = ["gpg", "hgpg", "five_gpg", "tgpg", "otga", "hppg", "otshga", "home"]


def create_csv():
    data = get_all_players()

    # Get the fields from the last entry (which should have all fields), set missing fields to None
    all_fields = data[-1].keys()
    for entry in data:
        for field in all_fields:
            entry.setdefault(field, None)

    os.makedirs(PATH, exist_ok=True)
    with open(DATA_PATH, "w+", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields)
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
