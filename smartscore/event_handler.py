from aws_lambda_powertools import Logger
from smartscore_info_client.schemas.player_info import PLAYER_INFO_SCHEMA, PlayerInfo
from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA, TeamInfo

from decorators import lambda_handler_error_responder
from service import (
    gather_odds,
    get_date,
    get_players_from_team,
    get_teams,
    get_tims,
    get_todays_schedule,
    make_predictions,
    backfill_dates,
    publish_public_db,
)

logger = Logger()


@lambda_handler_error_responder
def handle_get_teams(event, context):
    data = get_todays_schedule()

    teams = get_teams(data)
    logger.info(f"Found [{len(teams)}] teams")

    return {
        "statusCode": 200,
        "teams": TEAM_INFO_SCHEMA.dump(teams, many=True),  # Return teams as a direct field
    }


@lambda_handler_error_responder
def handle_get_players_from_team(event, context):
    team = TeamInfo(**event)

    logger.info(f"Getting players for team: {team.team_name}")
    players = get_players_from_team(team)
    logger.info(f"Found [{len(players)}] players for team")

    return PLAYER_INFO_SCHEMA.dump(players, many=True)


@lambda_handler_error_responder
def handle_make_predictions(event, context):
    all_players = [PlayerInfo(**player) for player in event.get("players")]
    all_teams = [TeamInfo(**team) for team in event.get("teams")]
    date = get_date()

    all_players = make_predictions(all_teams, all_players, date)

    return {
        "statusCode": 200,
        "method": "POST_BATCH",
        "date": date,
        "teams": TEAM_INFO_SCHEMA.dump(all_teams, many=True),
        "players": PLAYER_INFO_SCHEMA.dump(all_players, many=True),
    }


### This code does not work on AWS environment as the IP is blacklisted ###
@lambda_handler_error_responder
def handle_get_odds(event, context):
    logger.info("Getting odds for players")
    all_players = [PlayerInfo(**player) for player in event.get("players")]
    all_teams = [TeamInfo(**team) for team in event.get("teams")]

    all_players = gather_odds(all_players)

    return {
        "statusCode": 200,
        "teams": TEAM_INFO_SCHEMA.dump(all_teams, many=True),
        "players": PLAYER_INFO_SCHEMA.dump(all_players, many=True),
    }


@lambda_handler_error_responder
def handle_get_tims(event, context):
    all_players = [PlayerInfo(**player) for team in event for player in team.pop("players")]
    all_teams = [TeamInfo(**team) for team in event]

    all_players = get_tims(all_players)

    return {
        "statusCode": 200,
        "teams": TEAM_INFO_SCHEMA.dump(all_teams, many=True),
        "players": PLAYER_INFO_SCHEMA.dump(all_players, many=True),
    }


@lambda_handler_error_responder
def handle_backfill(event, context):
    backfill_dates()

    return {
        "statusCode": 200,
    }


@lambda_handler_error_responder
def handle_publish_db(event, context):
    players = [PlayerInfo(**player) for player in event.get("players")]
    teams = [TeamInfo(**team) for team in event.get("teams")]

    publish_public_db(teams, players)

    return {
        "statusCode": 200,
        "method": "POST_BATCH",
        "date": event.get("date"),
        "teams": TEAM_INFO_SCHEMA.dump(teams, many=True),
        "players": PLAYER_INFO_SCHEMA.dump(players, many=True),
    }


