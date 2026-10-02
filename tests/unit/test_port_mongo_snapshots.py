"""Unit tests for the one-off Mongo -> Supabase port (#113).

Exercised against a fake Mongo collection rather than a live cluster: the parts
worth testing are the dedupe rule, the column projection and the batching, none
of which need a server. A real run still needs the operator's credentials - see
the script's docstring - so this is deliberately not an integration test.
"""

import sys
from pathlib import Path

import pytest
from bson import ObjectId

from player_archive import SNAPSHOT_COLUMNS

# The script lives in smartscore/scripts and appends its parent to sys.path
# itself, but only when run as __main__'s sibling import; add it explicitly so
# the import works the same way the other scripts in that directory do.
sys.path.append(str(Path(__file__).resolve().parents[2] / "smartscore" / "scripts"))

import port_mongo_snapshots as port  # noqa: E402


class FakeCollection:
    """The slice of pymongo's Collection the port actually touches.

    Only the equality and ``$type`` matchers the port uses are understood, which
    is enough: everything else about this script is plain Python over the
    documents it returns.
    """

    def __init__(self, documents):
        self.documents = documents
        self.find_calls = []

    def distinct(self, field, query):
        # Mongo's distinct returns each value once; the fake has to as well, or
        # every date would be processed once per document.
        values = {document[field] for document in self.documents if self._matches(document, query)}
        return list(values)

    def find(self, query):
        self.find_calls.append(query)
        return [document for document in self.documents if self._matches(document, query)]

    @staticmethod
    def _matches(document, query):
        for key, condition in query.items():
            value = document.get(key)
            if isinstance(condition, dict) and "$type" in condition:
                actual = "string" if isinstance(value, str) else type(value).__name__
                if actual != condition["$type"]:
                    return False
            elif value != condition:
                return False
        return True


def mongo_document(date, player_id, name="Player One", scored=None, seconds=0, seq=0, **extra):
    """A document shaped like the worker's insertMany payload.

    ``_id`` is built the way Mongo builds one: a leading creation timestamp
    (``seconds``) followed by per-process random bytes, which ``seq`` stands in
    for so two documents never collide.
    """
    oid_player = player_id if isinstance(player_id, int) else 0
    return {
        "_id": ObjectId(f"{seconds:08x}{oid_player:08x}{seq:08x}"),
        "date": date,
        "id": player_id,
        "name": name,
        "scored": scored,
        "team_name": "Toronto",
        "gpg": 0.5,
        "stat": 0.61,
        **extra,
    }


def test_coerce_scored_maps_booleans_and_absence():
    """Absent must stay null: it means "not graded", not "did not score"."""
    assert port.coerce_scored(True) == 1
    assert port.coerce_scored(False) == 0
    assert port.coerce_scored(None) is None
    assert port.coerce_scored(1) == 1
    assert port.coerce_scored("not a number") is None


def test_coerce_player_id_rejects_non_numeric():
    assert port.coerce_player_id(8478402) == 8478402
    assert port.coerce_player_id("8478402") == 8478402
    assert port.coerce_player_id("unknown") is None
    assert port.coerce_player_id(None) is None
    # bool is an int subclass; a "true" player id is corrupt, not 1.
    assert port.coerce_player_id(True) is None


def test_dedupe_prefers_the_graded_copy():
    """Only a graded copy carries the training label."""
    ungraded = mongo_document("2026-04-15", 1, name="Older ungraded", seconds=100)
    graded = mongo_document("2026-04-15", 1, name="Graded", scored=True, seconds=200)

    winners, duplicates, unkeyed = port.dedupe_documents([ungraded, graded])

    assert duplicates == 1
    assert unkeyed == 0
    assert winners[1]["name"] == "Graded"


