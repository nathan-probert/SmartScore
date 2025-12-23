import json
import time
from datetime import timedelta

import boto3
import requests
from aws_lambda_powertools import Logger
from dateutil import parser
from postgrest.exceptions import APIError

from config import ENV, SUPABASE_CLIENT

logger = Logger()


_boto3_clients = {}


def get_lambda_client():
    if "lambda" not in _boto3_clients:
        _boto3_clients["lambda"] = boto3.client("lambda")
    return _boto3_clients["lambda"]


def get_sts_client():
    if "sts" not in _boto3_clients:
        _boto3_clients["sts"] = boto3.client("sts")
    return _boto3_clients["sts"]


def get_events_client():
    if "events" not in _boto3_clients:
        _boto3_clients["events"] = boto3.client("events")
    return _boto3_clients["events"]


def get_ssm_client():
    if "ssm" not in _boto3_clients:
        _boto3_clients["ssm"] = boto3.client("ssm")
    return _boto3_clients["ssm"]


def invoke_lambda(function_name, payload, wait=True):
    session = boto3.session.Session()
    region = session.region_name
    account_id = get_sts_client().get_caller_identity()["Account"]
    invocation_type = "RequestResponse" if wait else "Event"

    function_arn = f"arn:aws:lambda:{region}:{account_id}:function:{function_name}"
    response = get_lambda_client().invoke(
        FunctionName=function_arn, InvocationType=invocation_type, Payload=json.dumps(payload)
    )
    if wait:
        response_payload = json.loads(response["Payload"].read())
        return response_payload
    return None


def get_tims_players():
    headers = {
        "Origin": "https://hockeychallengehelper.com",
        "Referer": "https://hockeychallengehelper.com/",
        "User-Agent": "Mozilla/5.0",
    }
    response = exponential_backoff_request("https://api.hockeychallengehelper.com/api/picks?", headers=headers)
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

        get_events_client().put_rule(
            Name=rule_name,
            ScheduleExpression=cron_schedule,
            State="ENABLED",
        )

        session = boto3.session.Session()
        region = session.region_name
        account_id = get_sts_client().get_caller_identity()["Account"]

        sm_name = f"PlayerProcessingPipeline-{ENV}"
        state_machine_arn = f"arn:aws:states:{region}:{account_id}:stateMachine:{sm_name}"

        parameter = get_ssm_client().get_parameter(Name=f"/event_bridge_role/arn/{ENV}")
        role_arn = parameter["Parameter"]["Value"]

        get_events_client().put_targets(
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


def exponential_backoff_request(
    url, method="get", data=None, json_data=None, headers=None, max_retries=5, base_delay=1
):
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
                response = requests.get(url, headers=headers, timeout=10)
            elif method == "post":
                response = requests.post(url, data=data, json=json_data, headers=headers, timeout=10)
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
    method = method.upper()

    logger.info(f"Making {method} request to table: {table_name} with data: {json_data}")
    for attempt in range(max_retries):
        try:
            if method == "GET":
                response = (SUPABASE_CLIENT.table(table_name).select("*").execute()).data
            elif method == "POST":
                # Clear the table before inserting new data
                SUPABASE_CLIENT.table(table_name).delete().neq("id", 0).execute()
                if json_data is not None and len(json_data) > 0:
                    response = SUPABASE_CLIENT.table(table_name).upsert(json_data).execute()
                else:
                    logger.info(f"json_data is empty or None, skipping upsert for table: {table_name}")
                    response = None
            else:
                raise ValueError(f"Unsupported method: {method}")

            return response
        except ValueError as ve:
            logger.error(f"ValueError encountered: {ve}. Not retrying.")
            raise ve
        except APIError as api_error:
            logger.error(f"APIError encountered: {api_error}. Not retrying.")
            raise api_error
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Exception type: {type(e)}, Exception: {e}"
            )  # temporary logging, once we see a retryable error, we can remove this
            wait_time = base_delay * (2**attempt)
            logger.info(f"Attempt {attempt + 1} failed. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    raise Exception("Max retries reached. Request failed.")


def adjust_name(df_name):
    name_replacements = {
        "Cam": "Cameron",
        "J.J. Moser": "Janis Moser",
        "Pat Maroon": "Patrick Maroon",
        "T.J. Brodie": "TJ Brodie",
        "Mitchell Marner": "Mitch Marner",
        "Alex Wennberg": "Alexander Wennberg",
        "Tim Stuetzle": "Tim Stutzle",
        "Zach Aston-Reese": "Zachary Aston-Reese",
        "Nicholas Paul": "Nick Paul",
        "Matt Dumba": "Mathew Dumba",
        "Alex Kerfoot": "Alexander Kerfoot",
        "Josh Mahura": "Joshua Mahura",
        "Elias-Nils Pettersson": "Elias Pettersson",
    }
    for old_name, new_name in name_replacements.items():
        df_name = df_name.replace(old_name, new_name)

    return df_name
