import os
from unittest.mock import MagicMock, patch

import pytest

# Set dummy AWS credentials and config to prevent real AWS calls.
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
    Mocks boto3.client globally for the entire test session.
    Any call to boto3.client() will return a MagicMock.
    """
    with patch("boto3.client") as mock_client_constructor:
        mock_client_constructor.side_effect = lambda service_name, *args, **kwargs: MagicMock(
            name=f"MockService_{service_name}"
        )
        yield mock_client_constructor
