from aws_lambda_powertools import Logger
from smartscore_info_client.schemas.player_info import PLAYER_INFO_SCHEMA, PlayerInfo
from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA, TeamInfo

from decorators import lambda_handler_error_responder
from service import (
    backfill_dates,
    check_db_for_date,
    get_players_from_team,
    get_teams,
    get_tims,
    get_todays_schedule,
    make_predictions_entries,
    make_predictions_teams,
    publish_public_db,
)

logger = Logger()


@lambda_handler_error_responder
def handle_backfill(event, context):
    backfill_dates()

    return {
        "statusCode": 200,
    }


@lambda_handler_error_responder
def handle_check_completed(event, context):
    entries = check_db_for_date()
    completed = False
    if entries:
        completed = True

    return {"statusCode": 200, "completed": completed, "players": entries}


@lambda_handler_error_responder
def handle_get_teams(event, context):
    data = get_todays_schedule()

    teams = get_teams(data)
    logger.info(f"Found [{len(teams)}] teams")

    return {"statusCode": 200, "teams": TEAM_INFO_SCHEMA.dump(teams, many=True)}


@lambda_handler_error_responder
def handle_get_players_from_team(event, context):
    team = TeamInfo(**event)

    logger.info(f"Getting players for team: {team.team_name}")
    players = get_players_from_team(team)
    logger.info(f"Found [{len(players)}] players for team")

    return PLAYER_INFO_SCHEMA.dump(players, many=True)


@lambda_handler_error_responder
def handle_make_predictions(event, context):
    if event.get("players"):
        all_players = [PlayerInfo(**player) for player in event.get("players")]
        all_teams = [TeamInfo(**team) for team in event.get("teams")]

        entries = make_predictions_teams(all_teams, all_players)
    else:
        entries = event.get("entries")

        # remove date attribute from each player
        for player in entries:
            player.pop("date", None)
            player.pop("scored", None)
        entries = make_predictions_entries(entries)

    return {"statusCode": 200, "players": entries}


@lambda_handler_error_responder
def handle_get_tims(event, context):
    players = event.get("players")

    players = get_tims(players)

    return {"statusCode": 200, "players": players}


@lambda_handler_error_responder
def handle_publish_db(event, context):
    entries = event.get("players")

    publish_public_db(entries)

    return {"statusCode": 200}


### This code does not work on AWS environment as the AWS IP is blacklisted ###
# @lambda_handler_error_responder
# def handle_get_odds(event, context):
#     logger.info("Getting odds for players")
#     all_players = [PlayerInfo(**player) for player in event.get("players")]
#     all_teams = [TeamInfo(**team) for team in event.get("teams")]
#
#     all_players = gather_odds(all_players)
#
#     return {
#         "statusCode": 200,
#         "teams": TEAM_INFO_SCHEMA.dump(all_teams, many=True),
#         "players": PLAYER_INFO_SCHEMA.dump(all_players, many=True),
#     }


if __name__ == "__main__":
    handle_get_teams({}, {})