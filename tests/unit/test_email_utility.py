import smtplib
from unittest.mock import patch

import pytest

from email_utility import get_html_and_text, send_email

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


def test_stat_fraction_rendered_as_true_percent_in_text_body():
    """The DB stat is a 0-1 fraction, so 0.9 is 90%, not 0.90%."""
    picks = [
        {"name": "Player 1", "stat": 0.9, "tims": 1},
        {"name": "Player 2", "stat": 0.5, "tims": 2},
    ]

    _, text_body = get_html_and_text(picks)

    assert "| 90.00%" in text_body
    assert "| 50.00%" in text_body


def test_stat_fraction_rendered_as_true_percent_in_html_body():
    """The HTML table path scales the 0-1 fraction by 100 too."""
    picks = [
        {"name": "Player 1", "stat": 0.9, "tims": 1},
        {"name": "Player 2", "stat": 0.5, "tims": 2},
    ]

    html_body, _ = get_html_and_text(picks)

    assert ">90.00%</td>" in html_body
    assert ">50.00%</td>" in html_body


def test_stat_fallbacks_unchanged_for_missing_or_non_numeric_values():
    """None, empty, and non-numeric stats keep the pre-existing fallbacks."""
    picks = [
        {"name": "None Stat", "stat": None, "tims": 1},
        {"name": "Empty Stat", "stat": "", "tims": 2},
        {"name": "Text Stat", "stat": "n/a", "tims": 3},
    ]

    html_body, text_body = get_html_and_text(picks)

    assert "| None\n" in text_body
    assert "| \n" in text_body
    assert "| n/a\n" in text_body

    assert 'font-weight:bold;">None%</td>' in html_body
    assert 'font-weight:bold;"></td>' in html_body
    assert 'font-weight:bold;">n/a%</td>' in html_body
