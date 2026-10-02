from aws_lambda_powertools import Logger
from smartscore_info_client.models.team import GameTeam
from smartscore_info_client.schemas.player import PLAYER_INFO_SCHEMA
from smartscore_info_client.schemas.team import TEAM_INFO_SCHEMA

from decorators import lambda_handler_error_responder
from nhl_lineups import get_nhl_com_lineups, get_rotowire_lineups
from player_archive import save_player_snapshots
from service import (
    backfill_dates,
    calculate_metrics,
    calculate_season_metrics,
    check_db_for_date,
    choose_picks,
    enrich_starting_goalies,
    enrich_teams,
    get_all_emails,
    get_date,
    get_injury_data,
    get_players_from_team,
    get_teams,
    get_tims,
    get_todays_schedule,
    make_predictions_teams,
    mark_lineups_unknown,
    merge_goalie_data,
    merge_injury_data,
    merge_lineup_data,
    merge_players_and_teams,
    publish_public_db,
    resolve_season_id,
    send_emails,
    update_metrics,
    update_season_metrics,
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

    teams = enrich_teams(get_teams(data))
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
    team = GameTeam.from_mapping(event)

    logger.info(f"Getting players for team: {team.team_name}")
    players = get_players_from_team(team)
    logger.info(f"Found [{len(players)}] players for team")

    output = {key: event[key] for key in TEAM_INFO_SCHEMA.fields if key in event}
    output["players"] = PLAYER_INFO_SCHEMA.dump(players, many=True)

    return output


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
def handle_save_players(event, context):
    """
    Archives a batch of players to the Supabase Player-Snapshots table
    (Step Functions SaveToDb).

    Upserts on (date, player_id), so re-running the pipeline for a date
    refreshes that date's rows in place instead of appending a duplicate roster.

    Args:
        event (dict): A dictionary containing:
            - "players" (list): Player data for today.
            - "date" (str): The date the players are for, applied to each row.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "players" (list): Player data, passed through so the UpdateHistory
              state keeps working on the same payload.
    """

    players = event.get("players") or []
    date = event.get("date")

    if players:
        save_player_snapshots(players, date=date)

    return {"statusCode": 200, "players": players}


@lambda_handler_error_responder
def handle_parse_teams(event, context):
    # Handles the case when there are no games today
    if event == []:
        return []

    return merge_players_and_teams(event)


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

    # Season flow runs alongside lifetime; lifetime is left untouched.
    season_id = resolve_season_id(yesterday_results)
    new_season_metrics = calculate_season_metrics(yesterday_results, season_id)
    update_season_metrics(new_season_metrics, season_id)

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
def handle_get_goalies(event, context):
    """
    Fetch starting goalies from RotoWire, enrich with NHL stats, and merge the
    opposing starter into each skater.

    Args:
        event (dict): A dictionary containing player data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing player data with opp_goalie_* fields.
    """
    players = event.get("players", [])

    try:
        schedule_data = get_todays_schedule()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error fetching schedule for goalie merge: {e}")
        return {
            "statusCode": 200,
            "players": players,
        }

    starters = enrich_starting_goalies()
    merged_info = merge_goalie_data(players, starters, schedule_data)

    return {
        "statusCode": 200,
        "players": merged_info,
    }


@lambda_handler_error_responder
def handle_get_lineups(event, context):
    """
    Fetch projected starting lineups and merge them into each skater.

    Forward lines and defence pairs come from the NHL.com daily projections
    article; power play units come from the RotoWire lineups page. Both sources
    are optional here: if either fails the other still contributes, and if both
    fail the players are returned with lineup_status UNKNOWN rather than failing
    the pipeline.

    Args:
        event (dict): A dictionary containing player data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing player data with lineup fields.
    """
    players = event.get("players", [])
    if not players:
        return {"statusCode": 200, "players": players}

    # Each source is fetched independently so one being down still lets the other
    # contribute. Any unexpected failure here degrades to lineup_status UNKNOWN
    # rather than failing the whole pipeline step, which would block the picks.
    try:
        nhl_games = get_nhl_com_lineups()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error fetching NHL.com lineups: {e}")
        nhl_games = []

    try:
        rotowire_games = get_rotowire_lineups()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error fetching RotoWire lineups: {e}")
        rotowire_games = []

    if not nhl_games and not rotowire_games:
        logger.error("Both lineup sources returned no games; skipping lineup merge")

    try:
        merged_info = merge_lineup_data(players, nhl_games, rotowire_games)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error merging lineup data: {e}")
        return {
            "statusCode": 200,
            "players": mark_lineups_unknown(players),
        }

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
