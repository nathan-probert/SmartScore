import smtplib
from unittest.mock import patch

import pytest

from email_utility import send_email

PICKS = [{"name": "Player 1", "stat": 0.9, "tims": 1}]


@patch("email_utility.is_feature_enabled", return_value=True)
@patch("email_utility.smtplib.SMTP")
def test_send_email_reraises_smtp_auth_failure(mock_smtp, mock_feature_enabled):
    """SMTP failures are logged and re-raised so send_emails can count them."""
    mock_smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")

    with pytest.raises(smtplib.SMTPAuthenticationError, match="5.7.8"):
        send_email("to@example.com", PICKS, "Tester", "2026-04-16")

    mock_smtp.return_value.login.assert_called_once()


@patch("email_utility.is_feature_enabled", return_value=True)
@patch("email_utility.smtplib.SMTP")
def test_send_email_sends_on_success(mock_smtp, mock_feature_enabled):
    server = mock_smtp.return_value

    send_email("to@example.com", PICKS, "Tester", "2026-04-16")

    server.starttls.assert_called_once()
    server.send_message.assert_called_once()
    server.quit.assert_called_once()
