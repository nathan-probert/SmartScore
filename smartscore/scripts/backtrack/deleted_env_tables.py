"""Names of the dev backtracking tables that were deleted, with a fail-loud guard.

All three dev backtrack snapshots are now gone (row-deleted, table shells remain):

- ``Player-Snapshots-backtrack-dev`` (617,310 rows) on 2026-10-24 — superseded by
  ``Player-Snapshots-backtrack-prod`` (identical (date, player_id) key sets over
  the full dev range; 2,000-row value equality incl. all backfilled shot/TOI
  columns; prod's extra 6,048 rows are exactly the 2026-27 season).
- ``Team-Stats-backtrack-dev`` (34,300 rows) on 2026-10-24 — superseded by
  ``Team-Stats-backtrack-prod`` (identical (season, team_abbrev, game_id) key
  sets over the full dev range; 500-row value equality; prod's extra 336 rows
  are exactly the 2026-27 season).
- ``Goalie-Snapshots-backtrack-dev`` (36,696 rows) on 2026-10-24 — superseded by
  ``Goalie-Snapshots-backtrack-prod`` (identical (season, player_id, date) key
  sets over the full dev range; 500-row value equality; prod's extra 348 rows
  are exactly the 2026-27 season).

Anything that still resolves one of these names must fail with an explicit
pointer to ``--env prod`` / ``ENV=prod`` rather than surfacing a confusing
PostgREST 404 / empty result.
"""

DELETED_BACKTRACK_TABLES = {
    "Player-Snapshots-backtrack-dev",
    "Team-Stats-backtrack-dev",
    "Goalie-Snapshots-backtrack-dev",
}


def guard_backtrack_table(table: str) -> str:
    """Return ``table`` unchanged, or raise if it names a deleted dev table.

    Use it wherever a backtrack table name is resolved from an env suffix, so
    a stale ``--env dev`` / ``ENV=dev`` run fails loudly instead of hitting a
    PostgREST 404 on the dropped table.
    """
    if table in DELETED_BACKTRACK_TABLES:
        raise RuntimeError(
            f"{table} no longer exists: the dev backtracking snapshot was deleted"
            " after the matching -prod table was verified to supersede it (same"
            " key set over the full dev range 2013-10-01..2026-06-14; prod's extra"
            " rows are exactly the 2026-27 season). Re-run with ENV=prod /"
            " --env prod. Historical dev backups live in data/backups/"
            " (see MANIFEST.md); see docs/backtrack_dev_deleted.md."
        )
    return table