def test_dedupe_prefers_the_most_recently_written_copy():
    """With both copies ungraded, the newest insert wins."""
    older = mongo_document("2026-04-15", 1, name="Older", seconds=100)
    newer = mongo_document("2026-04-15", 1, name="Newer", seconds=200)

    winners, _, _ = port.dedupe_documents([older, newer])

    assert winners[1]["name"] == "Newer"
    # ...and the choice must not depend on the order they arrive in.
    assert port.dedupe_documents([newer, older])[0][1]["name"] == "Newer"


def test_dedupe_prefers_graded_over_newer_ungraded():
    """Graded beats recent: losing the label is worse than losing a field refresh."""
    graded_older = mongo_document("2026-04-15", 1, name="Graded", scored=False, seconds=100)
    ungraded_newer = mongo_document("2026-04-15", 1, name="Ungraded", seconds=900)

    winners, _, _ = port.dedupe_documents([ungraded_newer, graded_older])

    assert winners[1]["name"] == "Graded"


def test_dedupe_is_deterministic_for_identical_timestamps():
    """Same timestamp on both copies: the raw _id breaks the tie, not the order."""
    first = mongo_document("2026-04-15", 1, name="First", seq=0)
    second = mongo_document("2026-04-15", 1, name="Second", seq=1)

    winners_forward, _, _ = port.dedupe_documents([first, second])
    winners_reverse, _, _ = port.dedupe_documents([second, first])

    # Newest-first still applies, so the higher _id wins - but crucially the same
    # one does, whichever order the cursor happened to return them in.
    assert winners_forward[1]["name"] == winners_reverse[1]["name"] == "Second"


def test_dedupe_counts_and_drops_documents_without_a_usable_id():
    documents = [
        mongo_document("2026-04-15", 1),
        mongo_document("2026-04-15", 1, seconds=100),
        mongo_document("2026-04-15", "unknown"),
    ]

    winners, duplicates, unkeyed = port.dedupe_documents(documents)

    assert duplicates == 1
    assert unkeyed == 1
    assert list(winners) == [1]


def test_to_snapshot_projects_onto_known_columns():
    """PostgREST rejects the whole batch on an unknown column."""
    snapshot = port.to_snapshot(mongo_document("2026-04-15", 8478402, scored=True), 8478402)

    assert set(snapshot) == {"date", "player_id", "scored", *SNAPSHOT_COLUMNS}
    assert snapshot["player_id"] == 8478402
    assert snapshot["scored"] == 1
    assert "_id" not in snapshot
    assert "stat" not in snapshot


def test_to_snapshot_skips_a_document_missing_a_required_field():
    assert port.to_snapshot(mongo_document("2026-04-15", 1, name=None), 1) is None


def test_list_dates_ignores_non_string_dates():
    collection = FakeCollection(
        [
            mongo_document("2026-04-15", 1),
            {"date": 20260415, "id": 2},
            mongo_document("2026-04-14", 1),
        ]
    )

    assert port.list_dates(collection, None) == ["2026-04-14", "2026-04-15"]
    assert port.list_dates(collection, "2026-04-15") == ["2026-04-15"]


def test_dry_run_reports_counts_without_writing(monkeypatch):
    """--dry-run must not touch Supabase at all."""
    writes = []
    monkeypatch.setattr(port, "_retry", lambda operation, description: writes.append(description))
    collection = FakeCollection(
        [
            mongo_document("2026-04-15", 1, scored=True),
            mongo_document("2026-04-15", 1, scored=True, seconds=100),  # duplicate
            mongo_document("2026-04-15", 2, seconds=100),
            mongo_document("2026-04-14", 1, seconds=100),
        ]
    )

    stats, sample = port.port_collection(collection, dry_run=True)

    assert writes == []  # no upsert was even attempted
    assert stats.documents_scanned == 4
    assert stats.rows_written == 3  # 4 documents, 1 collapsed
    assert stats.duplicates_collapsed == 1
    assert stats.dates_with_duplicates == 1
    assert stats.dates_seen == 2
    assert len(sample) == port.SAMPLE_SIZE
    assert {row["player_id"] for row in sample} == {1, 2}


