import json

from smartscore_info_client.schemas.team_info import TEAM_INFO_SCHEMA


def test_get_teams(schedule_data, team_data):
    from smartscore.service import get_teams

    response = get_teams(schedule_data)

    expected = [frozenset(team_data.items()) for team_data in team_data]
    actual = [frozenset(team.items()) for team in json.loads(TEAM_INFO_SCHEMA.dumps(response, many=True))]
    assert set(expected) == set(actual)
