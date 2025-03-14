from aws_lambda_powertools import Logger
from smartscore_info_client.schemas.player_info import PLAYER_INFO_SCHEMA, PlayerInfo
from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA, TeamInfo

from decorators import lambda_handler_error_responder
from service import (
    backfill_dates,
    check_db_for_date,
    get_date,
    get_players_from_team,
    get_teams,
    get_tims,
    get_todays_schedule,
    make_predictions_teams,
    publish_public_db,
    separate_players,
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

    return {
        "statusCode": 200,
        "players": all_players,
    }


if __name__ == "__main__":
    input = {
  "statusCode": 200,
  "players": [
    {
      "name": "Jonatan Berggren",
      "id": 8481013,
      "gpg": 0.16393442622950818,
      "hgpg": 0.19285714285714287,
      "five_gpg": 0.2,
      "hppg": 0.05714285714285714,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "J.T. Compher",
      "id": 8477456,
      "gpg": 0.1206896551724138,
      "hgpg": 0.19642857142857142,
      "five_gpg": 0,
      "hppg": 0.0625,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Andrew Copp",
      "id": 8477429,
      "gpg": 0.17857142857142858,
      "hgpg": 0.14746543778801843,
      "five_gpg": 0.2,
      "hppg": 0.009216589861751152,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Alex DeBrincat",
      "id": 8479337,
      "gpg": 0.453125,
      "hgpg": 0.36403508771929827,
      "five_gpg": 0.4,
      "hppg": 0.14473684210526316,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Patrick Kane",
      "id": 8474141,
      "gpg": 0.2777777777777778,
      "hgpg": 0.30978260869565216,
      "five_gpg": 0.2,
      "hppg": 0.07608695652173914,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Marco Kasper",
      "id": 8483464,
      "gpg": 0.1694915254237288,
      "hgpg": 0.16666666666666666,
      "five_gpg": 0,
      "hppg": 0.03333333333333333,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Dylan Larkin",
      "id": 8477946,
      "gpg": 0.421875,
      "hgpg": 0.4339622641509434,
      "five_gpg": 0.6,
      "hppg": 0.2028301886792453,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Carter Mazur",
      "id": 8482802,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Tyler Motte",
      "id": 8477353,
      "gpg": 0.05405405405405406,
      "hgpg": 0.09444444444444444,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Michael Rasmussen",
      "id": 8479992,
      "gpg": 0.15254237288135594,
      "hgpg": 0.16842105263157894,
      "five_gpg": 0.2,
      "hppg": 0.010526315789473684,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Lucas Raymond",
      "id": 8482078,
      "gpg": 0.34375,
      "hgpg": 0.3181818181818182,
      "five_gpg": 0,
      "hppg": 0.07727272727272727,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Dominik Shine",
      "id": 8479942,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Craig Smith",
      "id": 8475225,
      "gpg": 0.21951219512195122,
      "hgpg": 0.14948453608247422,
      "five_gpg": 0.2,
      "hppg": 0.010309278350515464,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Elmer Soderblom",
      "id": 8481725,
      "gpg": 0.16666666666666666,
      "hgpg": 0.20512820512820512,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Vladimir Tarasenko",
      "id": 8475765,
      "gpg": 0.12903225806451613,
      "hgpg": 0.23949579831932774,
      "five_gpg": 0,
      "hppg": 0.037815126050420166,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Ben Chiarot",
      "id": 8475279,
      "gpg": 0.047619047619047616,
      "hgpg": 0.06018518518518518,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Simon Edvinsson",
      "id": 8482762,
      "gpg": 0.11666666666666667,
      "hgpg": 0.11764705882352941,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Erik Gustafsson",
      "id": 8476979,
      "gpg": 0.03571428571428571,
      "hgpg": 0.07272727272727272,
      "five_gpg": 0,
      "hppg": 0.004545454545454545,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Justin Holl",
      "id": 8475718,
      "gpg": 0.017857142857142856,
      "hgpg": 0.016483516483516484,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Albert Johansson",
      "id": 8481607,
      "gpg": 0.023255813953488372,
      "hgpg": 0.023255813953488372,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "William Lagesson",
      "id": 8478021,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Jeff Petry",
      "id": 8473507,
      "gpg": 0.029411764705882353,
      "hgpg": 0.05357142857142857,
      "five_gpg": 0,
      "hppg": 0.011904761904761904,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Moritz Seider",
      "id": 8481542,
      "gpg": 0.09375,
      "hgpg": 0.08771929824561403,
      "five_gpg": 0.2,
      "hppg": 0.02631578947368421,
      "team_name": "Detroit",
      "tgpg": 2.79687,
      "otga": 3.47619,
      "otshga": 0.6984126984126984,
      "home": True
    },
    {
      "name": "Zach Benson",
      "id": 8484145,
      "gpg": 0.15789473684210525,
      "hgpg": 0.15625,
      "five_gpg": 0,
      "hppg": 0.015625,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Josh Dunne",
      "id": 8482623,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Jordan Greenway",
      "id": 8478413,
      "gpg": 0.10714285714285714,
      "hgpg": 0.12101910828025478,
      "five_gpg": 0,
      "hppg": 0.006369426751592357,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Peyton Krebs",
      "id": 8481522,
      "gpg": 0.08064516129032258,
      "hgpg": 0.08333333333333333,
      "five_gpg": 0.2,
      "hppg": 0.004629629629629629,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Jiri Kulich",
      "id": 8483468,
      "gpg": 0.2553191489361702,
      "hgpg": 0.25,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Sam Lafferty",
      "id": 8478043,
      "gpg": 0.044444444444444446,
      "hgpg": 0.1308411214953271,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Beck Malenstyn",
      "id": 8479359,
      "gpg": 0.06779661016949153,
      "hgpg": 0.0718954248366013,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Ryan McLeod",
      "id": 8480802,
      "gpg": 0.23333333333333334,
      "hgpg": 0.1752136752136752,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Josh Norris",
      "id": 8480064,
      "gpg": 0.36363636363636365,
      "hgpg": 0.336283185840708,
      "five_gpg": 0.2,
      "hppg": 0.09734513274336283,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "JJ Peterka",
      "id": 8482175,
      "gpg": 0.3114754098360656,
      "hgpg": 0.2681818181818182,
      "five_gpg": 0.4,
      "hppg": 0.04090909090909091,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Jack Quinn",
      "id": 8482097,
      "gpg": 0.17857142857142858,
      "hgpg": 0.2088607594936709,
      "five_gpg": 0,
      "hppg": 0.02531645569620253,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Isak Rosen",
      "id": 8482765,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Tage Thompson",
      "id": 8479420,
      "gpg": 0.5789473684210527,
      "hgpg": 0.529126213592233,
      "five_gpg": 1,
      "hppg": 0.1650485436893204,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Alex Tuch",
      "id": 8477949,
      "gpg": 0.3968253968253968,
      "hgpg": 0.3915094339622642,
      "five_gpg": 0.6,
      "hppg": 0.05188679245283019,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Jason Zucker",
      "id": 8475722,
      "gpg": 0.32727272727272727,
      "hgpg": 0.28846153846153844,
      "five_gpg": 0,
      "hppg": 0.07692307692307693,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Jacob Bernard-Docker",
      "id": 8480879,
      "gpg": 0.04,
      "hgpg": 0.04310344827586207,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Jacob Bryson",
      "id": 8480196,
      "gpg": 0,
      "hgpg": 0.014925373134328358,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Bowen Byram",
      "id": 8481524,
      "gpg": 0.1111111111111111,
      "hgpg": 0.15135135135135136,
      "five_gpg": 0,
      "hppg": 0.010810810810810811,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Connor Clifton",
      "id": 8477365,
      "gpg": 0,
      "hgpg": 0.04205607476635514,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Rasmus Dahlin",
      "id": 8480839,
      "gpg": 0.2,
      "hgpg": 0.21495327102803738,
      "five_gpg": 0.4,
      "hppg": 0.07476635514018691,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Owen Power",
      "id": 8482671,
      "gpg": 0.09523809523809523,
      "hgpg": 0.07339449541284404,
      "five_gpg": 0,
      "hppg": 0.0045871559633027525,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Mattias Samuelsson",
      "id": 8480807,
      "gpg": 0.06976744186046512,
      "hgpg": 0.04316546762589928,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Buffalo",
      "tgpg": 3.12698,
      "otga": 3.1875,
      "otshga": 0.71875,
      "home": False
    },
    {
      "name": "Mikael Backlund",
      "id": 8474150,
      "gpg": 0.1746031746031746,
      "hgpg": 0.19823788546255505,
      "five_gpg": 0.2,
      "hppg": 0.03524229074889868,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Blake Coleman",
      "id": 8476399,
      "gpg": 0.19047619047619047,
      "hgpg": 0.26905829596412556,
      "five_gpg": 0,
      "hppg": 0.026905829596412557,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Matt Coronato",
      "id": 8482679,
      "gpg": 0.27586206896551724,
      "hgpg": 0.20430107526881722,
      "five_gpg": 0.2,
      "hppg": 0.043010752688172046,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Joel Farabee",
      "id": 8480797,
      "gpg": 0.1746031746031746,
      "hgpg": 0.21145374449339208,
      "five_gpg": 0.4,
      "hppg": 0.00881057268722467,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Morgan Frost",
      "id": 8480028,
      "gpg": 0.20967741935483872,
      "hgpg": 0.2102803738317757,
      "five_gpg": 0,
      "hppg": 0.02336448598130841,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Jonathan Huberdeau",
      "id": 8476456,
      "gpg": 0.3492063492063492,
      "hgpg": 0.21973094170403587,
      "five_gpg": 0,
      "hppg": 0.06726457399103139,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Nazem Kadri",
      "id": 8475172,
      "gpg": 0.3492063492063492,
      "hgpg": 0.3303964757709251,
      "five_gpg": 0.4,
      "hppg": 0.1145374449339207,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Justin Kirkland",
      "id": 8477993,
      "gpg": 0.09523809523809523,
      "hgpg": 0.06666666666666667,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Ryan Lomberg",
      "id": 8479066,
      "gpg": 0.01639344262295082,
      "hgpg": 0.0794979079497908,
      "five_gpg": 0,
      "hppg": 0.0041841004184100415,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Anthony Mantha",
      "id": 8477511,
      "gpg": 0.3076923076923077,
      "hgpg": 0.24203821656050956,
      "five_gpg": 0.4,
      "hppg": 0.03184713375796178,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Martin Pospisil",
      "id": 8481028,
      "gpg": 0.06349206349206349,
      "hgpg": 0.09523809523809523,
      "five_gpg": 0,
      "hppg": 0.007936507936507936,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Kevin Rooney",
      "id": 8479291,
      "gpg": 0.07692307692307693,
      "hgpg": 0.06862745098039216,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Yegor Sharangovich",
      "id": 8481068,
      "gpg": 0.21818181818181817,
      "hgpg": 0.26046511627906976,
      "five_gpg": 0.2,
      "hppg": 0.05116279069767442,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Connor Zary",
      "id": 8482074,
      "gpg": 0.25,
      "hgpg": 0.23423423423423423,
      "five_gpg": 0.4,
      "hppg": 0.018018018018018018,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Rasmus Andersson",
      "id": 8478397,
      "gpg": 0.12698412698412698,
      "hgpg": 0.12727272727272726,
      "five_gpg": 0,
      "hppg": 0.01818181818181818,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Kevin Bahl",
      "id": 8480860,
      "gpg": 0.037037037037037035,
      "hgpg": 0.026455026455026454,
      "five_gpg": 0,
      "hppg": 0.005291005291005291,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Jake Bean",
      "id": 8479402,
      "gpg": 0.043478260869565216,
      "hgpg": 0.05303030303030303,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Joel Hanley",
      "id": 8477810,
      "gpg": 0.02857142857142857,
      "hgpg": 0.017241379310344827,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Daniil Miromanov",
      "id": 8482624,
      "gpg": 0.02702702702702703,
      "hgpg": 0.08,
      "five_gpg": 0,
      "hppg": 0.013333333333333334,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Brayden Pachal",
      "id": 8481167,
      "gpg": 0.03225806451612903,
      "hgpg": 0.032520325203252036,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Ilya Solovyov",
      "id": 8482470,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "MacKenzie Weegar",
      "id": 8477346,
      "gpg": 0.1111111111111111,
      "hgpg": 0.13716814159292035,
      "five_gpg": 0.2,
      "hppg": 0.030973451327433628,
      "team_name": "Calgary",
      "tgpg": 2.55555,
      "otga": 3.03125,
      "otshga": 0.5,
      "home": True
    },
    {
      "name": "Nils Aman",
      "id": 8482496,
      "gpg": 0,
      "hgpg": 0.056451612903225805,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Teddy Blueger",
      "id": 8476927,
      "gpg": 0.109375,
      "hgpg": 0.08411214953271028,
      "five_gpg": 0.4,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Brock Boeser",
      "id": 8478444,
      "gpg": 0.3157894736842105,
      "hgpg": 0.3705357142857143,
      "five_gpg": 0,
      "hppg": 0.13392857142857142,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Filip Chytil",
      "id": 8480078,
      "gpg": 0.24074074074074073,
      "hgpg": 0.23841059602649006,
      "five_gpg": 0.2,
      "hppg": 0.026490066225165563,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Jake DeBrusk",
      "id": 8478498,
      "gpg": 0.34375,
      "hgpg": 0.33771929824561403,
      "five_gpg": 0.2,
      "hppg": 0.09649122807017543,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Conor Garland",
      "id": 8478856,
      "gpg": 0.25,
      "hgpg": 0.23333333333333334,
      "five_gpg": 0,
      "hppg": 0.0375,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Nils Hoglander",
      "id": 8481535,
      "gpg": 0.08333333333333333,
      "hgpg": 0.1875,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Dakota Joshua",
      "id": 8478057,
      "gpg": 0.10256410256410256,
      "hgpg": 0.19072164948453607,
      "five_gpg": 0.2,
      "hppg": 0.015463917525773196,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Jonathan Lekkerimaki",
      "id": 8483476,
      "gpg": 0.13333333333333333,
      "hgpg": 0.13333333333333333,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Drew O'Connor",
      "id": 8482055,
      "gpg": 0.12121212121212122,
      "hgpg": 0.1518324607329843,
      "five_gpg": 0,
      "hppg": 0.005235602094240838,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Elias Pettersson",
      "id": 8480012,
      "gpg": 0.22413793103448276,
      "hgpg": 0.37339055793991416,
      "five_gpg": 0.4,
      "hppg": 0.11587982832618025,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Kiefer Sherwood",
      "id": 8480748,
      "gpg": 0.23333333333333334,
      "hgpg": 0.1927710843373494,
      "five_gpg": 0.2,
      "hppg": 0.006024096385542169,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Pius Suter",
      "id": 8480459,
      "gpg": 0.25396825396825395,
      "hgpg": 0.2072072072072072,
      "five_gpg": 0.2,
      "hppg": 0.018018018018018018,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Derek Forbort",
      "id": 8475762,
      "gpg": 0.02631578947368421,
      "hgpg": 0.043795620437956206,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Filip Hronek",
      "id": 8479425,
      "gpg": 0.09302325581395349,
      "hgpg": 0.0945273631840796,
      "five_gpg": 0.2,
      "hppg": 0.029850746268656716,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Quinn Hughes",
      "id": 8480800,
      "gpg": 0.28,
      "hgpg": 0.17040358744394618,
      "five_gpg": 0,
      "hppg": 0.04484304932735426,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Noah Juulsen",
      "id": 8478454,
      "gpg": 0,
      "hgpg": 0.009708737864077669,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Victor Mancini",
      "id": 8483768,
      "gpg": 0.05263157894736842,
      "hgpg": 0.05263157894736842,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Tyler Myers",
      "id": 8474574,
      "gpg": 0.06557377049180328,
      "hgpg": 0.043859649122807015,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Elias Pettersson",
      "id": 8483678,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Marcus Pettersson",
      "id": 8477969,
      "gpg": 0.05,
      "hgpg": 0.0380952380952381,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Vancouver",
      "tgpg": 2.70312,
      "otga": 2.87301,
      "otshga": 0.746031746031746,
      "home": False
    },
    {
      "name": "Nick Bjugstad",
      "id": 8475760,
      "gpg": 0.09433962264150944,
      "hgpg": 0.2146118721461187,
      "five_gpg": 0,
      "hppg": 0.0182648401826484,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Michael Carcone",
      "id": 8479619,
      "gpg": 0.13636363636363635,
      "hgpg": 0.2283464566929134,
      "five_gpg": 0.2,
      "hppg": 0.007874015748031496,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Logan Cooley",
      "id": 8483431,
      "gpg": 0.2982456140350877,
      "hgpg": 0.26618705035971224,
      "five_gpg": 0.2,
      "hppg": 0.07194244604316546,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Lawson Crouse",
      "id": 8478474,
      "gpg": 0.15873015873015872,
      "hgpg": 0.2579185520361991,
      "five_gpg": 0.2,
      "hppg": 0.06334841628959276,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Josh Doan",
      "id": 8482659,
      "gpg": 0.12121212121212122,
      "hgpg": 0.20454545454545456,
      "five_gpg": 0,
      "hppg": 0.045454545454545456,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Dylan Guenther",
      "id": 8482699,
      "gpg": 0.4423076923076923,
      "hgpg": 0.36153846153846153,
      "five_gpg": 0.4,
      "hppg": 0.17692307692307693,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Barrett Hayton",
      "id": 8480849,
      "gpg": 0.265625,
      "hgpg": 0.21787709497206703,
      "five_gpg": 0.4,
      "hppg": 0.055865921787709494,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Clayton Keller",
      "id": 8479343,
      "gpg": 0.36507936507936506,
      "hgpg": 0.4170403587443946,
      "five_gpg": 0.4,
      "hppg": 0.09865470852017937,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Alexander Kerfoot",
      "id": 8477021,
      "gpg": 0.109375,
      "hgpg": 0.13389121338912133,
      "five_gpg": 0,
      "hppg": 0.02092050209205021,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Matias Maccelli",
      "id": 8481711,
      "gpg": 0.15384615384615385,
      "hgpg": 0.18181818181818182,
      "five_gpg": 0,
      "hppg": 0.015151515151515152,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Jack McBain",
      "id": 8480855,
      "gpg": 0.171875,
      "hgpg": 0.14553990610328638,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Liam O'Brien",
      "id": 8477070,
      "gpg": 0,
      "hgpg": 0.05128205128205128,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Nick Schmaltz",
      "id": 8477951,
      "gpg": 0.234375,
      "hgpg": 0.28640776699029125,
      "five_gpg": 0.8,
      "hppg": 0.10679611650485436,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Kevin Stenlund",
      "id": 8478831,
      "gpg": 0.125,
      "hgpg": 0.11403508771929824,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Robert Bortuzzo",
      "id": 8474145,
      "gpg": 0,
      "hgpg": 0.02197802197802198,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Ian Cole",
      "id": 8474013,
      "gpg": 0.015625,
      "hgpg": 0.029288702928870293,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Nick DeSimone",
      "id": 8480084,
      "gpg": 0,
      "hgpg": 0.038461538461538464,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Michael Kesselring",
      "id": 8480891,
      "gpg": 0.09375,
      "hgpg": 0.07971014492753623,
      "five_gpg": 0,
      "hppg": 0.007246376811594203,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "John Marino",
      "id": 8478507,
      "gpg": 0.045454545454545456,
      "hgpg": 0.05202312138728324,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Olli Määttä",
      "id": 8476874,
      "gpg": 0.03278688524590164,
      "hgpg": 0.05687203791469194,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Mikhail Sergachev",
      "id": 8479410,
      "gpg": 0.1864406779661017,
      "hgpg": 0.13333333333333333,
      "five_gpg": 0.2,
      "hppg": 0.03333333333333333,
      "team_name": "Utah",
      "tgpg": 2.82812,
      "otga": 3.09375,
      "otshga": 0.734375,
      "home": True
    },
    {
      "name": "Leo Carlsson",
      "id": 8484153,
      "gpg": 0.2413793103448276,
      "hgpg": 0.23008849557522124,
      "five_gpg": 0.2,
      "hppg": 0.061946902654867256,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Sam Colangelo",
      "id": 8482118,
      "gpg": 0.3157894736842105,
      "hgpg": 0.3181818181818182,
      "five_gpg": 1,
      "hppg": 0.045454545454545456,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Robby Fabbri",
      "id": 8477952,
      "gpg": 0.18181818181818182,
      "hgpg": 0.2357142857142857,
      "five_gpg": 0,
      "hppg": 0.05714285714285714,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Cutter Gauthier",
      "id": 8483445,
      "gpg": 0.1875,
      "hgpg": 0.18461538461538463,
      "five_gpg": 0,
      "hppg": 0.03076923076923077,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Jansen Harkins",
      "id": 8478424,
      "gpg": 0.0425531914893617,
      "hgpg": 0.043859649122807015,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Ross Johnston",
      "id": 8477527,
      "gpg": 0.023809523809523808,
      "hgpg": 0.015873015873015872,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Alex Killorn",
      "id": 8473986,
      "gpg": 0.203125,
      "hgpg": 0.2837209302325581,
      "five_gpg": 0.4,
      "hppg": 0.04186046511627907,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Brett Leason",
      "id": 8481517,
      "gpg": 0.09433962264150944,
      "hgpg": 0.12571428571428572,
      "five_gpg": 0,
      "hppg": 0.005714285714285714,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Isac Lundestrom",
      "id": 8480806,
      "gpg": 0.06557377049180328,
      "hgpg": 0.07738095238095238,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Brock McGinn",
      "id": 8476934,
      "gpg": 0.15384615384615385,
      "hgpg": 0.136,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Mason McTavish",
      "id": 8482745,
      "gpg": 0.29310344827586204,
      "hgpg": 0.2623762376237624,
      "five_gpg": 0.6,
      "hppg": 0.07920792079207921,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Ryan Strome",
      "id": 8476458,
      "gpg": 0.15625,
      "hgpg": 0.16,
      "five_gpg": 0.2,
      "hppg": 0.013333333333333334,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Troy Terry",
      "id": 8478873,
      "gpg": 0.288135593220339,
      "hgpg": 0.2926829268292683,
      "five_gpg": 0,
      "hppg": 0.06341463414634146,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Frank Vatrano",
      "id": 8478366,
      "gpg": 0.31746031746031744,
      "hgpg": 0.3495575221238938,
      "five_gpg": 0.4,
      "hppg": 0.09292035398230089,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Trevor Zegras",
      "id": 8481533,
      "gpg": 0.1794871794871795,
      "hgpg": 0.23841059602649006,
      "five_gpg": 0,
      "hppg": 0.039735099337748346,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Radko Gudas",
      "id": 8475462,
      "gpg": 0.015873015873015872,
      "hgpg": 0.04054054054054054,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Drew Helleson",
      "id": 8481563,
      "gpg": 0.10526315789473684,
      "hgpg": 0.12195121951219512,
      "five_gpg": 0.4,
      "hppg": 0,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Oliver Kylington",
      "id": 8478430,
      "gpg": 0.07692307692307693,
      "hgpg": 0.08695652173913043,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Jackson LaCombe",
      "id": 8481605,
      "gpg": 0.19298245614035087,
      "hgpg": 0.1,
      "five_gpg": 0,
      "hppg": 0.007692307692307693,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Pavel Mintyukov",
      "id": 8483490,
      "gpg": 0.09615384615384616,
      "hgpg": 0.0782608695652174,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Jacob Trouba",
      "id": 8476885,
      "gpg": 0.015873015873015872,
      "hgpg": 0.05485232067510549,
      "five_gpg": 0.2,
      "hppg": 0.008438818565400843,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Olen Zellweger",
      "id": 8482803,
      "gpg": 0.10869565217391304,
      "hgpg": 0.09722222222222222,
      "five_gpg": 0.2,
      "hppg": 0.013888888888888888,
      "team_name": "Anaheim",
      "tgpg": 2.65625,
      "otga": 2.9375,
      "otshga": 0.578125,
      "home": False
    },
    {
      "name": "Matty Beniers",
      "id": 8482665,
      "gpg": 0.23076923076923078,
      "hgpg": 0.24152542372881355,
      "five_gpg": 0.4,
      "hppg": 0.05508474576271186,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Andre Burakovsky",
      "id": 8477444,
      "gpg": 0.0967741935483871,
      "hgpg": 0.1625,
      "five_gpg": 0.2,
      "hppg": 0.05625,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Jordan Eberle",
      "id": 8474586,
      "gpg": 0.28,
      "hgpg": 0.25125628140703515,
      "five_gpg": 0.2,
      "hppg": 0.06030150753768844,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Michael Eyssimont",
      "id": 8479591,
      "gpg": 0.1,
      "hgpg": 0.11330049261083744,
      "five_gpg": 0.2,
      "hppg": 0.009852216748768473,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "John Hayden",
      "id": 8477401,
      "gpg": 0.1111111111111111,
      "hgpg": 0.16666666666666666,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Kaapo Kakko",
      "id": 8481554,
      "gpg": 0.16129032258064516,
      "hgpg": 0.1894273127753304,
      "five_gpg": 0,
      "hppg": 0.01762114537444934,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Tye Kartye",
      "id": 8481789,
      "gpg": 0.0784313725490196,
      "hgpg": 0.13043478260869565,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Jared McCann",
      "id": 8477955,
      "gpg": 0.24615384615384617,
      "hgpg": 0.3706896551724138,
      "five_gpg": 0.2,
      "hppg": 0.08189655172413793,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Jani Nyman",
      "id": 8483497,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Jaden Schwartz",
      "id": 8475768,
      "gpg": 0.2923076923076923,
      "hgpg": 0.27358490566037735,
      "five_gpg": 0,
      "hppg": 0.08018867924528301,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Chandler Stephenson",
      "id": 8476905,
      "gpg": 0.171875,
      "hgpg": 0.21285140562248997,
      "five_gpg": 0.2,
      "hppg": 0.04819277108433735,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Eeli Tolvanen",
      "id": 8480009,
      "gpg": 0.27692307692307694,
      "hgpg": 0.248868778280543,
      "five_gpg": 0.4,
      "hppg": 0.027149321266968326,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Shane Wright",
      "id": 8483524,
      "gpg": 0.24193548387096775,
      "hgpg": 0.2564102564102564,
      "five_gpg": 0.4,
      "hppg": 0.08974358974358974,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Vince Dunn",
      "id": 8478407,
      "gpg": 0.24444444444444444,
      "hgpg": 0.18592964824120603,
      "five_gpg": 0,
      "hppg": 0.03015075376884422,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Ryker Evans",
      "id": 8482858,
      "gpg": 0.08620689655172414,
      "hgpg": 0.06382978723404255,
      "five_gpg": 0,
      "hppg": 0.010638297872340425,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Cale Fleury",
      "id": 8479985,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Adam Larsson",
      "id": 8476457,
      "gpg": 0.07692307692307693,
      "hgpg": 0.07851239669421488,
      "five_gpg": 0.4,
      "hppg": 0,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Joshua Mahura",
      "id": 8479372,
      "gpg": 0,
      "hgpg": 0.021505376344086023,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Brandon Montour",
      "id": 8477986,
      "gpg": 0.203125,
      "hgpg": 0.18823529411764706,
      "five_gpg": 0.6,
      "hppg": 0.043137254901960784,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Jamie Oleksiak",
      "id": 8476467,
      "gpg": 0.06153846153846154,
      "hgpg": 0.06779661016949153,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Seattle",
      "tgpg": 2.93846,
      "otga": 3.25,
      "otshga": 0.578125,
      "home": True
    },
    {
      "name": "Josh Anderson",
      "id": 8476981,
      "gpg": 0.15625,
      "hgpg": 0.1895734597156398,
      "five_gpg": 0.2,
      "hppg": 0.018957345971563982,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Joel Armia",
      "id": 8476469,
      "gpg": 0.171875,
      "hgpg": 0.2023121387283237,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Cole Caufield",
      "id": 8481540,
      "gpg": 0.5,
      "hgpg": 0.4479166666666667,
      "five_gpg": 0.8,
      "hppg": 0.13541666666666666,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Kirby Dach",
      "id": 8481523,
      "gpg": 0.17543859649122806,
      "hgpg": 0.20512820512820512,
      "five_gpg": 0,
      "hppg": 0.05982905982905983,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Christian Dvorak",
      "id": 8477989,
      "gpg": 0.09375,
      "hgpg": 0.13291139240506328,
      "five_gpg": 0,
      "hppg": 0.012658227848101266,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Jake Evans",
      "id": 8478133,
      "gpg": 0.1875,
      "hgpg": 0.105,
      "five_gpg": 0.2,
      "hppg": 0.005,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Brendan Gallagher",
      "id": 8475848,
      "gpg": 0.234375,
      "hgpg": 0.21910112359550563,
      "five_gpg": 0,
      "hppg": 0.033707865168539325,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Emil Heineman",
      "id": 8482476,
      "gpg": 0.20408163265306123,
      "hgpg": 0.18867924528301888,
      "five_gpg": 0,
      "hppg": 0.05660377358490566,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Patrik Laine",
      "id": 8479339,
      "gpg": 0.4117647058823529,
      "hgpg": 0.3925233644859813,
      "five_gpg": 0.2,
      "hppg": 0.18691588785046728,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Alex Newhook",
      "id": 8481618,
      "gpg": 0.1875,
      "hgpg": 0.1971153846153846,
      "five_gpg": 0.2,
      "hppg": 0.019230769230769232,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Michael Pezzetta",
      "id": 8479543,
      "gpg": 0,
      "hgpg": 0.07142857142857142,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Joshua Roy",
      "id": 8482749,
      "gpg": 0,
      "hgpg": 0.14285714285714285,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Juraj Slafkovský",
      "id": 8483515,
      "gpg": 0.19672131147540983,
      "hgpg": 0.1978021978021978,
      "five_gpg": 0.4,
      "hppg": 0.054945054945054944,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Nick Suzuki",
      "id": 8480018,
      "gpg": 0.3125,
      "hgpg": 0.34649122807017546,
      "five_gpg": 0.4,
      "hppg": 0.09649122807017543,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Alexandre Carrier",
      "id": 8478851,
      "gpg": 0.04918032786885246,
      "hgpg": 0.0546448087431694,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Kaiden Guhle",
      "id": 8482087,
      "gpg": 0.09090909090909091,
      "hgpg": 0.08860759493670886,
      "five_gpg": 0.2,
      "hppg": 0,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Lane Hutson",
      "id": 8483457,
      "gpg": 0.0625,
      "hgpg": 0.06060606060606061,
      "five_gpg": 0,
      "hppg": 0.015151515151515152,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Mike Matheson",
      "id": 8476875,
      "gpg": 0.0967741935483871,
      "hgpg": 0.13020833333333334,
      "five_gpg": 0.4,
      "hppg": 0.03125,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "David Savard",
      "id": 8475233,
      "gpg": 0.017241379310344827,
      "hgpg": 0.05555555555555555,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Jayden Struble",
      "id": 8481593,
      "gpg": 0.05263157894736842,
      "hgpg": 0.05319148936170213,
      "five_gpg": 0,
      "hppg": 0,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    },
    {
      "name": "Arber Xhekaj",
      "id": 8482964,
      "gpg": 0.01639344262295082,
      "hgpg": 0.057692307692307696,
      "five_gpg": 0,
      "hppg": 0.01282051282051282,
      "team_name": "Montréal",
      "tgpg": 2.9375,
      "otga": 3.21538,
      "otshga": 0.5076923076923077,
      "home": False
    }
  ]
}
    
    print(handle_make_predictions(input, None))