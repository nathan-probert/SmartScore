#!/usr/bin/env python3
"""One-off port of the MongoDB player archive into Supabase ``Player-Snapshots-{ENV}`` (#113).

The Cloudflare ``smartscore-api`` worker wrote every day's full roster into
MongoDB with ``insertMany`` and no unique constraint, so re-uploading a date
appended a second (and third, ...) copy of the whole roster. Those copies are
the source data here; ``smartscore/player_archive.py`` writes the same archive
into Supabase with an upsert on ``(date, player_id)``, so the port collapses the
duplicates as it goes.

Shape mapping:

===============  ==========================================================
Mongo            Supabase ``Player-Snapshots-{ENV}``
===============  ==========================================================
``_id``          dropped - Mongo's own surrogate key
``id``           ``player_id`` (BIGINT); the NHL player id, now the PK half
``date``         ``date`` (TEXT, unchanged)
``scored``       ``scored`` (INTEGER): ``true``->1, ``false``->0, absent->null
everything else  projected onto ``player_archive.SNAPSHOT_COLUMNS``
===============  ==========================================================

Rows are projected onto the known columns rather than passed through, because
PostgREST rejects the *entire* batch on an unknown column and the collection
also carries the retired ``stat`` field.

Deduplication rule (deterministic, so a re-run picks the same rows):

1. Prefer a copy whose ``scored`` is set over one where it is absent. Only a
   graded copy carries the training label, and the worker's two ``updateMany``
   calls graded *every* duplicate of a ``(date, id)`` pair at once, so
   duplicates never disagree about the outcome.
2. Then prefer the most recently inserted copy, using the creation timestamp
   embedded in the leading 4 bytes of the Mongo ``ObjectId``.
3. Then break any remaining tie on the raw ``_id`` string, so the result never
   depends on cursor or filesystem ordering.

Every step is a plain comparison over a single date's documents, which keeps
peak memory to one date rather than the whole archive.

Usage (credentials come from the environment only; see below - never commit a
URI or a ``.env``)::

    # Always start here: reports counts and sample rows, writes nothing.
    uv run python smartscore/scripts/port_mongo_snapshots.py --dry-run

    # Then, once the counts look right and you have the go-ahead:
    uv run python smartscore/scripts/port_mongo_snapshots.py

The port is idempotent - it upserts on the ``(date, player_id)`` primary key -
so a partial or repeated run converges on the same result. Rows already graded
in Supabase by ``backfill_scored`` are only ever overwritten by this script
before that grading happens, which is why ``scored`` *is* included in the
payload here (``save_player_snapshots`` deliberately leaves it out so a daily
re-upload cannot wipe a backfill).

Required environment variables:

``ENV``
    ``dev`` or ``prod``. Selects both the Mongo collection and the Supabase
    table, matching the worker. Defaults to ``dev``.
``MONGODB_URI``
    Same secret the worker reads as ``MONGODB_URI``.
``SUPABASE_URL`` / ``SUPABASE_SERVICE_ROLE_KEY``
    Read by ``config.py``, exactly as at Lambda runtime. ``load_dotenv()`` picks
    up a local (gitignored) ``.env``.
``MONGODB_DATABASE``
    Optional. Defaults to ``players``, the database the worker hardcoded.
"""

import argparse
import os
import sys

from bson import ObjectId
from pymongo import MongoClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aws_lambda_powertools import Logger  # noqa: E402

from config import ENV, SUPABASE_ADMIN_CLIENT  # noqa: E402

# _retry is private to player_archive, but re-implementing the backoff here
# would let the two drift; importing it keeps one policy for "retry a
# transport error, re-raise a schema rejection" across the whole archive path.
from player_archive import (  # noqa: E402
    REQUIRED_FIELDS,
    SNAPSHOT_COLUMNS,
    SNAPSHOT_TABLE,
    _retry,
)

logger = Logger()


