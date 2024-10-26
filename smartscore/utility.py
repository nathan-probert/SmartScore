import ctypes
import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import boto3
import requests
from aws_lambda_powertools import Logger
from dateutil import parser

from config import ENV
from constants import DB_URL

logger = Logger()
lambda_client = boto3.client("lambda")
sts_client = boto3.client("sts")
events_client = boto3.client("events")


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
    response = requests.get("https://api.hockeychallengehelper.com/api/picks?", timeout=5)
    if response.status_code != HTTPStatus.OK:
        raise ValueError(f"Failed to get Tim's players: {response.text}")
    allPlayers = response.json()["playerLists"]

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
    response = requests.post(DB_URL, timeout=5, json=data)

    if response.status_code == HTTPStatus.OK:
        logger.info(
            "Successfully updated the database",
        )
    else:
        logger.info(f"Request failed with status code: {response.status_code}")
        raise ValueError(f"Failed to update the database: {response.text}")

    return response.status_code


def get_today_db():
    response = requests.get(DB_URL, timeout=5)

    if response.status_code == HTTPStatus.OK:
        logger.info(
            "Got today's players from the database",
        )
    else:
        logger.info(f"Request failed with status code: {response.status_code}")
        raise ValueError(f"Failed to access the database: {response.text}")

    return response.json()


def calculate_hours_to_set_endtime(invocation_time, buffer_hours=1):
    # Convert the invocation time from string to a timezone-aware datetime object
    invocation_datetime = datetime.fromisoformat(invocation_time.replace("Z", "+00:00"))

    # Use timezone-aware current time
    current_time = datetime.now(timezone.utc)

    # Calculate the expiration time based on the buffer
    expiration_time = invocation_datetime + timedelta(hours=buffer_hours)
    expiration_time = expiration_time.replace(tzinfo=timezone.utc)

    # Calculate how many hours until the expiration time
    hours_until_expiration = (expiration_time - current_time).total_seconds() // 3600

    return int(hours_until_expiration)


def create_cron_schedule(date_string):
    dt = date_string

    # AWS cron format: cron(Minutes Hours Day-of-Month Month Day-of-Week Year)
    cron_expression = f"cron({dt.minute} {dt.hour} {dt.day} {dt.month} ? {dt.year})"
    return cron_expression


def delete_expired_rules():
    # Create a CloudWatch Events client
    client = boto3.client("events")

    # Get the current time in UTC
    current_time = datetime.now(timezone.utc)

    # List all rules
    response = client.list_rules()

    for rule in response.get("Rules", []):
        # Check if the rule name matches the desired format
        # The format is TriggerStateMachineAt_YYYYMMDDHHMM
        if rule["Name"].startswith("TriggerStateMachineAt_"):
            # Extract the timestamp part from the rule name
            timestamp_str = rule["Name"][len("TriggerStateMachineAt_") :]

            # Attempt to parse the timestamp
            try:
                # Convert the timestamp string to a datetime object
                rule_time = datetime.strptime(timestamp_str, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)

                # Check if the rule's time has expired
                if rule_time < current_time:
                    print(f"Deleting expired rule: {rule['Name']}")
                    client.delete_rule(Name=rule["Name"])
            except ValueError:
                # If the timestamp format is incorrect, skip the rule
                print(f"Skipping rule with invalid timestamp format: {rule['Name']}")


def schedule_run(times):
    logger.info(f"Scheduling rule for given times: [{times}]")
    delete_expired_rules()

    for time_str in times:
        event_time = parser.parse(time_str)
        trigger_time = event_time + timedelta(minutes=5)
        cron_schedule = create_cron_schedule(trigger_time)

        rule_name = f"TriggerStateMachineAt_{trigger_time.strftime('%Y%m%d%H%M')}-{ENV}"

        events_client.put_rule(
            Name=rule_name,
            ScheduleExpression=cron_schedule,
            State="ENABLED",
        )

        session = boto3.session.Session()
        region = session.region_name
        account_id = sts_client.get_caller_identity()["Account"]

        sm_name = f"GetAllPlayersStateMachine-{ENV}"
        state_machine_arn = f"arn:aws:states:{region}:{account_id}:stateMachine:{sm_name}"
        events_client.put_targets(
            Rule=rule_name,
            Targets=[
                {
                    "Id": "1",
                    "Arn": state_machine_arn,
                    "RoleArn": "arn:aws:iam::242254217132:role/service-role/StepFunctions-EventBridge-ExecutionRole",
                    "Input": '{"source": "eventBridge"}',
                }
            ],
        )

        print(f"Scheduled event for {trigger_time} with rule name {rule_name}")


def remove_min_max_times(time_set):
    time_objects = set()
    for time_str in time_set:
        event_time = parser.parse(time_str)
        time_objects.add(event_time)

    # Find the minimum and maximum times
    min_time = min(time_objects)
    max_time = max(time_objects)

    # Remove min and max times from the original set
    time_objects.discard(min_time)
    time_objects.discard(max_time)

    # Return the updated set
    return {time.isoformat() for time in time_objects}
