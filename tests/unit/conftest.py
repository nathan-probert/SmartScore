import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ✅ Global boto3.client mock for all tests
def _mock_boto3_client(service_name, *args, **kwargs):
    if service_name == "lambda":
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"Payload": MagicMock(read=lambda: b'{"statusCode": 200, "body": "{}"}')}
        return mock_lambda
    # Add other services as needed:
    # elif service_name == "s3": ...

    raise NotImplementedError(f"No mock implemented for service: {service_name}")


@pytest.fixture(autouse=True, scope="session")
def _auto_mock_boto3_client():
    """Automatically mock boto3.client globally for all tests."""
    with patch("boto3.client", side_effect=_mock_boto3_client):
        yield
