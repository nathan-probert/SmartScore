"""team_map: abbreviation -> place translation, aliases, and the NYI/NYR collision.

The archive's team_name column stores the place, not the club, so both New York
clubs writing "New York" is correct for a place join and flagged for anything
that needs club identity.
"""

from team_map import TEAM_ABBREV_TO_PLACE, is_place_ambiguous, to_place


def test_new_york_clubs_share_place_but_only_those_collide():
    assert to_place("NYI") == "New York"
    assert to_place("NYR") == "New York"
    assert to_place("NJD") == "New Jersey"

    assert is_place_ambiguous("NYI")
    assert is_place_ambiguous("NYR")
    assert not is_place_ambiguous("NJD")
    assert not is_place_ambiguous("BOS")


def test_every_mapped_club_has_a_place():
    assert len(TEAM_ABBREV_TO_PLACE) == 33
    assert all(place for place in TEAM_ABBREV_TO_PLACE.values())


def test_abbreviation_aliases_resolve_before_lookup():
    assert to_place("LA") == "Los Angeles"
    assert to_place("SJ") == "San Jose"
    assert not is_place_ambiguous("LA")
    assert not is_place_ambiguous("SJ")


def test_lookup_is_case_insensitive():
    assert to_place("la") == "Los Angeles"
    assert to_place("bos") == "Boston"
    assert is_place_ambiguous("nyi")


def test_montreal_keeps_accent():
    assert to_place("MTL") == "Montréal"


def test_ari_and_uta_both_present_across_the_relocation():
    assert to_place("ARI") == "Arizona"
    assert to_place("UTA") == "Utah"


def test_unknown_and_empty_return_none():
    assert to_place("XYZ") is None
    assert to_place(None) is None
    assert to_place("") is None
    assert not is_place_ambiguous(None)
    assert not is_place_ambiguous("XYZ")