def mongo_collection_name(env):
    """The collection the worker wrote to for a given environment.

    ``SmartScoreDev`` is deliberate, not a typo: it is what
    ``getPlayersCollection`` in smartscore-api selected for anything that was not
    ``prod``.
    """
    return "SmartScore" if env == "prod" else "SmartScoreDev"


# Same database/collection split as the worker's getPlayersCollection.
MONGO_DATABASE = "players"
MONGO_COLLECTION = mongo_collection_name(ENV)

# PostgREST has no server-side batch limit we can lean on - postgrest-py sends
# the whole JSON array in one request - so the batch size is ours to pick. 500
# keeps a request body comfortably small (a full roster row is ~30 columns) and
# stays under the 1000-row default of PostgREST's db-max-rows, so the same
# figure stays safe if we ever switch the write to returning=representation.
BATCH_SIZE = 500

SAMPLE_SIZE = 3


class PortStats:
    """Counters reported at the end of a run (and printed by --dry-run)."""

    def __init__(self):
        self.documents_scanned = 0
        self.rows_written = 0
        self.duplicates_collapsed = 0
        self.dates_with_duplicates = 0
        self.rows_skipped = 0
        self.dates_seen = 0

    def as_dict(self):
        return {
            "dates": self.dates_seen,
            "documents_scanned": self.documents_scanned,
            "rows": self.rows_written,
            "duplicates_collapsed": self.duplicates_collapsed,
            "dates_with_duplicates": self.dates_with_duplicates,
            "rows_skipped": self.rows_skipped,
        }


def coerce_scored(value):
    """Map Mongo's boolean/absent ``scored`` onto the archive's INTEGER.

    ``None`` (ungraded) stays ``None``: the archive distinguishes "not graded
    yet" from "did not score", and a false-y default would quietly relabel every
    ungraded player as a negative example.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring non-numeric scored value: {value!r}")
        return None


def coerce_player_id(value):
    """Return the NHL player id as an int, or ``None`` if it is not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _NewestFirst:
    """Comparable wrapper that sorts ObjectIds newest-first.

    An ObjectId's leading four bytes are a creation timestamp, so ordering by it
    descending is "most recently inserted". A non-ObjectId ``_id`` sorts after
    every ObjectId on a fixed flag rather than raising on an incomparable pair,
    which keeps a mixed collection deterministic too.
    """

    def __init__(self, value):
        self._key = (0, value) if isinstance(value, ObjectId) else (1, str(value))

    def __lt__(self, other):
        return other._key < self._key

    def __eq__(self, other):
        return isinstance(other, _NewestFirst) and self._key == other._key

    def __hash__(self):
        return hash(self._key)


def _sort_key(document):
    """Ordering key implementing the dedupe rule from the module docstring.

    Lower sorts first and wins, so: graded before ungraded, then newest ``_id``
    before oldest, then ``_id`` ascending purely as a final tie-break.
    """
    graded = document.get("scored") is not None
    return (0 if graded else 1, _NewestFirst(document.get("_id")), str(document.get("_id")))


def pick_winner(documents):
    """Return the single document to keep from a ``(date, player_id)`` group.

    See the module docstring for the rule.
    """
    return min(documents, key=_sort_key)


def dedupe_documents(documents):
    """Collapse duplicate rosters for one date.

    Returns ``(winners, duplicates_collapsed, unkeyed)``: a mapping of
    ``player_id`` to the surviving document, how many copies were dropped, and
    how many documents had no usable ``id``.
    """
    grouped = {}
    unkeyed = []
    for document in documents:
        player_id = coerce_player_id(document.get("id"))
        if player_id is None:
            unkeyed.append(document)
            continue
        grouped.setdefault(player_id, []).append(document)

    winners = {}
    for player_id, group in grouped.items():
        if len(group) > 1:
            logger.warning(f"Collapsed {len(group)} copies of player {player_id}")
        winners[player_id] = pick_winner(group)

    if unkeyed:
        logger.warning(f"Skipping {len(unkeyed)} document(s) with a non-numeric id: {[d.get('id') for d in unkeyed]}")

    return winners, sum(len(group) - 1 for group in grouped.values()), len(unkeyed)


