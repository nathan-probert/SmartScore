import json
import time
from datetime import timedelta

import boto3
import requests
from aws_lambda_powertools import Logger
from dateutil import parser

from config import ENV
from constants import SUPABASE_CLIENT

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
    response = exponential_backoff_request("https://api.hockeychallengehelper.com/api/picks?")
    allPlayers = response["playerLists"]

    ids = []
    for groupNum in range(3):
        ids.append([player["nhlPlayerId"] for player in allPlayers[groupNum]["players"]])

    return ids


def save_to_db(players):
    # remove fields that aren't currently show in frontend
    for i, player in enumerate(players):
        player.pop("home", None)
        player.pop("hppg", None)
        player.pop("otshga", None)
        player.pop("Scored", None)
        player["id"] = i + 1
    exponential_backoff_supabase_request(f"Picks-{ENV}", method="post", json_data=players)


def get_today_db():
    return exponential_backoff_supabase_request(f"Picks-{ENV}")


def get_historical_data():
    return exponential_backoff_supabase_request(f"Historic-Picks-{ENV}")


def update_historical_data(players):
    # remove fields that aren't currently show in frontend
    for i, player in enumerate(players):
        player.pop("home", None)
        player.pop("hppg", None)
        player.pop("otshga", None)
        player["id"] = i + 1
    exponential_backoff_supabase_request(f"Historic-Picks-{ENV}", method="post", json_data=players)


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


def exponential_backoff_request(url, method="get", data=None, json_data=None, max_retries=5, base_delay=1):
    """
    Makes HTTP requests with exponential backoff retry strategy.

    Args:
        url: URL to send the request to
        method: HTTP method ("get" or "post")
        data: Form data for POST requests
        json_data: JSON data for POST requests
        max_retries: Maximum number of retry attempts
        base_delay: Base delay between retries in seconds

    Returns:
        Parsed JSON response
    """
    method = method.lower()
    for attempt in range(max_retries):
        try:
            if method == "get":
                response = requests.get(url, timeout=5)
            elif method == "post":
                response = requests.post(url, data=data, json=json_data, timeout=5)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            wait_time = base_delay * (2**attempt)
            print(f"Attempt {attempt + 1} failed: {e}. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)
    raise Exception("Max retries reached. Request failed.")


def exponential_backoff_supabase_request(
    table_name, method="get", data=None, json_data=None, max_retries=5, base_delay=1
):
    """
    Makes Supabase requests with exponential backoff retry strategy.

    Args:
        table_name: Name of the Supabase table to query
        data: Form data for POST requests
        json_data: JSON data for POST requests
        max_retries: Maximum number of retry attempts
        base_delay: Base delay between retries in seconds

    Returns:
        Parsed JSON response
    """
    method = method.lower()
    for attempt in range(max_retries):
        try:
            if method == "get":
                response = (SUPABASE_CLIENT.table(table_name).select("*").execute()).data
            elif method == "post":
                # Clear the table before inserting new data
                SUPABASE_CLIENT.table(table_name).delete().neq("id", 0).execute()
                response = SUPABASE_CLIENT.table(table_name).upsert(json_data).execute()
                print(response)
            else:
                raise ValueError(f"Unsupported method: {method}")

            return response
        except Exception as e:
            wait_time = base_delay * (2**attempt)
            print(f"Attempt {attempt + 1} failed: {e}. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)
    raise Exception("Max retries reached. Request failed.")
