import json

import pytest


@pytest.fixture()
def schedule_data():
    with open("tests/data/schedule_data.json") as f:
        yield json.load(f)


@pytest.fixture()
def team_data():
    with open("tests/data/teams_response.json") as f:
        yield json.load(f)
