import ctypes
import json

import boto3
import requests
from aws_lambda_powertools import Logger


logger = Logger()
lambda_client = boto3.client("lambda")
sts_client = boto3.client('sts')


class PlayerInfoC(ctypes.Structure):
  _fields_ = [
    ("gpg", ctypes.c_float),
    ("hgpg", ctypes.c_float),
    ("five_gpg", ctypes.c_float),
    ("tgpg", ctypes.c_float),
    ("otga", ctypes.c_float),
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


def c_predict(teams, players, all_players, date):
    c_players = []
    team_table = {teams.team_id: teams for teams in teams}
    for player in players:
        c_players.append(
            {
                "gpg": player.gpg,
                "hgpg": player.hgpg,
                "five_gpg": player.five_gpg,
                "tgpg": team_table[player.team_id].tgpg,
                "otga": team_table[player.team_id].otga,
            }
        )

    player_array = create_player_info_array(c_players)
    all_player_array = create_player_info_array(all_players)

    ProbabilitiesC = ctypes.c_float * len(players)
    probabilities = ProbabilitiesC()

    players_lib = ctypes.CDLL("./compiled_code.so")
    players_lib.process_players(player_array, len(players), all_player_array, len(all_players), probabilities)

    for i, player in enumerate(players):
        player.set_stat(probabilities[i])

    return players


def invoke_lambda(function_name, payload):
    session = boto3.session.Session()
    region = session.region_name
    account_id = sts_client.get_caller_identity()['Account']

    function_arn = f'arn:aws:lambda:{region}:{account_id}:function:{function_name}'
    response = lambda_client.invoke(
        FunctionName=function_arn, InvocationType="RequestResponse", Payload=json.dumps(payload)
    )
    response_payload = json.loads(response["Payload"].read())
    return response_payload


def get_tims_players():
    response = requests.get("https://api.hockeychallengehelper.com/api/picks?").json()
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
    response = requests.post("https://x8ki-letl-twmt.n7.xano.io/api:OvqrJ0Ps/players", json=data)

    if response.status_code == 200:
        logger.info("Successfully updated the database",)
    else:
        logger.info("Request failed with status code: ", response.status_code)
        raise ValueError(f"Failed to update the database: {response.text}")

    return response.status_code
