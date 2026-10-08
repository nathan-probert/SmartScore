"""NHL team abbreviation to the place names used by Player-Snapshots-{ENV}.

``team_name`` in the archive is the NHL schedule place name ("New York", "St.
Louis", "Montréal"), not the abbreviation the game log reports ("NYR", "STL").
The game log has no place name, so the mapping has to live somewhere; this is it.

Two entries need comment:

* **NYR and NJD both map to "New York".** The archive field is genuinely coarse
  here - it is the place, not the club - so both clubs writing "New York" is
  correct behaviour for a join on place, and wrong for anything that needs to tell
  the teams apart. :func:`is_place_ambiguous` flags this so a caller can decide.
* **Montréal keeps its accent.** That matches the stored values, so it is a
  literal copy from the archive rather than a corrected spelling.

The map covers the 33 teams that can appear in a game log. Historical franchises
(SFG, CHI-eraWinnipeg, VAN-era Hartford) are out of scope: the backtrack range is
2023-24 onward, and the game log's ``teamAbbrev`` is always a current club.
"""

TEAM_ABBREV_TO_PLACE = {
    # ARI was the Coyotes through 2024-25 and relocated to Utah as UTA, which is
    # also in this map. Both spellings appear in 2023-24 game logs, so a range that
    # spans the move needs both. Omitting one silently nulls every row involving
    # that club rather than erroring, which is how ARI went missing once already.
    "ARI": "Arizona",
    "ANA": "Anaheim",
    "BOS": "Boston",
    "BUF": "Buffalo",
    "CGY": "Calgary",
    "CAR": "Carolina",
    "CHI": "Chicago",
    "COL": "Colorado",
    "CBJ": "Columbus",
    "DAL": "Dallas",
    "DET": "Detroit",
    "EDM": "Edmonton",
    "FLA": "Florida",
    "LAK": "Los Angeles",
    "MIN": "Minnesota",
    "MTL": "Montréal",
    "NSH": "Nashville",
    "NJD": "New Jersey",
    "NYI": "New York",
    "NYR": "New York",
    "OTT": "Ottawa",
    "PHI": "Philadelphia",
    "PIT": "Pittsburgh",
    "SEA": "Seattle",
    "SJS": "San Jose",
    "STL": "St. Louis",
    "TBL": "Tampa Bay",
    "TOR": "Toronto",
    "UTA": "Utah",
    "VAN": "Vancouver",
    "VGK": "Vegas",
    "WPG": "Winnipeg",
    "WSH": "Washington",
}

# Clubs that share a place name with another club in the map above. A caller that
# needs club identity rather than place identity should not trust the mapping for
# these.
_PLACE_COLLISIONS = {"NYR", "NJD"}

# "SJS" is San Jose in the abbreviations used by the archived rows. It is kept here
# explicitly rather than derived, because the abbreviation set has churned over the
# years (LAK/LA, SJS/SJ, VGK/VGK) and the archive is keyed on the stored spelling.
_ABBREV_ALIASES = {
    "LA": "LAK",
    "SJ": "SJS",
}


def to_place(abbrev):
    """Translate a game-log ``teamAbbrev`` to an archive ``team_name``.

    Returns None for an unknown abbreviation rather than guessing. A missing team
    name is visible and fixable; a wrong one silently corrupts a join.
    """
    if not abbrev:
        return None

    abbrev = str(abbrev).upper()
    abbrev = _ABBREV_ALIASES.get(abbrev, abbrev)

    return TEAM_ABBREV_TO_PLACE.get(abbrev)


def is_place_ambiguous(abbrev):
    """True if this abbreviation's place name is shared with another club."""
    if not abbrev:
        return False

    abbrev = str(abbrev).upper()

    return _ABBREV_ALIASES.get(abbrev, abbrev) in _PLACE_COLLISIONS