def to_snapshot(document, player_id):
    """Project one Mongo document onto the archive's columns.

    Returns ``None`` when a required field is missing, which is logged rather
    than sent as a null the database would reject.
    """
    snapshot = {
        "date": document.get("date"),
        "player_id": player_id,
        "scored": coerce_scored(document.get("scored")),
    }
    for column in SNAPSHOT_COLUMNS:
        snapshot[column] = document.get(column)

    if any(snapshot.get(field) is None for field in REQUIRED_FIELDS):
        logger.warning(f"Skipping Mongo document missing a required field: {document.get('name')!r}")
        return None

    return snapshot


def flush(rows, dry_run, stats):
    """Upsert one batch, or count it when running as a dry run."""
    if dry_run:
        stats.rows_written += len(rows)
        return

    _retry(
        lambda: (
            SUPABASE_ADMIN_CLIENT.table(SNAPSHOT_TABLE)
            .upsert(rows, on_conflict="date,player_id", returning="minimal")
            .execute()
        ),
        f"Porting {len(rows)} player snapshot(s)",
    )
    stats.rows_written += len(rows)


def list_dates(collection, only_date):
    """Return the sorted dates to port.

    One date at a time keeps peak memory to a single day's duplicates rather
    than the whole archive, and gives natural upsert batches.
    """
    if only_date:
        return [only_date]

    dates = collection.distinct("date", {"date": {"$type": "string"}})
    return sorted(dates)


def port_collection(collection, dry_run, only_date=None, batch_size=BATCH_SIZE, stats=None):
    """Port every deduped Mongo document for the given dates into Supabase."""
    stats = stats or PortStats()
    sample = []

    for date in list_dates(collection, only_date):
        stats.dates_seen += 1
        documents = list(collection.find({"date": date}))
        if not documents:
            continue

        stats.documents_scanned += len(documents)
        winners, duplicates, unkeyed = dedupe_documents(documents)
        stats.duplicates_collapsed += duplicates
        stats.rows_skipped += unkeyed
        if duplicates:
            stats.dates_with_duplicates += 1

        rows = []
        for player_id, document in sorted(winners.items()):
            snapshot = to_snapshot(document, player_id)
            if snapshot is None:
                stats.rows_skipped += 1
                continue
            rows.append(snapshot)
            if len(sample) < SAMPLE_SIZE:
                sample.append(snapshot)

        for offset in range(0, len(rows), batch_size):
            flush(rows[offset : offset + batch_size], dry_run, stats)

    return stats, sample


def report(stats, sample, dry_run):
    verb = "Would port" if dry_run else "Ported"
    print(f"\n{'=' * 70}\n{verb} into {SNAPSHOT_TABLE}\n{'=' * 70}")
    for key, value in stats.as_dict().items():
        print(f"  {key:>24}: {value}")
    if sample:
        print(f"\nSample rows ({SAMPLE_SIZE}):")
        for row in sample:
            printable = {key: row[key] for key in ("date", "player_id", "name", "scored", "team_name")}
            print(f"  {printable}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts and sample rows without writing anything to Supabase.",
    )
    parser.add_argument(
        "--date",
        help="Port a single YYYY-MM-DD date instead of the whole collection.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Rows per Supabase upsert (default: {BATCH_SIZE}).",
    )
    args = parser.parse_args(argv)

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        parser.error("MONGODB_URI is not set. Export it (and SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY) first.")

    database = os.environ.get("MONGODB_DATABASE", MONGO_DATABASE)
    logger.info(f"Porting {ENV} Mongo {database}.{MONGO_COLLECTION} into {SNAPSHOT_TABLE} (dry_run={args.dry_run})")

    client = MongoClient(uri)
    try:
        collection = client[database][MONGO_COLLECTION]
        stats, sample = port_collection(
            collection,
            dry_run=args.dry_run,
            only_date=args.date,
            batch_size=args.batch_size,
        )
    finally:
        client.close()

    report(stats, sample, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
