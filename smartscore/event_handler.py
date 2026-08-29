from aws_lambda_powertools import Logger
from smartscore_info_client.schemas.player_info import PLAYER_INFO_SCHEMA, PlayerInfo
from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA, TeamInfo

from decorators import lambda_handler_error_responder
from service import (
    backfill_dates,
    calculate_metrics,
    check_db_for_date,
    choose_picks,
    get_all_emails,
    get_date,
    get_injury_data,
    get_players_from_team,
    get_teams,
    get_tims,
    get_todays_schedule,
    make_predictions_teams,
    merge_injury_data,
    publish_public_db,
    send_emails,
    separate_players,
    update_metrics,
    write_historic_db,
)

logger = Logger()


@lambda_handler_error_responder
def handle_backfill(event, context):
    """
    Backfills the database with who scored for each game

    Args:
        event (dict): Unused event data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
    """
    backfill_dates()

    return {
        "statusCode": 200,
    }


@lambda_handler_error_responder
def handle_check_completed(event, context):
    """
    Checks if the data has been previously retrieved for the day.

    Args:
        event (dict): Unused event data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "status" (str): The current status of data retrieval.
            - "players" (list | None): Retrieved player data, if available.
    """

    entries = check_db_for_date()
    if event.get("last_game"):
        status = "last_run"
    elif entries:
        status = "normal_run"
    else:
        status = "first_run"

    return {"statusCode": 200, "status": status, "players": entries}


@lambda_handler_error_responder
def handle_get_teams(event, context):
    """
    Gets a list of all the teams playing today.

    Args:
        event (dict): Unused event data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "teams" (list): Retrieved team data.
    """
    data = get_todays_schedule()

    teams = get_teams(data)
    logger.info(f"Found [{len(teams)}] teams")

    return {"statusCode": 200, "teams": TEAM_INFO_SCHEMA.dump(teams, many=True)}


@lambda_handler_error_responder
def handle_get_players_from_team(event, context):
    """
    Gets players for a team and returns the complete team structure with players.

    Args:
        event (dict): A dictionary of a single teams data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing team information and players:
            - "team_name" (str): Team name.
            - "team_abbr" (str): Team abbreviation.
            - "season" (str): Season identifier.
            - "team_id" (int): Team ID.
            - "opponent_id" (int): Opponent team ID.
            - "home" (bool): Whether team is playing at home.
            - "players" (list): Retrieved player data for the given team.
    """
    team = TeamInfo(**event)

    logger.info(f"Getting players for team: {team.team_name}")
    players = get_players_from_team(team)
    logger.info(f"Found [{len(players)}] players for team")

    return {
        "team_name": event.get("team_name"),
        "team_abbr": event.get("team_abbr"),
        "season": event.get("season"),
        "team_id": event.get("team_id"),
        "opponent_id": event.get("opponent_id"),
        "home": event.get("home"),
        "players": PLAYER_INFO_SCHEMA.dump(players, many=True),
    }


@lambda_handler_error_responder
def handle_make_predictions(event, context):
    """
    Makes predictions for the given players.

    Args:
        event (dict): A dictionary of all player data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "players" (list): Player data, now including stat (and beta stat).
    """
    players = make_predictions_teams(event.get("players"))

    return {"statusCode": 200, "players": players}


@lambda_handler_error_responder
def handle_get_tims(event, context):
    """
    Retrieve Tim Horton's data for today.

    Args:
        event (dict): A dictionary of all player data.
            - Optional["completed"] (bool): Whether data has already been retrieved.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "date" (str): The current date.
            - "players" (list): Player data, now including tims.
            - "is_initial_run" (bool): Whether this is the first run of the day.
    """
    players = event.get("players")
    players = get_tims(players)

    return {
        "statusCode": 200,
        "date": get_date(),
        "players": players,
        "status": event.get("status", "first_run"),
    }


@lambda_handler_error_responder
def handle_publish_db(event, context):
    """
    Publishes the player data to the public database.

    Args:
        event (dict): A dictionary of all player data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "players" (list): Player data, now including stat (and beta stat).
    """

    entries = event.get("players")
    if not entries:
        entries = []

    publish_public_db(entries)

    return {"statusCode": 200}


@lambda_handler_error_responder
def handle_parse_teams(event, context):
    # Handles the case when there are no games today
    if event == []:
        return []

    players = [PlayerInfo(**player) for team in event for player in team.pop("players")]
    teams = [TeamInfo(**team) for team in event]

    all_players = separate_players(players, teams)

    return all_players


@lambda_handler_error_responder
def handle_save_historic_db(event, context):
    """
    Saves the player data to the historic database.
    """

    players = event.get("players")
    picks = choose_picks(players)

    yesterday_results = write_historic_db(picks)

    new_metrics = calculate_metrics(yesterday_results)
    update_metrics(new_metrics)

    return {"statusCode": 200, "players": players}


@lambda_handler_error_responder
def handle_get_injuries(event, context):
    """
    Scrape current injury data from RotoWire.

    Args:
        event (dict): A dictionary containing player data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing injury data.
    """
    players = event.get("players", [])

    injuries = get_injury_data()
    merged_info = merge_injury_data(players, injuries)

    return {
        "statusCode": 200,
        "players": merged_info,
    }


@lambda_handler_error_responder
def handle_emails(event, context):
    """
    Sends out emails to users with their smartscore picks.

    Args:
        event (dict): A dictionary containing player data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing status code.
    """
    picks = choose_picks(event.get("players", []))

    users = get_all_emails()  # Now returns list of dicts with email and display_name
    for user in users:
        logger.info(f"Sending email to {user['email']} (Display name: {user.get('display_name', '')})")

    send_emails(users, picks)

    return {
        "statusCode": 200,
    }
