"""lineup_reconstruct: sweep, clustering, PP detection on synthetic shifts."""

import lineup_reconstruct


def _shift(player_id, team_id, start, end, period=1):
    return {
        "playerId": player_id,
        "teamId": team_id,
        "period": period,
        "startTime": start,
        "endTime": end,
    }


def _game():
    """One period, two teams, two lines + a pair each, one home power play.

    Home (100): L1 = 1,2,3 over 00:00-10:00 (360s EV + 120s PP each); L2 =
    4,5,6 over 10:00-16:00 (360s EV); PP stint for 4 at 01:00-03:00
    alongside 1,2,3,7. D = 7 (00:00-10:00) and 8 (03:00-10:00). Away (200)
    mirrors with 30-35 and 42,43, all shorthanded during the PP.
    """
    shifts = [
        _shift(1, 100, "00:00", "10:00"),
        _shift(2, 100, "00:00", "10:00"),
        _shift(3, 100, "00:00", "10:00"),
        _shift(4, 100, "01:00", "03:00"),
        _shift(4, 100, "10:00", "16:00"),
        _shift(5, 100, "10:00", "16:00"),
        _shift(6, 100, "10:00", "16:00"),
        _shift(7, 100, "00:00", "10:00"),
        _shift(8, 100, "03:00", "10:00"),
        _shift(30, 200, "00:00", "10:00"),
        _shift(31, 200, "00:00", "10:00"),
        _shift(32, 200, "00:00", "10:00"),
        _shift(33, 200, "10:00", "16:00"),
        _shift(34, 200, "10:00", "16:00"),
        _shift(35, 200, "10:00", "16:00"),
        _shift(42, 200, "00:00", "10:00"),
        _shift(43, 200, "03:00", "10:00"),
    ]
    positions = {
        1: "C",
        2: "L",
        3: "R",
        4: "C",
        5: "L",
        6: "R",
        7: "D",
        8: "D",
        30: "C",
        31: "L",
        32: "R",
        33: "C",
        34: "L",
        35: "R",
        42: "D",
        43: "D",
    }
    return shifts, positions


def test_lines_cluster_by_shared_even_strength_toi():
    shifts, positions = _game()
    result = lineup_reconstruct.reconstruct_game(shifts, positions)

    assert result[1]["lineup_unit"] == result[2]["lineup_unit"] == result[3]["lineup_unit"] == "F1"
    assert result[4]["lineup_unit"] == result[5]["lineup_unit"] == result[6]["lineup_unit"] == "F2"
    assert result[1]["lineup_unit"] != result[4]["lineup_unit"]
    # Home L1 carries the PP minutes, so it ranks above L2 despite equal EV time.
    assert result[7]["lineup_unit"] == result[8]["lineup_unit"] == "D1"
    assert result[30]["lineup_unit"] == result[31]["lineup_unit"] == result[32]["lineup_unit"] == "F1"


def test_power_play_unit_is_the_five_skaters_with_pp_time():
    shifts, positions = _game()
    result = lineup_reconstruct.reconstruct_game(shifts, positions)

    for pid in (1, 2, 3, 4, 7):
        assert result[pid]["pp_unit"] == "PP1", pid

    # No second PP unit forms from two skaters; everyone else stays NULL -
    # including the shorthanded side, whose PK time is not PP time.
    for pid in (5, 6, 8, 30, 31, 32, 33, 42, 43):
        assert result[pid]["pp_unit"] is None, pid


def test_status_and_position_group_follow_the_unit():
    shifts, positions = _game()
    result = lineup_reconstruct.reconstruct_game(shifts, positions)

    assert result[1]["lineup_status"] == "ACTUAL"
    assert result[1]["lineup_position_group"] == "F"
    assert result[7]["lineup_position_group"] == "D"


def test_reconstruction_is_deterministic():
    shifts, positions = _game()

    first = lineup_reconstruct.reconstruct_game(shifts, positions)
    second = lineup_reconstruct.reconstruct_game(shifts, positions)

    assert first == second


