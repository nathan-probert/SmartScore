from aws_lambda_powertools import Logger
from smartscore_info_client.models.team import GameTeam
from smartscore_info_client.schemas.player import PLAYER_INFO_SCHEMA
from smartscore_info_client.schemas.team import TEAM_INFO_SCHEMA

from decorators import lambda_handler_error_responder
from nhl_lineups import get_nhl_com_lineups, get_rotowire_lineups
from player_archive import save_player_snapshots
from service import (
    backfill_dates,
    build_goalies_with_team_id,
    calculate_metrics,
    calculate_season_metrics,
    check_db_for_date,
    choose_picks,
    denormalize_players_for_db,
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

    Requires the relational shape (``players`` + ``teams``, joined on
    numeric ``team_id``) and preserves ``teams``/``goalies`` for downstream
    steps so Step Functions state stays lean.

    Args:
        event (dict): A dictionary of all player data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "players" (list): Player data, now including stat (and beta stat).
            - "teams" (list, optional): Passed through when present.
    """
    players = make_predictions_teams(event.get("players"), event.get("teams"))

    output = {"statusCode": 200, "players": players}
    if "teams" in event:
        output["teams"] = event["teams"]
    if "goalies" in event:
        output["goalies"] = event["goalies"]
    return output


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
            - "teams"/"goalies" (list, optional): Passed through when present.
    """
    players = event.get("players")
    players = get_tims(players)

    output = {
        "statusCode": 200,
        "date": get_date(),
        "players": players,
        "status": event.get("status", "first_run"),
    }
    if "teams" in event:
        output["teams"] = event["teams"]
    if "goalies" in event:
        output["goalies"] = event["goalies"]
    return output


@lambda_handler_error_responder
def handle_publish_db(event, context):
    """
    Publishes the player data to the public database.

    Joins relational ``players`` + ``teams`` + ``goalies`` into full rows
    for the write only; the state itself stays lean.

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

    if event.get("teams") or event.get("goalies"):
        entries = denormalize_players_for_db(entries, event.get("teams"), event.get("goalies"))

    publish_public_db(entries)

    return {"statusCode": 200}


@lambda_handler_error_responder
def handle_save_players(event, context):
    """
    Archives a batch of players to the Supabase Player-Snapshots table
    (Step Functions SaveToDb).

    Upserts on (date, player_id), so re-running the pipeline for a date
    refreshes that date's rows in place instead of appending a duplicate roster.

    Joins relational ``players`` + ``teams`` + ``goalies`` into full rows
    for the upload only; the returned state stays lean so downstream steps
    stay under the Step Functions size limit.

    Args:
        event (dict): A dictionary containing:
            - "players" (list): Player data for today.
            - "date" (str): The date the players are for, applied to each row.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing:
            - "statusCode" (int): HTTP status code.
            - "players" (list): Lean player data, passed through so the UpdateHistory
              state keeps working on the same payload.
    """

    players = event.get("players") or []
    date = event.get("date")

    if players:
        if event.get("teams") or event.get("goalies"):
            upload_rows = denormalize_players_for_db(players, event.get("teams"), event.get("goalies"))
        else:
            upload_rows = players
        save_player_snapshots(upload_rows, date=date)

    output = {"statusCode": 200, "players": players}
    if "teams" in event:
        output["teams"] = event["teams"]
    if "goalies" in event:
        output["goalies"] = event["goalies"]
    return output


@lambda_handler_error_responder
def handle_parse_teams(event, context):
    # Handles the case when there are no games today
    if event == []:
        return {"players": [], "teams": []}

    return merge_players_and_teams(event)


@lambda_handler_error_responder
def handle_save_historic_db(event, context):
    """
    Saves the player data to the historic database.

    Picks are chosen from lean players (stat + tims only), then joined with
    ``teams``/``goalies`` for the historic write so stored rows keep the
    full denormalized columns.
    """

    players = event.get("players")
    teams = event.get("teams")
    goalies = event.get("goalies")
    picks = choose_picks(players)

    if picks and (teams or goalies):
        picks = denormalize_players_for_db(picks, teams, goalies)

    yesterday_results = write_historic_db(picks)

    new_metrics = calculate_metrics(yesterday_results)
    update_metrics(new_metrics)

    # Season flow runs alongside lifetime; lifetime is left untouched.
    season_id = resolve_season_id(yesterday_results)
    new_season_metrics = calculate_season_metrics(yesterday_results, season_id)
    update_season_metrics(new_season_metrics, season_id)

    output = {"statusCode": 200, "players": players}
    if teams is not None:
        output["teams"] = teams
    if goalies is not None:
        output["goalies"] = goalies
    return output


@lambda_handler_error_responder
def handle_get_injuries(event, context):
    """
    Scrape current injury data from RotoWire.

    Injury fields stay denormalized per skater (2 small fields); ``teams``
    and ``goalies`` pass through untouched.

    Args:
        event (dict): A dictionary containing player data.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing injury data.
    """
    players = event.get("players", [])

    injuries = get_injury_data()
    merged_info = merge_injury_data(players, injuries)

    output = {
        "statusCode": 200,
        "players": merged_info,
    }
    if "teams" in event:
        output["teams"] = event["teams"]
    if "goalies" in event:
        output["goalies"] = event["goalies"]
    return output


@lambda_handler_error_responder
def handle_get_goalies(event, context):
    """
    Fetch starting goalies from RotoWire, enrich with NHL stats, and keep
    them relational (hard cutover: no legacy denormalized fallback).

    Starters are returned as a separate ``goalies`` list keyed by numeric
    ``team_id`` instead of duplicating ~10 ``opp_goalie_*`` fields onto every
    skater -- that duplication is what exceeded the Step Functions 256KB
    state limit. The opponent join (via ``teams[].opponent_id``) happens only
    inside the final DB lambdas. No NHL schedule fetch is needed here.

    Args:
        event (dict): A dictionary containing relational players + teams.
        context (dict): Unused Lambda context.

    Returns:
        dict: players (lean) + teams + goalies.
    """
    players = event.get("players", [])
    teams = event.get("teams", [])

    starters = enrich_starting_goalies()
    goalies = build_goalies_with_team_id(starters, teams)
    return {
        "statusCode": 200,
        "players": players,
        "teams": teams,
        "goalies": goalies,
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

    def _passthrough(payload_players):
        output = {"statusCode": 200, "players": payload_players}
        if "teams" in event:
            output["teams"] = event["teams"]
        if "goalies" in event:
            output["goalies"] = event["goalies"]
        return output

    if not players:
        return _passthrough(players)

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
        return _passthrough(mark_lineups_unknown(players))

    return _passthrough(merged_info)


@lambda_handler_error_responder
def handle_emails(event, context):
    """
    Sends out emails to users with their smartscore picks.

    Picks are read here rather than carried in the Step Functions state.
    NotifyUsers' first state projects CheckCompleted's result down to ``status``
    alone (see templates/notify_users.asl.json): that result is the whole
    Picks-prod roster, which exceeds the 256KB state limit now that each skater
    row carries the opp_goalie_* and lineup columns, and ``choose_picks``
    reduces it to a handful of rows regardless.

    Args:
        event (dict): A dictionary containing the run status. ``players`` is
            honoured for direct invocation, but NotifyUsers does not send it.
        context (dict): Unused Lambda context.

    Returns:
        dict: A dictionary containing status code.
    """
    picks = choose_picks(event.get("players") or check_db_for_date())

    users = get_all_emails()  # Now returns list of dicts with email and display_name
    for user in users:
        logger.info(f"Sending email to {user['email']} (Display name: {user.get('display_name', '')})")

    send_emails(users, picks)

    return {
        "statusCode": 200,
    }
