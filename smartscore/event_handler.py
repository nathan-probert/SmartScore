import json

from aws_lambda_powertools import Logger

from decorators import lambda_handler_error_responder
from schemas.player_info import PLAYER_INFO_SCHEMA
from service import get_players_from_team, get_teams, get_todays_schedule

logger = Logger()


@lambda_handler_error_responder
def handle_get_players_for_date(event, context):
    data = get_todays_schedule()

    teams = get_teams(data)
    logger.info(f"Found [{len(teams)}] teams")

    # invoke separate lambda for each get_player_from_team, then combine results?
    players = get_players_from_team(teams)
    logger.info(f"Found [{len(players)}] players")

    return {
        "statusCode": 200,
        "body": PLAYER_INFO_SCHEMA.dumps(players, many=True),
    }


def make_predictions(event, context):
    body = event["body"]

    players_data = json.loads(body)
    players = PLAYER_INFO_SCHEMA.loads(players_data, many=True)


if __name__ == "__main__":
    response = handle_get_players_for_date(None, None)
    make_predictions(response, None)
