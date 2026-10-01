import json
import time
from datetime import date as _date
from datetime import timedelta

import boto3
from aws_lambda_powertools import Logger
from dateutil import parser
from postgrest.exceptions import APIError
from smartscore_info_client.utility import exponential_backoff_request

from config import ENV, SUPABASE_ADMIN_AUTH_CLIENT, SUPABASE_CLIENT
from constants import CURRENT_PICK_ACCURACY, SEASON_CUTOFF_MONTH, SEASON_PICK_ACCURACY_PREFIX

logger = Logger()


_boto3_clients = {}


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
        if rule["Name"].startswith("TriggerStateMachineAt_") and rule["Name"].endswith(f"-{ENV}"):
            targets = client.list_targets_by_rule(Rule=rule["Name"]).get("Targets", [])
            if targets:
                target_ids = [target["Id"] for target in targets]
                client.remove_targets(Rule=rule["Name"], Ids=target_ids)

            client.delete_rule(Name=rule["Name"])


def schedule_run(times):
    logger.info(f"Scheduling rule for given times: [{times}]")
    delete_expired_rules()

    times = sorted(times)
    for idx, time_str in enumerate(times):
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

        # Add "last_game": True to the last rule's input
        input_payload = {
            "source": "eventBridge",
            "last_game": True if idx == len(times) - 1 else False,
        }

        get_events_client().put_targets(
            Rule=rule_name,
            Targets=[
                {
                    "Id": "1",
                    "Arn": state_machine_arn,
                    "RoleArn": role_arn,
                    "Input": json.dumps(input_payload),
                }
            ],
        )

        print(f"Scheduled event for {trigger_time} with rule name {rule_name}")


def exponential_backoff_supabase_request(
    table_name, method="get", data=None, json_data=None, max_retries=5, base_delay=1, select="*", eq=None
):
    """
    Makes Supabase requests with exponential backoff retry strategy.

    Args:
        table_name: Name of the Supabase table to query
        data: Form data for POST requests
        json_data: JSON data for POST requests
        max_retries: Maximum number of retry attempts
        base_delay: Base delay between retries in seconds
        select: Columns to select in GET requests

    Returns:
        Parsed JSON response
    """
    method = method.upper()

    logger.info(f"Making {method} request to table: {table_name} with data: {json_data} select: {select} eq: {eq}")
    for attempt in range(max_retries):
        try:
            if method == "GET":
                query = SUPABASE_CLIENT.table(table_name).select(select)
                if eq is not None:
                    # eq should be a tuple: (column, value)
                    col, val = eq
                    query = query.eq(col, val)
                response = query.execute().data
            elif method == "POST":
                # Scoped delete when eq is given (e.g. Metrics rows share a table),
                # otherwise legacy full-table wipe (e.g. Picks / Historic-Picks).
                if eq is not None:
                    col, val = eq
                    SUPABASE_CLIENT.table(table_name).delete().eq(col, val).execute()
                else:
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


def get_season_id(date_str=None):
    """Derive NHL season id (e.g. "20252026") from a YYYY-MM-DD date.

    Season spans Oct-June: Aug-Dec -> f"{year}{year+1}", Jan-Jul -> f"{year-1}{year}".
    Defaults to today when no date is given.
    """
    if date_str is None:
        today = _date.today()
        year, month = today.year, today.month
    else:
        parts = str(date_str).split("-")
        year, month = int(parts[0]), int(parts[1])
    if month >= SEASON_CUTOFF_MONTH:
        return f"{year}{year + 1}"
    return f"{year - 1}{year}"


def get_season_metric_id(season_id):
    return f"{SEASON_PICK_ACCURACY_PREFIX}{season_id}"


def get_metric_by_id(metric_id):
    response = exponential_backoff_supabase_request(
        f"Metrics-{ENV}",
        method="get",
        eq=("id", metric_id),
    )
    if not response:
        return

    return {
        "value": response[0].get("value", 0.0),
        "correct": response[0].get("correct", 0),
        "total": response[0].get("total", 0),
    }


def get_cur_pick_pct(metric_id=CURRENT_PICK_ACCURACY):
    return get_metric_by_id(metric_id)


def get_season_pick_pct(season_id):
    return get_metric_by_id(get_season_metric_id(season_id))


def upload_metrics(metrics) -> None:
    metrics["id"] = CURRENT_PICK_ACCURACY
    exponential_backoff_supabase_request(
        f"Metrics-{ENV}", method="post", json_data=metrics, eq=("id", CURRENT_PICK_ACCURACY)
    )


def upload_season_metrics(metrics, season_id) -> None:
    metric_id = get_season_metric_id(season_id)
    metrics["id"] = metric_id
    exponential_backoff_supabase_request(f"Metrics-{ENV}", method="post", json_data=metrics, eq=("id", metric_id))


def get_emails():
    """
    Fetches emails for users who have opted in to notifications.

    Implements the canonical query via the get_opted_in_emails() database function:
        SELECT u.email
        FROM public.user_preferences p
        JOIN auth.users u ON u.id = p.user_id
        WHERE p.notify = true AND u.email IS NOT NULL

    Returns a list of emails for users who want notifications.
    """

    try:
        response = SUPABASE_ADMIN_AUTH_CLIENT.rpc("get_opted_in_emails").execute()
        users = [
            {"email": row["email"], "display_name": row.get("Display_name", "")}
            for row in response.data
            if row.get("email")
        ]
        logger.info(f"Found {len(users)} users with notifications enabled")
        return users
    except Exception as e:  # noqa: BLE001 - a failure fetching emails must not crash the send-emails pipeline
        logger.error(f"Failed to fetch opted-in emails: {e.__class__.__name__}: {e}")
        return []
