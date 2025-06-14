import json
from pathlib import Path

import pytest


@pytest.fixture
def players_input():
    """Yields player data from a JSON file."""
    file_path = Path(__file__).parent.parent / "data" / "players_input.json"
    with open(file_path, "r") as f:
        data = json.load(f)
    yield data


@pytest.fixture
def old_entries():
    """Yields old entries from a JSON file."""
    file_path = Path(__file__).parent.parent / "data" / "old_entries.json"
    with open(file_path, "r") as f:
        data = json.load(f)
    yield data
