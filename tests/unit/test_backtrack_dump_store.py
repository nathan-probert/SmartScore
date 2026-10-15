"""dump_store._literal: SQL literal rendering, including quote escaping."""

import dump_store
import local_store
from dump_store import _literal


def test_none_renders_as_null():
    assert _literal(None) == "NULL"


def test_int_and_bool_render_as_numbers():
    assert _literal(42) == "42"
    assert _literal(0) == "0"
    assert _literal(-7) == "-7"
    # bools hit the int branch but str() them; SQLite accepts TRUE as a keyword.
    assert _literal(True) == "True"


def test_float_renders_as_repr():
    assert _literal(1.5) == repr(1.5)
    assert _literal(0.320755) == repr(0.320755)


def test_strings_are_quoted_with_doubled_quotes():
    assert _literal("BOS") == "'BOS'"
    assert _literal("O'Brien") == "'O''Brien'"
    assert _literal("") == "''"
    assert _literal("it's a 'test'") == "'it''s a ''test'''"


def test_every_dump_table_exists_and_carries_a_season_column(tmp_path):
    """The per-season export filters WHERE season = ? on every dumped table.

    The contract is comment, not code: a table added to _ALL_TABLES without a
    season column would export nothing for all seasons and skip silently.
    """
    conn = local_store.connect(tmp_path / "raw.sqlite")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert set(dump_store._ALL_TABLES) <= names
    for table in dump_store._ALL_TABLES:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert "season" in columns, table

    # Derived tables are cleared by --load but never dumped - they are what
    # --derive recomputes, and shipping them would freeze a formula into SQL.
    for table in dump_store._DERIVED_TABLES:
        assert table not in dump_store._ALL_TABLES
    conn.close()
