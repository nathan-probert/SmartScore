"""Guards for the deleted dev backtrack tables (deleted_env_tables.py)."""

import sys
from unittest import mock  # noqa: F401

import pytest

sys.path.insert(0, "smartscore/scripts/backtrack")

from deleted_env_tables import DELETED_BACKTRACK_TABLES, guard_backtrack_table


def test_dev_tables_named_in_error():
    for table in DELETED_BACKTRACK_TABLES:
        with pytest.raises(RuntimeError, match=table.replace("-", "\\-")):
            guard_backtrack_table(table)


def test_error_points_to_prod():
    for table in DELETED_BACKTRACK_TABLES:
        with pytest.raises(RuntimeError, match="ENV=prod"):
            guard_backtrack_table(table)


def test_deleted_registry_only_lists_verified_tables():
    assert DELETED_BACKTRACK_TABLES == {
        "Player-Snapshots-backtrack-dev",
        "Team-Stats-backtrack-dev",
        "Goalie-Snapshots-backtrack-dev",
    }


def test_prod_tables_pass_through():
    for prod in (
        "Player-Snapshots-backtrack-prod",
        "Team-Stats-backtrack-prod",
        "Goalie-Snapshots-backtrack-prod",
    ):
        assert guard_backtrack_table(prod) == prod
