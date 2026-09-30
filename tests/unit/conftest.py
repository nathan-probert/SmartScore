import atexit
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Patch supabase.create_client globally for all unit tests to avoid real client
# creation and key validation.
patcher = patch("supabase.create_client", return_value=MagicMock())
patcher.start()
atexit.register(patcher.stop)


# Set dummy AWS credentials and config to prevent real AWS calls during unit
# tests. (The integration tests run separately and need the real creds, so
# these are scoped to unit tests only.)
os.environ["AWS_ACCESS_KEY_ID"] = "mocking_key_id"
os.environ["AWS_SECRET_ACCESS_KEY"] = "mocking_secret_key"
os.environ["AWS_SESSION_TOKEN"] = "mocking_session_token"
os.environ["AWS_SECURITY_TOKEN"] = "mocking_session_token"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

# Prevent loading from shared config/credentials files
os.environ["AWS_CONFIG_FILE"] = "/dev/null"
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"


@pytest.fixture(scope="session", autouse=True)
def mock_global_boto3_client():
    """
    Mocks boto3.client globally for the entire unit test session.
    Any call to boto3.client() will return a MagicMock.
    """
    with patch("boto3.client") as mock_client_constructor:
        mock_client_constructor.side_effect = lambda service_name, *args, **kwargs: MagicMock(
            name=f"MockService_{service_name}"
        )
        yield mock_client_constructor


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
