from unittest.mock import patch

import pytest

from decorators import lambda_handler_error_responder


def test_lambda_handler_error_responder_success():
    """Test decorator when function executes successfully."""

    @lambda_handler_error_responder
    def successful_handler(event, context):
        return {"statusCode": 200, "body": "Success"}

    result = successful_handler({}, {})

    assert result == {"statusCode": 200, "body": "Success"}


def test_lambda_handler_error_responder_with_exception():
    """Test decorator when function raises an exception."""

    @lambda_handler_error_responder
    def failing_handler(event, context):
        raise ValueError("Test error")

    with pytest.raises(ValueError, match="Test error"):
        failing_handler({}, {})


@patch("decorators.logger")
def test_lambda_handler_error_responder_logs_error(mock_logger):
    """Test that decorator logs the error before re-raising."""

    @lambda_handler_error_responder
    def failing_handler(event, context):
        raise RuntimeError("Critical error")

    with pytest.raises(RuntimeError):
        failing_handler({}, {})

    # Verify that logger.error was called
    mock_logger.error.assert_called_once()
    error_message = mock_logger.error.call_args[0][0]
    assert "Critical error" in error_message
    assert "Traceback" in error_message


def test_lambda_handler_error_responder_preserves_return_value():
    """Test that decorator preserves complex return values."""

    @lambda_handler_error_responder
    def complex_handler(event, context):
        return {
            "statusCode": 200,
            "body": {"players": [{"id": 1, "name": "Player"}], "count": 1},
            "headers": {"Content-Type": "application/json"},
        }

    result = complex_handler({}, {})

    assert result["statusCode"] == 200
    assert result["body"]["count"] == 1
    assert len(result["body"]["players"]) == 1


def test_lambda_handler_error_responder_with_event_context():
    """Test that decorator passes event and context correctly."""

    @lambda_handler_error_responder
    def handler_with_params(event, context):
        return {"event_data": event.get("key"), "context_data": context.get("request_id")}

    event = {"key": "value"}
    context = {"request_id": "12345"}

    result = handler_with_params(event, context)

    assert result["event_data"] == "value"
    assert result["context_data"] == "12345"