def test_six_on_five_is_not_a_power_play():
    # Goalie-pulled finish: 5 home skaters vs 6 away - smaller side is 5, so
    # the interval classifies as even and nobody gains a pp_unit.
    shifts = [_shift(pid, 100, "00:00", "01:00") for pid in (50, 51, 52, 53, 54)]
    shifts += [_shift(pid, 200, "00:00", "01:00") for pid in (60, 61, 62, 63, 64, 65)]
    positions = {
        **{pid: "C" for pid in (50, 51, 52)},
        **{pid: "L" for pid in (53, 54)},
        **{pid: "C" for pid in (60, 61, 62)},
        **{pid: "L" for pid in (63, 64, 65)},
    }

    result = lineup_reconstruct.reconstruct_game(shifts, positions)

    assert all(entry["pp_unit"] is None for entry in result.values())


def test_empty_shift_list_yields_no_assignments():
    assert lineup_reconstruct.reconstruct_game([], {}) == {}


def test_goalies_are_excluded_even_though_shifts_include_them():
    # A goalie's shift spans his whole time in net. Left in the pool his ~60
    # shared minutes would glue the forwards together and outrank every line.
    shifts = [
        _shift(1, 100, "00:00", "10:00"),
        _shift(2, 100, "00:00", "10:00"),
        _shift(3, 100, "00:00", "10:00"),
        _shift(900, 100, "00:00", "10:00"),  # goalie
        _shift(30, 200, "00:00", "10:00"),
        _shift(31, 200, "00:00", "10:00"),
        _shift(32, 200, "00:00", "10:00"),
        _shift(901, 200, "00:00", "10:00"),  # goalie
    ]
    positions = {1: "C", 2: "L", 3: "R", 900: "G", 30: "C", 31: "L", 32: "R", 901: "G"}

    result = lineup_reconstruct.reconstruct_game(shifts, positions)

    assert 900 not in result and 901 not in result
    assert result[1]["lineup_unit"] == result[2]["lineup_unit"] == result[3]["lineup_unit"] == "F1"


def test_size_cap_stops_the_mega_cluster_collapse():
    # The rich-get-richer bug: a strong trio plus four skaters who each rotate
    # through the trio's ice (big summed cross-edges) but barely touch each
    # other. Without a size cap, greedy sum-linkage glues all seven into one
    # line and leaves singles; the cap keeps the trio whole.
    weights = {frozenset((a, b)): 400 for a, b in ((1, 2), (1, 3), (2, 3))}
    for skater in (4, 5, 6, 7):
        for starter in (1, 2, 3):
            weights[frozenset((skater, starter))] = 100
    for a, b in ((4, 5), (4, 6), (4, 7), (5, 6), (5, 7), (6, 7)):
        weights[frozenset((a, b))] = 10

    clusters = lineup_reconstruct._cluster([1, 2, 3, 4, 5, 6, 7], weights, 3, 3)

    assert len(clusters) == 3
    assert all(len(c) <= 3 for c in clusters)
    assert any({1, 2, 3} == set(c) for c in clusters)


def test_duplicated_team_ids_and_repeated_shifts_reconstruct_the_same_game():
    # 2021020513 came back from shiftcharts with every shift tripled and the
    # copies split across two team ids per club - four ids, two rosters. The
    # sweep needs two sides, so normalization collapses the twins and repeats
    # and the assignments match the clean feed exactly.
    shifts, positions = _game()
    # Each copy tags home with one of {100, 77} and away with one of {200, 88},
    # so each id consistently carries one club's roster - the real corruption.
    aliases = ({100: 100, 200: 200}, {100: 77, 200: 88}, {100: 77, 200: 200})

    corrupted = []
    for alias in aliases:
        for shift in shifts:
            corrupted.append({**shift, "teamId": alias[shift["teamId"]]})

    clean = lineup_reconstruct.reconstruct_game(shifts, positions)
    repaired = lineup_reconstruct.reconstruct_game(corrupted, positions)

    assert repaired == clean
