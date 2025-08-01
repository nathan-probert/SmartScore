from aws_lambda_powertools import Logger
from smartscore_info_client.schemas.player_info import PLAYER_INFO_SCHEMA, PlayerInfo
from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA, TeamInfo

from decorators import lambda_handler_error_responder
from service import (
    backfill_dates,
    check_db_for_date,
    choose_picks,
    get_date,
    get_players_from_team,
    get_teams,
    get_tims,
    get_todays_schedule,
    make_predictions_teams,
    publish_public_db,
    separate_players,
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
            - "completed" (bool): Whether data has already been retrieved.
            - "players" (list | None): Retrieved player data, if available.
    """
    entries = check_db_for_date()
    completed = False
    if entries:
        completed = True

    return {"statusCode": 200, "completed": completed, "players": entries}


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
    Checks if the data has been previously retrieved for the day.

    Args:
        event (dict): A dictionary of a single teams data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "players" (list): Retrieved player data for the given team.
    """
    team = TeamInfo(**event)

    logger.info(f"Getting players for team: {team.team_name}")
    players = get_players_from_team(team)
    logger.info(f"Found [{len(players)}] players for team")

    return PLAYER_INFO_SCHEMA.dump(players, many=True)


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

    # we only have completed property if this is not the first run
    initial_run = False if event.get("completed") else True

    return {
        "statusCode": 200,
        "date": get_date(),
        "players": players,
        "is_initial_run": initial_run,
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

    publish_public_db(entries)

    return {"statusCode": 200}


@lambda_handler_error_responder
def handle_parse_teams(event, context):
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

    write_historic_db(picks)

    return {"statusCode": 200, "players": players}
