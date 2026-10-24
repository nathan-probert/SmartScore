"""Guards for the deleted dev backtrack tables (deleted_env_tables.py)."""

import sys
from unittest import mock  # noqa: F401

import pytest

sys.path.insert(0, "smartscore/scripts/backtrack")

from deleted_env_tables import DELETED_BACKTRACK_TABLES, guard_backtrack_table


def test_dev_player_table_named_in_error():
    with pytest.raises(RuntimeError, match="Player-Snapshots-backtrack-dev"):
        guard_backtrack_table("Player-Snapshots-backtrack-dev")


def test_error_points_to_prod():
    with pytest.raises(RuntimeError, match="ENV=prod"):
        guard_backtrack_table("Player-Snapshots-backtrack-dev")


def test_deleted_registry_only_lists_verified_tables():
    assert DELETED_BACKTRACK_TABLES == {"Player-Snapshots-backtrack-dev"}


def test_prod_tables_pass_through():
    assert (
        guard_backtrack_table("Player-Snapshots-backtrack-prod")
        == "Player-Snapshots-backtrack-prod"
    )
