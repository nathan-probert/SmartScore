"""Names of the dev backtracking tables that were deleted, with a fail-loud guard.

``Player-Snapshots-backtrack-dev`` was deleted on 2026-10-24 because
``Player-Snapshots-backtrack-prod`` superseded it (row counts, identical
(date, player_id) key sets over the full dev range, and 2,000-row value
equality including every backfilled shot/TOI column). Anything that still
resolves that name must fail with an explicit pointer to ``--env prod`` /
``ENV=prod`` rather than surfacing a confusing PostgREST 404.
"""

DELETED_BACKTRACK_TABLES = {"Player-Snapshots-backtrack-dev"}


def guard_backtrack_table(table: str) -> str:
    """Return ``table`` unchanged, or raise if it names a deleted dev table.

    Use it wherever a backtrack table name is resolved from an env suffix, so
    a stale ``--env dev`` / ``ENV=dev`` run fails loudly instead of hitting a
    PostgREST 404 on the dropped table.
    """
    if table in DELETED_BACKTRACK_TABLES:
        raise RuntimeError(
            f"{table} no longer exists: the dev backtracking snapshot was deleted"
            " after Player-Snapshots-backtrack-prod was verified to supersede it"
            " (same (date, player_id) key set over the full dev range 2013-10-01.."
            "2026-06-14; 2,000-row value equality incl. all backfilled shot/TOI"
            " columns). Re-run with ENV=prod / --env prod. Historical dev backups"
            " live in data/backups/ (see MANIFEST.md)."
        )
    return table