def test_port_upserts_in_batches(monkeypatch):
    batches = []
    monkeypatch.setattr(port, "_retry", lambda operation, description: batches.append(description))
    documents = [mongo_document("2026-04-15", index) for index in range(5)]

    stats, _ = port.port_collection(FakeCollection(documents), dry_run=False, batch_size=2)

    assert len(batches) == 3  # 2 + 2 + 1
    assert stats.rows_written == 5


class FakeTable:
    """Records the table name and the upsert payload the port builds."""

    def __init__(self, name):
        self.name = name
        self.upserts = []

    def upsert(self, rows, **kwargs):
        self.upserts.append((rows, kwargs))
        return self

    def execute(self):
        return None


def test_port_targets_the_archive_primary_key(monkeypatch):
    """The port has to be re-runnable, which means upserting on the PK."""
    table = FakeTable(port.SNAPSHOT_TABLE)
    monkeypatch.setattr(port.SUPABASE_ADMIN_CLIENT, "table", lambda name: table)

    port.port_collection(FakeCollection([mongo_document("2026-04-15", 1)]), dry_run=False)

    assert table.name == port.SNAPSHOT_TABLE
    ((rows, kwargs),) = table.upserts
    assert kwargs["on_conflict"] == "date,player_id"
    assert rows[0]["date"] == "2026-04-15"


def test_report_is_readable_without_a_client(capsys):
    """The summary is the operator's only signal before a real run."""
    stats = port.PortStats()
    stats.dates_seen = 2
    stats.rows_written = 3

    port.report(stats, [{"date": "2026-04-15", "player_id": 1, "name": "A", "scored": 1, "team_name": "Toronto"}], True)

    out = capsys.readouterr().out
    assert "Would port" in out
    assert port.SNAPSHOT_TABLE in out
    assert "2026-04-15" in out


def test_collection_name_matches_the_worker():
    """The worker used SmartScore for prod and SmartScoreDev otherwise."""
    assert port.mongo_collection_name("prod") == "SmartScore"
    assert port.mongo_collection_name("dev") == "SmartScoreDev"
    # Whatever ENV resolves to at import, the module agrees with the rule.
    assert port.MONGO_COLLECTION == port.mongo_collection_name(port.ENV)
    assert port.MONGO_DATABASE == "players"


class FakeDatabase:
    def __init__(self, collection):
        self._collection = collection

    def __getitem__(self, name):
        assert name == port.MONGO_COLLECTION
        return self._collection


class FakeMongoClient:
    def __init__(self, documents):
        self.collection = FakeCollection(documents)
        self.closed = False

    def __getitem__(self, name):
        assert name == port.MONGO_DATABASE
        return FakeDatabase(self.collection)

    def close(self):
        self.closed = True


def test_main_dry_run_never_opens_a_supabase_write(monkeypatch, capsys):
    """The whole CLI path, exercised the way an operator would first run it."""
    fake = FakeMongoClient(
        [
            mongo_document("2026-04-15", 1, scored=True),
            mongo_document("2026-04-15", 1, scored=True, seconds=100),
            mongo_document("2026-04-15", 2, seconds=100),
        ]
    )
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.invalid")
    monkeypatch.setattr(port, "MongoClient", lambda uri: fake)
    monkeypatch.setattr(
        port.SUPABASE_ADMIN_CLIENT,
        "table",
        lambda name: pytest.fail(f"dry run must not write; tried {name}"),
    )

    assert port.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "duplicates_collapsed: 1" in out
    assert "rows: 2" in out
    assert fake.closed


def test_main_requires_a_mongo_uri(monkeypatch):
    """Credentials come from the environment; there is no fallback URI."""
    monkeypatch.delenv("MONGODB_URI", raising=False)

    with pytest.raises(SystemExit):
        port.main([])
