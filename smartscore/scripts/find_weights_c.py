import ctypes
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from shared import get_data

from service import get_min_max  # noqa: E402

# Load the compiled C library
lib_path = os.path.join(os.path.dirname(__file__), "..", "compiled_code.so")
c_lib = ctypes.CDLL(lib_path)


# Define C structures matching the header.h
class PlayerInfo(ctypes.Structure):
    _fields_ = [
        ("gpg", ctypes.c_float),
        ("hgpg", ctypes.c_float),
        ("five_gpg", ctypes.c_float),
        ("tgpg", ctypes.c_float),
        ("otga", ctypes.c_float),
        ("hppg", ctypes.c_float),
        ("otshga", ctypes.c_float),
        ("is_home", ctypes.c_float),
        ("hppg_otshga", ctypes.c_float),
    ]


class TestingPlayerInfo(ctypes.Structure):
    _fields_ = [
        ("gpg", ctypes.c_float),
        ("hgpg", ctypes.c_float),
        ("five_gpg", ctypes.c_float),
        ("tgpg", ctypes.c_float),
        ("otga", ctypes.c_float),
        ("hppg", ctypes.c_float),
        ("otshga", ctypes.c_float),
        ("is_home", ctypes.c_float),
        ("hppg_otshga", ctypes.c_float),
        ("scored", ctypes.c_float),
        ("tims", ctypes.c_float),
        ("date", ctypes.c_char_p),
    ]


class MinMax(ctypes.Structure):
    _fields_ = [
        ("min_gpg", ctypes.c_float),
        ("max_gpg", ctypes.c_float),
        ("min_hgpg", ctypes.c_float),
        ("max_hgpg", ctypes.c_float),
        ("min_five_gpg", ctypes.c_float),
        ("max_five_gpg", ctypes.c_float),
        ("min_tgpg", ctypes.c_float),
        ("max_tgpg", ctypes.c_float),
        ("min_otga", ctypes.c_float),
        ("max_otga", ctypes.c_float),
        ("min_hppg", ctypes.c_float),
        ("max_hppg", ctypes.c_float),
        ("min_otshga", ctypes.c_float),
        ("max_otshga", ctypes.c_float),
    ]


class Weights(ctypes.Structure):
    _fields_ = [
        ("gpg", ctypes.c_float),
        ("hgpg", ctypes.c_float),
        ("five_gpg", ctypes.c_float),
        ("tgpg", ctypes.c_float),
        ("otga", ctypes.c_float),
        ("is_home", ctypes.c_float),
        ("hppg_otshga", ctypes.c_float),
    ]


# Define function signatures
c_lib.test_weights.argtypes = [
    ctypes.POINTER(TestingPlayerInfo),
    ctypes.c_int,
    MinMax,
    ctypes.POINTER(ctypes.c_float),
    ctypes.c_int,
]
c_lib.test_weights.restype = None


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
            TestingPlayerInfo(
                gpg=row["gpg"],
                hgpg=row["hgpg"],
                five_gpg=row["five_gpg"],
                tgpg=row.get("tgpg", 0.0),
                otga=row.get("otga", 0.0),
                hppg=row["hppg"],
                otshga=row.get("otshga", 0.0),
                is_home=row.get("home", 0.0),
                hppg_otshga=0.0,  # Will be calculated in C
                scored=row.get("scored", 0.0),
                tims=row.get("tims", 0.0),
                date=row["date"].encode("utf-8"),  # Convert to bytes
            )
        )
    return all_players


def call_c_function(all_players):
    # Filter players who have scoring data and were tims picks
    filtered_players = [player for player in all_players if player.scored in {0.0, 1.0} and player.tims in {1, 2, 3}]

    # Sort players by date to ensure proper grouping
    filtered_players.sort(key=lambda p: p.date.decode("utf-8"))

    # Create min_max object
    min_max_dict = create_min_max_dict(get_min_max())
    min_max = MinMax(**min_max_dict)

    # Count unique dates with TIMS picks
    unique_dates = set()
    for player in filtered_players:
        unique_dates.add(player.date.decode("utf-8"))
    num_tims_dates = len(unique_dates)

    # Prepare arrays for C function
    num_players = len(filtered_players)
    players_array = (TestingPlayerInfo * num_players)(*filtered_players)
    probabilities = (ctypes.c_float * num_players)()

    print("Testing weights using C implementation")

    # Call C function
    start_time_c = time.time()
    c_lib.test_weights(players_array, num_players, min_max, probabilities, num_tims_dates)
    c_duration = time.time() - start_time_c

    print(f"C function took {c_duration:.2f} seconds")


if __name__ == "__main__":
    all_players = get_players()
    call_c_function(all_players)
