import json
from unittest.mock import patch

from utility import schedule_run


@patch("utility.get_events_client")
@patch("utility.get_sts_client")
@patch("utility.get_ssm_client")
@patch("utility.boto3.session.Session")
def test_schedule_run_last_game_true(mock_session, mock_ssm_client, mock_sts_client, mock_events_client):
    times = [
        "2026-01-15T03:00:00Z",  # +5 min -> 03:05
        "2026-01-15T00:00:00Z",  # +5 min -> 00:05
        "2026-01-15T00:30:00Z",  # +5 min -> 00:35
    ]
    expected_rule_names = [
        "TriggerStateMachineAt_202601150005-dev",
        "TriggerStateMachineAt_202601150035-dev",
        "TriggerStateMachineAt_202601150305-dev",
    ]

    # Mock region/account
    mock_session.return_value.region_name = "us-east-1"
    mock_sts_client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    mock_ssm_client.return_value.get_parameter.return_value = {"Parameter": {"Value": "role-arn"}}

    put_targets_calls = []
    mock_events_client.return_value.put_targets.side_effect = lambda Rule, Targets: put_targets_calls.append(
        (Rule, Targets)
    )

    schedule_run(times)
    assert len(put_targets_calls) == 3
    # After sorting, the order should be: 00:05, 00:35, 03:05
    for i, (rule_name, targets) in enumerate(put_targets_calls):
        assert rule_name == expected_rule_names[i]
        payload_dict = json.loads(targets[0]["Input"])
        if i == 2:
            assert payload_dict["last_game"] is True
        else:
            assert payload_dict["last_game"] is False
        assert payload_dict["source"] == "eventBridge"