if __name__ == "__main__":
    data = {
  "statusCode": 200,
  "teams": [
    {
      "team_name": "Boston",
      "team_abbr": "BOS",
      "season": "20242025",
      "team_id": 6,
      "opponent_id": 13,
      "tgpg": 4,
      "otga": 4
    },
    {
      "team_name": "Florida",
      "team_abbr": "FLA",
      "season": "20242025",
      "team_id": 13,
      "opponent_id": 6,
      "tgpg": 3,
      "otga": 3.66666
    },
    {
      "team_name": "Ottawa",
      "team_abbr": "OTT",
      "season": "20242025",
      "team_id": 9,
      "opponent_id": 26,
      "tgpg": 2,
      "otga": 1.5
    },
    {
      "team_name": "Los Angeles",
      "team_abbr": "LAK",
      "season": "20242025",
      "team_id": 26,
      "opponent_id": 9,
      "tgpg": 2,
      "otga": 2.5
    },
    {
      "team_name": "New Jersey",
      "team_abbr": "NJD",
      "season": "20242025",
      "team_id": 1,
      "opponent_id": 59,
      "tgpg": 3.5,
      "otga": 3.66666
    },
    {
      "team_name": "Utah",
      "team_abbr": "UTA",
      "season": "20242025",
      "team_id": 59,
      "opponent_id": 1,
      "tgpg": 5.33333,
      "otga": 2.25
    },
    {
      "team_name": "New York",
      "team_abbr": "NYR",
      "season": "20242025",
      "team_id": 3,
      "opponent_id": 17,
      "tgpg": 5.5,
      "otga": 3
    },
    {
      "team_name": "Detroit",
      "team_abbr": "DET",
      "season": "20242025",
      "team_id": 17,
      "opponent_id": 3,
      "tgpg": 3,
      "otga": 3
    },
    {
      "team_name": "Montréal",
      "team_abbr": "MTL",
      "season": "20242025",
      "team_id": 8,
      "opponent_id": 5,
      "tgpg": 3,
      "otga": 4.33333
    },
    {
      "team_name": "Pittsburgh",
      "team_abbr": "PIT",
      "season": "20242025",
      "team_id": 5,
      "opponent_id": 8,
      "tgpg": 2.66666,
      "otga": 2.33333
    },
    {
      "team_name": "Colorado",
      "team_abbr": "COL",
      "season": "20242025",
      "team_id": 21,
      "opponent_id": 2,
      "tgpg": 4,
      "otga": 4
    },
    {
      "team_name": "New York",
      "team_abbr": "NYI",
      "season": "20242025",
      "team_id": 2,
      "opponent_id": 21,
      "tgpg": 2,
      "otga": 7
    }
  ],
  "players": [
    {
      "name": "John Beecher",
      "id": 8481556,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.11940298507462686,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Justin Brazeau",
      "id": 8479638,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.1935483870967742,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Charlie Coyle",
      "id": 8475745,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.22994652406417113,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Trent Frederic",
      "id": 8479365,
      "team_id": 6,
      "gpg": 0.3333333333333333,
      "hgpg": 0.21428571428571427,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Morgan Geekie",
      "id": 8479987,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.1839080459770115,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Max Jones",
      "id": 8479368,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.11475409836065574,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Mark Kastelic",
      "id": 8480355,
      "team_id": 6,
      "gpg": 0.6666666666666666,
      "hgpg": 0.10687022900763359,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Cole Koepke",
      "id": 8481043,
      "team_id": 6,
      "gpg": 0.3333333333333333,
      "hgpg": 0.06896551724137931,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Elias Lindholm",
      "id": 8477496,
      "team_id": 6,
      "gpg": 0.6666666666666666,
      "hgpg": 0.2573099415204678,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Brad Marchand",
      "id": 8473419,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.32386363636363635,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "David Pastrnak",
      "id": 8477956,
      "team_id": 6,
      "gpg": 1,
      "hgpg": 0.6417112299465241,
      "five_gpg": 0.6,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Matthew Poitras",
      "id": 8483505,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.14705882352941177,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Riley Tufte",
      "id": 8479362,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.1111111111111111,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Pavel Zacha",
      "id": 8478401,
      "team_id": 6,
      "gpg": 0.3333333333333333,
      "hgpg": 0.24043715846994534,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Brandon Carlo",
      "id": 8478443,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.05747126436781609,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Hampus Lindholm",
      "id": 8476854,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.07954545454545454,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Mason Lohrei",
      "id": 8482511,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.09259259259259259,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Charlie McAvoy",
      "id": 8479325,
      "team_id": 6,
      "gpg": 0.6666666666666666,
      "hgpg": 0.13414634146341464,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Andrew Peeke",
      "id": 8479369,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.05511811023622047,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Parker Wotherspoon",
      "id": 8478450,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nikita Zadorov",
      "id": 8477507,
      "team_id": 6,
      "gpg": 0,
      "hgpg": 0.13872832369942195,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Aleksander Barkov",
      "id": 8477493,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.31382978723404253,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Sam Bennett",
      "id": 8477935,
      "team_id": 13,
      "gpg": 1,
      "hgpg": 0.29310344827586204,
      "five_gpg": 0.6,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jesper Boqvist",
      "id": 8480003,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.11940298507462686,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Jonah Gadjovich",
      "id": 8479981,
      "team_id": 13,
      "gpg": 0.3333333333333333,
      "hgpg": 0.07792207792207792,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Patrick Giles",
      "id": 8480825,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "A.J. Greer",
      "id": 8478421,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.08943089430894309,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Anton Lundell",
      "id": 8482113,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.1507537688442211,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Eetu Luostarinen",
      "id": 8480185,
      "team_id": 13,
      "gpg": 0.3333333333333333,
      "hgpg": 0.1642512077294686,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Tomas Nosek",
      "id": 8477931,
      "team_id": 13,
      "gpg": 0.05555555555555555,
      "hgpg": 0.06282722513089005,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Sam Reinhart",
      "id": 8477933,
      "team_id": 13,
      "gpg": 0.3333333333333333,
      "hgpg": 0.5047169811320755,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Evan Rodrigues",
      "id": 8478542,
      "team_id": 13,
      "gpg": 0.3333333333333333,
      "hgpg": 0.20218579234972678,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Mackie Samoskevich",
      "id": 8482713,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Matthew Tkachuk",
      "id": 8479314,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.40487804878048783,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Carter Verhaeghe",
      "id": 8477409,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.4585365853658537,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Uvis Balinskis",
      "id": 8484304,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.034482758620689655,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Adam Boqvist",
      "id": 8480871,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.07228915662650602,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Aaron Ekblad",
      "id": 8477932,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.1242603550295858,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Gustav Forsling",
      "id": 8478055,
      "team_id": 13,
      "gpg": 0.3333333333333333,
      "hgpg": 0.14354066985645933,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Dmitry Kulikov",
      "id": 8475179,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.023529411764705882,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Niko Mikkola",
      "id": 8478859,
      "team_id": 13,
      "gpg": 0,
      "hgpg": 0.030456852791878174,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nate Schmidt",
      "id": 8477220,
      "team_id": 13,
      "gpg": 0.5,
      "hgpg": 0.0763888888888889,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Michael Amadio",
      "id": 8478020,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.2222222222222222,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Drake Batherson",
      "id": 8480208,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.30120481927710846,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nick Cousins",
      "id": 8476393,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.09836065573770492,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Adam Gaudette",
      "id": 8478874,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Claude Giroux",
      "id": 8473512,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.3373493975903614,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Noah Gregor",
      "id": 8479393,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.12903225806451613,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ridly Greig",
      "id": 8482092,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.1595744680851064,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Matthew Highmore",
      "id": 8478146,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.09090909090909091,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Zack MacEwen",
      "id": 8479772,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.06818181818181818,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Josh Norris",
      "id": 8480064,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.3,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "David Perron",
      "id": 8474102,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.25625,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Shane Pinto",
      "id": 8481596,
      "team_id": 9,
      "gpg": 0.5,
      "hgpg": 0.24,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Tim Stützle",
      "id": 8482116,
      "team_id": 9,
      "gpg": 1.5,
      "hgpg": 0.3870967741935484,
      "five_gpg": 0.6,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Brady Tkachuk",
      "id": 8480801,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.43636363636363634,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jacob Bernard-Docker",
      "id": 8480879,
      "team_id": 9,
      "gpg": 0.05555555555555555,
      "hgpg": 0.04040404040404041,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Thomas Chabot",
      "id": 8478469,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.1652892561983471,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Travis Hamonic",
      "id": 8474612,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.064,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Nick Jensen",
      "id": 8475324,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.0379746835443038,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Tyler Kleven",
      "id": 8482095,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jake Sanderson",
      "id": 8482105,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.08860759493670886,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Artem Zub",
      "id": 8482245,
      "team_id": 9,
      "gpg": 0,
      "hgpg": 0.06451612903225806,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Quinton Byfield",
      "id": 8482124,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.1643835616438356,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Phillip Danault",
      "id": 8476479,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.2138728323699422,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kevin Fiala",
      "id": 8477942,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.33540372670807456,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Warren Foegele",
      "id": 8477998,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.20540540540540542,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Tanner Jeannot",
      "id": 8479661,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.09285714285714286,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Arthur Kaliyev",
      "id": 8481560,
      "team_id": 26,
      "gpg": 0.13725490196078433,
      "hgpg": 0.17346938775510204,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Adrian Kempe",
      "id": 8477960,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.45348837209302323,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Anze Kopitar",
      "id": 8471685,
      "team_id": 26,
      "gpg": 1.5,
      "hgpg": 0.3409090909090909,
      "five_gpg": 0.6,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Alex Laferriere",
      "id": 8482155,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.14772727272727273,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Trevor Lewis",
      "id": 8473453,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.09941520467836257,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Trevor Moore",
      "id": 8479675,
      "team_id": 26,
      "gpg": 0.5,
      "hgpg": 0.2857142857142857,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Akil Thomas",
      "id": 8480851,
      "team_id": 26,
      "gpg": 0.42857142857142855,
      "hgpg": 0.42857142857142855,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Alex Turcotte",
      "id": 8481532,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.038461538461538464,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Mikey Anderson",
      "id": 8479998,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.04878048780487805,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kyle Burroughs",
      "id": 8477335,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.032520325203252036,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Brandt Clarke",
      "id": 8482730,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.07407407407407407,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Drew Doughty",
      "id": 8474563,
      "team_id": 26,
      "gpg": 0.19540229885057472,
      "hgpg": 0.15492957746478872,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Joel Edmundson",
      "id": 8476441,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.024390243902439025,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Andreas Englund",
      "id": 8477971,
      "team_id": 26,
      "gpg": 0.011494252873563218,
      "hgpg": 0.007462686567164179,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Vladislav Gavrikov",
      "id": 8478882,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.07407407407407407,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Caleb Jones",
      "id": 8478452,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.05921052631578947,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jacob Moverare",
      "id": 8479421,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.038461538461538464,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jordan Spence",
      "id": 8481606,
      "team_id": 26,
      "gpg": 0,
      "hgpg": 0.023809523809523808,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nathan Bastian",
      "id": 8479414,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.10619469026548672,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jesper Bratt",
      "id": 8479407,
      "team_id": 1,
      "gpg": 0.25,
      "hgpg": 0.3388888888888889,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Paul Cotter",
      "id": 8481032,
      "team_id": 1,
      "gpg": 1,
      "hgpg": 0.17777777777777778,
      "five_gpg": 0.8,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Erik Haula",
      "id": 8475287,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.19767441860465115,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nico Hischier",
      "id": 8480002,
      "team_id": 1,
      "gpg": 0.25,
      "hgpg": 0.35714285714285715,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jack Hughes",
      "id": 8481559,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.48717948717948717,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Curtis Lazar",
      "id": 8477508,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.08461538461538462,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Kurtis MacDermid",
      "id": 8477073,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.03333333333333333,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Timo Meier",
      "id": 8478414,
      "team_id": 1,
      "gpg": 0.5,
      "hgpg": 0.4444444444444444,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Dawson Mercer",
      "id": 8482110,
      "team_id": 1,
      "gpg": 0.25,
      "hgpg": 0.2833333333333333,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Stefan Noesen",
      "id": 8476474,
      "team_id": 1,
      "gpg": 0.25,
      "hgpg": 0.19047619047619047,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ondrej Palat",
      "id": 8476292,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.16296296296296298,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Tomas Tatar",
      "id": 8475193,
      "team_id": 1,
      "gpg": 0.25,
      "hgpg": 0.18452380952380953,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Seamus Casey",
      "id": 8483429,
      "team_id": 1,
      "gpg": 0.5,
      "hgpg": 0.5,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Brenden Dillon",
      "id": 8475455,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.05847953216374269,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Dougie Hamilton",
      "id": 8476462,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.23728813559322035,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Santeri Hatakka",
      "id": 8481701,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Johnathan Kovacevic",
      "id": 8480192,
      "team_id": 1,
      "gpg": 0.25,
      "hgpg": 0.06993006993006994,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Simon Nemec",
      "id": 8483495,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.046875,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jonas Siegenthaler",
      "id": 8478399,
      "team_id": 1,
      "gpg": 0,
      "hgpg": 0.039473684210526314,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Michael Carcone",
      "id": 8479619,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.27058823529411763,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Logan Cooley",
      "id": 8483431,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.23529411764705882,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Lawson Crouse",
      "id": 8478474,
      "team_id": 59,
      "gpg": 0.6666666666666666,
      "hgpg": 0.30434782608695654,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Josh Doan",
      "id": 8482659,
      "team_id": 59,
      "gpg": 0.3333333333333333,
      "hgpg": 0.42857142857142855,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Dylan Guenther",
      "id": 8482699,
      "team_id": 59,
      "gpg": 1.6666666666666667,
      "hgpg": 0.35802469135802467,
      "five_gpg": 1,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Barrett Hayton",
      "id": 8480849,
      "team_id": 59,
      "gpg": 1,
      "hgpg": 0.211864406779661,
      "five_gpg": 0.6,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Clayton Keller",
      "id": 8479343,
      "team_id": 59,
      "gpg": 1,
      "hgpg": 0.44785276073619634,
      "five_gpg": 0.6,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Alex Kerfoot",
      "id": 8477021,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.1404494382022472,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Matias Maccelli",
      "id": 8481711,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.18791946308724833,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jack McBain",
      "id": 8480855,
      "team_id": 59,
      "gpg": 0.3333333333333333,
      "hgpg": 0.13815789473684212,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Liam O'Brien",
      "id": 8477070,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.06060606060606061,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nick Schmaltz",
      "id": 8477951,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.30344827586206896,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kevin Stenlund",
      "id": 8478831,
      "team_id": 59,
      "gpg": 0.3333333333333333,
      "hgpg": 0.11377245508982035,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kailer Yamamoto",
      "id": 8479977,
      "team_id": 59,
      "gpg": 0.13559322033898305,
      "hgpg": 0.18303571428571427,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Robert Bortuzzo",
      "id": 8474145,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.0189873417721519,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ian Cole",
      "id": 8474013,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.033707865168539325,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Sean Durzi",
      "id": 8480434,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.12101910828025478,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Michael Kesselring",
      "id": 8480891,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.06493506493506493,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Vladislav Kolyachonok",
      "id": 8481609,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.1,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Mikhail Sergachev",
      "id": 8479410,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.10483870967741936,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Juuso Valimaki",
      "id": 8479976,
      "team_id": 59,
      "gpg": 0,
      "hgpg": 0.040268456375838924,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jonny Brodzinski",
      "id": 8477380,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.08974358974358974,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Sam Carrick",
      "id": 8475842,
      "team_id": 3,
      "gpg": 0.5,
      "hgpg": 0.09929078014184398,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Filip Chytil",
      "id": 8480078,
      "team_id": 3,
      "gpg": 0.5,
      "hgpg": 0.24242424242424243,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Will Cuylle",
      "id": 8482157,
      "team_id": 3,
      "gpg": 0.5,
      "hgpg": 0.14563106796116504,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Adam Edstrom",
      "id": 8481726,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.15384615384615385,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kaapo Kakko",
      "id": 8481554,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.19760479041916168,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Chris Kreider",
      "id": 8475184,
      "team_id": 3,
      "gpg": 1,
      "hgpg": 0.489247311827957,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Alexis Lafrenière",
      "id": 8482109,
      "team_id": 3,
      "gpg": 0.5,
      "hgpg": 0.28191489361702127,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Artemi Panarin",
      "id": 8478550,
      "team_id": 3,
      "gpg": 1,
      "hgpg": 0.4497354497354497,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Matt Rempe",
      "id": 8482460,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.06896551724137931,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Reilly Smith",
      "id": 8475191,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.24157303370786518,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Vincent Trocheck",
      "id": 8476389,
      "team_id": 3,
      "gpg": 0.5,
      "hgpg": 0.30158730158730157,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jimmy Vesey",
      "id": 8476918,
      "team_id": 3,
      "gpg": 0.15217391304347827,
      "hgpg": 0.13306451612903225,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Mika Zibanejad",
      "id": 8476459,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.3670212765957447,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Adam Fox",
      "id": 8479323,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.16201117318435754,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Zac Jones",
      "id": 8481708,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.061224489795918366,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ryan Lindgren",
      "id": 8479324,
      "team_id": 3,
      "gpg": 0.03260869565217391,
      "hgpg": 0.042801556420233464,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Victor Mancini",
      "id": 8483768,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "K'Andre Miller",
      "id": 8480817,
      "team_id": 3,
      "gpg": 0.5,
      "hgpg": 0.10326086956521739,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Chad Ruhwedel",
      "id": 8477244,
      "team_id": 3,
      "gpg": 0.019230769230769232,
      "hgpg": 0.03260869565217391,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Braden Schneider",
      "id": 8482073,
      "team_id": 3,
      "gpg": 0.5,
      "hgpg": 0.06382978723404255,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jacob Trouba",
      "id": 8476885,
      "team_id": 3,
      "gpg": 0,
      "hgpg": 0.06818181818181818,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jonatan Berggren",
      "id": 8481013,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.20987654320987653,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "J.T. Compher",
      "id": 8477456,
      "team_id": 17,
      "gpg": 0.5,
      "hgpg": 0.2261904761904762,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Andrew Copp",
      "id": 8477429,
      "team_id": 17,
      "gpg": 0.5,
      "hgpg": 0.1411042944785276,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Alex DeBrincat",
      "id": 8479337,
      "team_id": 17,
      "gpg": 1,
      "hgpg": 0.3373493975903614,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Christian Fischer",
      "id": 8478432,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.11180124223602485,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Patrick Kane",
      "id": 8474141,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.3181818181818182,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Dylan Larkin",
      "id": 8477946,
      "team_id": 17,
      "gpg": 0.5,
      "hgpg": 0.44,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Tyler Motte",
      "id": 8477353,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.10344827586206896,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Michael Rasmussen",
      "id": 8479992,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.17293233082706766,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Lucas Raymond",
      "id": 8482078,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.3037974683544304,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Vladimir Tarasenko",
      "id": 8475765,
      "team_id": 17,
      "gpg": 0.5,
      "hgpg": 0.2808988764044944,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Joe Veleno",
      "id": 8480813,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.12883435582822086,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ben Chiarot",
      "id": 8475279,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.06451612903225806,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Simon Edvinsson",
      "id": 8482762,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.1111111111111111,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Erik Gustafsson",
      "id": 8476979,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.08484848484848485,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Justin Holl",
      "id": 8475718,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.015748031496062992,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Albert Johansson",
      "id": 8481607,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Olli Maatta",
      "id": 8476874,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.06578947368421052,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jeff Petry",
      "id": 8473507,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.05925925925925926,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Moritz Seider",
      "id": 8481542,
      "team_id": 17,
      "gpg": 0,
      "hgpg": 0.08433734939759036,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Josh Anderson",
      "id": 8476981,
      "team_id": 8,
      "gpg": 0.3333333333333333,
      "hgpg": 0.20666666666666667,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Joel Armia",
      "id": 8476469,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.21428571428571427,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Alex Barré-Boulet",
      "id": 8479718,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.15384615384615385,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Cole Caufield",
      "id": 8481540,
      "team_id": 8,
      "gpg": 1.3333333333333333,
      "hgpg": 0.44274809160305345,
      "five_gpg": 0.8,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kirby Dach",
      "id": 8481523,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.2222222222222222,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Christian Dvorak",
      "id": 8477989,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.15463917525773196,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jake Evans",
      "id": 8478133,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.06474820143884892,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Brendan Gallagher",
      "id": 8475848,
      "team_id": 8,
      "gpg": 0.6666666666666666,
      "hgpg": 0.2222222222222222,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Emil Heineman",
      "id": 8482476,
      "team_id": 8,
      "gpg": 0.5,
      "hgpg": 0.16666666666666666,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Oliver Kapanen",
      "id": 8482775,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Alex Newhook",
      "id": 8481618,
      "team_id": 8,
      "gpg": 0.3333333333333333,
      "hgpg": 0.20408163265306123,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Michael Pezzetta",
      "id": 8479543,
      "team_id": 8,
      "gpg": 0.04918032786885246,
      "hgpg": 0.08571428571428572,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Juraj Slafkovsky",
      "id": 8483515,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.1935483870967742,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nick Suzuki",
      "id": 8480018,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.3532934131736527,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Justin Barron",
      "id": 8482111,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.12222222222222222,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kaiden Guhle",
      "id": 8482087,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.08547008547008547,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Lane Hutson",
      "id": 8483457,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Mike Matheson",
      "id": 8476875,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.14285714285714285,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "David Savard",
      "id": 8475233,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.072,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jayden Struble",
      "id": 8481593,
      "team_id": 8,
      "gpg": 0.05357142857142857,
      "hgpg": 0.05357142857142857,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Arber Xhekaj",
      "id": 8482964,
      "team_id": 8,
      "gpg": 0,
      "hgpg": 0.08163265306122448,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Noel Acciari",
      "id": 8478569,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.136986301369863,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Anthony Beauvillier",
      "id": 8478463,
      "team_id": 5,
      "gpg": 0.6666666666666666,
      "hgpg": 0.17218543046357615,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Michael Bunting",
      "id": 8478047,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.24855491329479767,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Sidney Crosby",
      "id": 8471675,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.4491017964071856,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Lars Eller",
      "id": 8474189,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.14204545454545456,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Cody Glass",
      "id": 8479996,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.1724137931034483,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Kevin Hayes",
      "id": 8475763,
      "team_id": 5,
      "gpg": 0.3333333333333333,
      "hgpg": 0.19631901840490798,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Blake Lizotte",
      "id": 8481481,
      "team_id": 5,
      "gpg": 0.11940298507462686,
      "hgpg": 0.12719298245614036,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Evgeni Malkin",
      "id": 8471215,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.32335329341317365,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Rutger McGroarty",
      "id": 8483487,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Matt Nieto",
      "id": 8476442,
      "team_id": 5,
      "gpg": 0.045454545454545456,
      "hgpg": 0.10555555555555556,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Drew O'Connor",
      "id": 8482055,
      "team_id": 5,
      "gpg": 0.3333333333333333,
      "hgpg": 0.171875,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jesse Puljujarvi",
      "id": 8479344,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.07547169811320754,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Valtteri Puustinen",
      "id": 8481703,
      "team_id": 5,
      "gpg": 0.09615384615384616,
      "hgpg": 0.09433962264150944,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Rickard Rakell",
      "id": 8476483,
      "team_id": 5,
      "gpg": 0.3333333333333333,
      "hgpg": 0.2838709677419355,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Bryan Rust",
      "id": 8475810,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.3333333333333333,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ryan Graves",
      "id": 8477435,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.06832298136645963,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Matt Grzelcyk",
      "id": 8476891,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0.04054054054054054,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Erik Karlsson",
      "id": 8474578,
      "team_id": 5,
      "gpg": 0.3333333333333333,
      "hgpg": 0.2215568862275449,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kris Letang",
      "id": 8471724,
      "team_id": 5,
      "gpg": 0.3333333333333333,
      "hgpg": 0.15436241610738255,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Marcus Pettersson",
      "id": 8477969,
      "team_id": 5,
      "gpg": 0.3333333333333333,
      "hgpg": 0.0392156862745098,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ryan Shea",
      "id": 8478854,
      "team_id": 5,
      "gpg": 0.03225806451612903,
      "hgpg": 0.03225806451612903,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jack St. Ivany",
      "id": 8481030,
      "team_id": 5,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ross Colton",
      "id": 8479525,
      "team_id": 21,
      "gpg": 0.5,
      "hgpg": 0.2,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Maxmilian Curran",
      "id": 8484846,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jonathan Drouin",
      "id": 8477494,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.14893617021276595,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ivan Ivan",
      "id": 8483930,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Parker Kelly",
      "id": 8480448,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.06569343065693431,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Joel Kiviranta",
      "id": 8481641,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.08609271523178808,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nikolai Kovalenko",
      "id": 8481042,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Nathan MacKinnon",
      "id": 8477492,
      "team_id": 21,
      "gpg": 0.5,
      "hgpg": 0.5838150289017341,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Casey Mittelstadt",
      "id": 8479999,
      "team_id": 21,
      "gpg": 1,
      "hgpg": 0.21714285714285714,
      "five_gpg": 0.4,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Logan O'Connor",
      "id": 8481186,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.14864864864864866,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Mikko Rantanen",
      "id": 8478420,
      "team_id": 21,
      "gpg": 1.5,
      "hgpg": 0.6098901098901099,
      "five_gpg": 0.6,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Calum Ritchie",
      "id": 8484221,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Chris Wagner",
      "id": 8475780,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.058823529411764705,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Miles Wood",
      "id": 8477425,
      "team_id": 21,
      "gpg": 0.5,
      "hgpg": 0.16374269005847952,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Calvin de Haan",
      "id": 8475177,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.043478260869565216,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Samuel Girard",
      "id": 8479398,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.058823529411764705,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Oliver Kylington",
      "id": 8478430,
      "team_id": 21,
      "gpg": 0.09090909090909091,
      "hgpg": 0.11016949152542373,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "John Ludvig",
      "id": 8481206,
      "team_id": 21,
      "gpg": 0.09090909090909091,
      "hgpg": 0.09090909090909091,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Cale Makar",
      "id": 8480069,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.28205128205128205,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Sam Malinski",
      "id": 8484258,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.12,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Josh Manson",
      "id": 8476312,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.09917355371900827,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Devon Toews",
      "id": 8478038,
      "team_id": 21,
      "gpg": 0,
      "hgpg": 0.11602209944751381,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Mathew Barzal",
      "id": 8478445,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.271523178807947,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 1
    },
    {
      "name": "Casey Cizikas",
      "id": 8475231,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.10975609756097561,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Anthony Duclair",
      "id": 8477407,
      "team_id": 2,
      "gpg": 0.5,
      "hgpg": 0.25833333333333336,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 2
    },
    {
      "name": "Julien Gauthier",
      "id": 8479328,
      "team_id": 2,
      "gpg": 0.18518518518518517,
      "hgpg": 0.12781954887218044,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Simon Holmstrom",
      "id": 8481601,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.16153846153846155,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Bo Horvat",
      "id": 8477500,
      "team_id": 2,
      "gpg": 0.5,
      "hgpg": 0.4277456647398844,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Anders Lee",
      "id": 8475314,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.2840909090909091,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kyle MacLean",
      "id": 8481237,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.1282051282051282,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Brock Nelson",
      "id": 8475754,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.4180790960451977,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Jean-Gabriel Pageau",
      "id": 8476419,
      "team_id": 2,
      "gpg": 0.5,
      "hgpg": 0.15853658536585366,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Kyle Palmieri",
      "id": 8475151,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.32666666666666666,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Maxim Tsyplakov",
      "id": 8484958,
      "team_id": 2,
      "gpg": 0.5,
      "hgpg": 0.5,
      "five_gpg": 0.2,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Oliver Wahlstrom",
      "id": 8480789,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.13043478260869565,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Dennis Cholowski",
      "id": 8479395,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Noah Dobson",
      "id": 8480865,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.13529411764705881,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Scott Mayfield",
      "id": 8476429,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.05343511450381679,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 3
    },
    {
      "name": "Adam Pelech",
      "id": 8476917,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.06060606060606061,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Ryan Pulock",
      "id": 8477506,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.0718954248366013,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Mike Reilly",
      "id": 8476422,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.08974358974358974,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    },
    {
      "name": "Alexander Romanov",
      "id": 8481014,
      "team_id": 2,
      "gpg": 0,
      "hgpg": 0.05357142857142857,
      "five_gpg": 0,
      "stat": None,
      "odds": None,
      "tims": 0
    }
  ]
}
    # handle_make_predictions(data, None)
    handle_backfill(None, None)