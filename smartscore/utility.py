import ctypes
import json
from http import HTTPStatus

import boto3
import requests
from aws_lambda_powertools import Logger

logger = Logger()
lambda_client = boto3.client("lambda")
sts_client = boto3.client("sts")


class PlayerInfoC(ctypes.Structure):
    _fields_ = [
        ("gpg", ctypes.c_float),
        ("hgpg", ctypes.c_float),
        ("five_gpg", ctypes.c_float),
        ("tgpg", ctypes.c_float),
        ("otga", ctypes.c_float),
    ]


class MinMaxC(ctypes.Structure):
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
    ]


def create_player_info_array(players):
    PlayerArrayC = PlayerInfoC * len(players)
    player_array = PlayerArrayC()

    for i, player in enumerate(players):
        player_array[i].gpg = player.get("gpg")
        player_array[i].hgpg = player.get("hgpg")
        player_array[i].five_gpg = player.get("five_gpg")
        player_array[i].tgpg = player.get("tgpg")
        player_array[i].otga = player.get("otga")

    return player_array


def create_min_max(min_max):
    min_max_c = MinMaxC()
    min_max_c.min_gpg = min_max.get("gpg", {}).get("min")
    min_max_c.max_gpg = min_max.get("gpg", {}).get("max")
    min_max_c.min_hgpg = min_max.get("hgpg", {}).get("min")
    min_max_c.max_hgpg = min_max.get("hgpg", {}).get("max")
    min_max_c.min_five_gpg = min_max.get("five_gpg", {}).get("min")
    min_max_c.max_five_gpg = min_max.get("five_gpg", {}).get("max")
    min_max_c.min_tgpg = min_max.get("tgpg", {}).get("min")
    min_max_c.max_tgpg = min_max.get("tgpg", {}).get("max")
    min_max_c.min_otga = min_max.get("otga", {}).get("min")
    min_max_c.max_otga = min_max.get("otga", {}).get("max")

    return min_max_c


def c_predict(c_players, min_max):
    player_array = create_player_info_array(c_players)
    size = len(c_players)

    ProbabilitiesC = ctypes.c_float * size
    probabilities = ProbabilitiesC()

    min_max_c = create_min_max(min_max)

    players_lib = ctypes.CDLL("./compiled_code.so")
    players_lib.process_players(player_array, size, min_max_c, probabilities)

    return probabilities


def invoke_lambda(function_name, payload):
    session = boto3.session.Session()
    region = session.region_name
    account_id = sts_client.get_caller_identity()["Account"]

    function_arn = f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"
    response = lambda_client.invoke(
        FunctionName=function_arn, InvocationType="RequestResponse", Payload=json.dumps(payload)
    )
    response_payload = json.loads(response["Payload"].read())
    return response_payload


def get_tims_players():
    response = requests.get("https://api.hockeychallengehelper.com/api/picks?", timeout=5).json()
    allPlayers = response["playerLists"]

    ids = []
    for groupNum in range(3):
        ids.append([player["nhlPlayerId"] for player in allPlayers[groupNum]["players"]])

    return ids


def adjust_name(df_name):
    name_replacements = {
        "Cam": "Cameron",
        "J.J. Moser": "Janis Moser",
        "Pat Maroon": "Patrick Maroon",
        "T.J. Brodie": "TJ Brodie",
    }
    for old_name, new_name in name_replacements.items():
        df_name = df_name.replace(old_name, new_name)

    return df_name


def link_odds(players, player_infos):
    player_table = {player.name: player for player in players}
    for player_info in player_infos:
        if player_table.get(player_info["name"]):
            player_table[player_info["name"]].set_odds(player_info["odds"])
        else:
            # retry with adjustments
            player_info["name"] = adjust_name(player_info["name"])
            if player_table.get(player_info["name"]):
                player_table[player_info["name"]].set_odds(player_info["odds"])
            else:
                print(f"Player {player_info['name']} not found in player list")


def save_to_db(players):
    data = {"items": players}
    response = requests.post("https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/players", timeout=5, json=data)

    if response.status_code == HTTPStatus.OK:
        logger.info(
            "Successfully updated the database",
        )
    else:
        logger.info(f"Request failed with status code: {response.status_code}")
        raise ValueError(f"Failed to update the database: {response.text}")

    return response.status_code
