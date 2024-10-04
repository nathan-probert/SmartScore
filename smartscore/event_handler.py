import json

from aws_lambda_powertools import Logger

from decorators import lambda_handler_error_responder
from schemas.player_info import PLAYER_INFO_SCHEMA, PlayerInfo
from schemas.team_info import TEAM_INFO_SCHEMA, TeamInfo
from service import get_players_from_team, get_teams, get_todays_schedule

logger = Logger()


@lambda_handler_error_responder
def handle_get_teams(event, context):
    data = get_todays_schedule()

    teams = get_teams(data)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "teams": TEAM_INFO_SCHEMA.dumps(teams, many=True)  # Serialize the teams list as a JSON string
        })
    }


@lambda_handler_error_responder
def handle_get_players_from_team(event, context):
    body = json.loads(event["body"])
    team_data = body["team"]

    team = TEAM_INFO_SCHEMA.loads(team_data)
    team = TeamInfo(**team)

    print(f"Getting players for team: {team.name}")
    players = get_players_from_team(team)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "players": PLAYER_INFO_SCHEMA.dumps(players, many=True),
        })
    }


@lambda_handler_error_responder
def make_predictions(event, context):
    body = event["body"]
    data = PLAYER_INFO_SCHEMA.loads(body['players'], many=True)
    players = [PlayerInfo(**player) for player in data]

    logger.info(players)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "players": PLAYER_INFO_SCHEMA.dumps(players, many=True),
        })
    }


if __name__ == "__main__":
    response = handle_get_players_for_date(None, None)
    make_predictions(response, None)
