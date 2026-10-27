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


@patch("utility.get_events_client")
@patch("utility.get_sts_client")
@patch("utility.get_ssm_client")
@patch("utility.boto3.session.Session")
def test_schedule_run_second_target_is_the_ml_picks_kick(
    mock_session, mock_ssm_client, mock_sts_client, mock_events_client
):
    """Architecture A: one rule, two targets - state machine + MLPicksKick.

    The kick rides on the same per-game TriggerStateMachineAt_* rule
    (EventBridge multi-target), stamped with the slate date in
    America/Toronto and dry off; the relay Lambda ARN is namespace-local.
    """
    times = ["2026-01-15T00:30:00Z"]  # game start 00:30 UTC -> trigger at 00:35 UTC

    mock_session.return_value.region_name = "us-east-1"
    mock_sts_client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    mock_ssm_client.return_value.get_parameter.return_value = {"Parameter": {"Value": "role-arn"}}

    rule_name = "TriggerStateMachineAt_202601150035-dev"
    schedule_run(times)

    mock_events_client.return_value.put_rule.assert_called_once_with(
        Name=rule_name,
        ScheduleExpression="cron(35 0 15 1 ? 2026)",
        State="ENABLED",
    )

    targets = mock_events_client.return_value.put_targets.call_args[1]["Targets"]
    assert [t["Id"] for t in targets] == ["1", "mlPicksKickTarget"]
    kick = targets[1]
    assert kick["Arn"] == "arn:aws:lambda:us-east-1:123456789012:function:MLPicksKick-dev"
    # No RoleArn on the Lambda target - the invoke is granted by the
    # template's MLPicksKickFunctionPermission (SourceArn scoped to this
    # rule family, same pattern the removed MLPicksFunctionPermission used).
    assert "RoleArn" not in kick
    kick_payload = json.loads(kick["Input"])
    assert kick_payload == {
        "source": "eventBridge",
        "kind": "mlPicksKick",
        "date": "2026-01-14",  # 00:35 UTC = 19:35 ET on the previous day
        "dry": False,
    }
