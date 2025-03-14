import ctypes
import json
from datetime import timedelta
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
ssm_client = boto3.client("ssm")


def invoke_lambda(function_name, payload, wait=True):
    session = boto3.session.Session()
    region = session.region_name
    account_id = sts_client.get_caller_identity()["Account"]
    invocation_type = "RequestResponse" if wait else "Event"

    function_arn = f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"
    response = lambda_client.invoke(
        FunctionName=function_arn, InvocationType=invocation_type, Payload=json.dumps(payload)
    )
    if wait:
        response_payload = json.loads(response["Payload"].read())
        return response_payload
    return None


def get_tims_players():
    response = requests.get("https://api.hockeychallengehelper.com/api/picks?", timeout=5)
    if response.status_code != HTTPStatus.OK:
        logger.info(f"Request failed with status code: {response.status_code}")
        return []
    allPlayers = response.json()["playerLists"]

    ids = []
    for groupNum in range(3):
        ids.append([player["nhlPlayerId"] for player in allPlayers[groupNum]["players"]])

    return ids


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

    if response.status_code != HTTPStatus.OK:
        logger.info(f"Request failed with status code: {response.status_code}")
        raise ValueError(f"Failed to access the database: {response.text}")

    return response.json()


def create_cron_schedule(date_string):
    dt = date_string

    # AWS cron format: cron(Minutes Hours Day-of-Month Month Day-of-Week Year)
    cron_expression = f"cron({dt.minute} {dt.hour} {dt.day} {dt.month} ? {dt.year})"
    return cron_expression


def delete_expired_rules():
    client = boto3.client("events")
    response = client.list_rules()

    for rule in response.get("Rules", []):
        # Remove if the rule name matches TriggerStateMachineAt_YYYYMMDDHHMM
        if rule["Name"].startswith("TriggerStateMachineAt_"):
            targets = client.list_targets_by_rule(Rule=rule["Name"]).get("Targets", [])
            if targets:
                target_ids = [target["Id"] for target in targets]
                client.remove_targets(Rule=rule["Name"], Ids=target_ids)

            client.delete_rule(Name=rule["Name"])


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

        parameter = ssm_client.get_parameter(Name=f"/event_bridge_role/arn/{ENV}")
        role_arn = parameter["Parameter"]["Value"]

        events_client.put_targets(
            Rule=rule_name,
            Targets=[
                {
                    "Id": "1",
                    "Arn": state_machine_arn,
                    "RoleArn": role_arn,
                    "Input": '{"source": "eventBridge"}',
                }
            ],
        )

        print(f"Scheduled event for {trigger_time} with rule name {rule_name}")


def remove_last_game(time_set):
    time_objects = set()
    for time_str in time_set:
        event_time = parser.parse(time_str)
        time_objects.add(event_time)

    time_objects.discard(max(time_objects))

    return {time.isoformat() for time in time_objects}
