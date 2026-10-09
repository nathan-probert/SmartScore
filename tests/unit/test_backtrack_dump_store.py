"""dump_store._literal: SQL literal rendering, including quote escaping."""

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
