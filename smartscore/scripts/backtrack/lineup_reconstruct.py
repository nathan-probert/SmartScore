"""Reconstruct actual lines and power-play units from shift-chart data.

The live product stored PROJECTED lineups scraped from Rotowire and the
NHL.com projections article - both current-day only, with no archive. The
legacy stats API's shiftcharts feed (see nhl_client.fetch_shiftcharts) carries
every skater shift for every completed game, which is enough to rebuild what
actually deployed, for any season the feed covers.

METHOD
------
Shifts become intervals; a sweep over one period's shift boundaries yields
constant on-ice sets between consecutive events. Each interval contributes its
duration to every on-ice pair's shared-TOI matrix, split by strength:

* equal side counts, or 6v5-type noise (smaller side >= 5) -> even strength
* larger side with the smaller side at 3-4 -> power play for the larger side
  (a 6v4 is a pulled-goalie power play and counts; a 5v1 empty-net finish
  does not, because the smaller side is below 3)
* everything else (goalie-pulled extremes) -> even

Lines and pairs are agglomerative clusters over the even-strength matrix:
start with one cluster per skater, repeatedly merge the two clusters sharing
the most ice time until the target count (4 forward lines, 3 defensive pairs,
or fewer when a team dressed a short bench). Merges are size-capped at the
unit's real maximum - 3 forwards, 2 defense, 5 on a power play - because a
sum-linkage that is free to grow without bound does exactly that: once one
cluster is large, the sum of its many small cross-edges (line-change overlap,
rotations) outbids any within-line pair, and the whole roster collapses into
one mega-cluster with singles left over. The cap makes every merge legal only
while it still forms a plausible unit. Clusters are ranked by summed player
TOI to produce F1-F4 / D1-D3. Power play units cluster the same way over the
power-play matrix, and skaters under a floor of power-play time are left off
the units entirely.

This deliberately stores defensive pair labels in lineup_unit, which the live
merge never did (it kept F1-F4 only). The column exists, the data is real, and
a replacement table carrying strictly more deployment detail is the point of
the backtrack.

lineup_status is ACTUAL rather than the live product's PROJECTED: these units
were reconstructed from what happened in the game, not from a morning scrape.
"""

from math import ceil

# A skater must accumulate at least this many power-play seconds in a game
# before being assigned to a unit. Late-game leftovers - a fourth line out for
# the final two minutes of a decided power play - run tens of seconds and would
# otherwise clutter PP2.
MIN_PP_SECONDS = 15

# Sub-minute shared-TOI pairs are line-change overlap, not linemates: two
# skaters crossing during a change can register a couple of seconds together.
# Zeroing them keeps the clustering graph honest.
MIN_PAIR_SECONDS = 5

# Box score position codes. Goalies are excluded before the sweep (they appear
# in shift data but have no deployment label); anything
# unrecognised is treated as a forward rather than dropped, because losing a
# forward silently shifts every line label.
_FORWARD_POSITIONS = {"L", "C", "R"}
_DEFENSE_POSITIONS = {"D"}


def _to_seconds(mark):
    """``"12:34"`` -> 754. None when the mark is missing or unparseable."""
    if not mark:
        return None

    parts = str(mark).split(":")
    if len(parts) != 2:
        return None

    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def _classify_strength(home_on, away_on):
    """Strength state from on-ice side counts: 'even', or 'pp-home'/'pp-away'."""
    smaller = min(home_on, away_on)
    larger = max(home_on, away_on)

    if smaller == larger or smaller >= 5 or smaller < 3:
        return "even"

    return "pp-home" if home_on > away_on else "pp-away"


def _accumulate(shifts):
    """Sweep one game's shifts into per-team shared-TOI and per-player TOI.

    Returns ``(pair_toi, player_toi)`` where ``pair_toi[team][frozenset({a, b})]``
    is shared seconds tagged 'even'/'pp', and ``player_toi[team][pid]`` is
    {'even': s, 'pp': s}. Teams are keyed by the shift's teamId, so a traded
    player's two stints in one game stay on the right sides.
    """
    pair_toi = {}
    player_toi = {}

    by_period = {}
    for shift in shifts:
        start = _to_seconds(shift.get("startTime"))
        end = _to_seconds(shift.get("endTime"))
        player_id = shift.get("playerId")
        team_id = shift.get("teamId")

        if start is None or end is None or end <= start or not player_id or not team_id:
            continue

        by_period.setdefault((shift.get("period"), team_id), []).append((start, end, player_id))

    def _team_maps(team_id):
        return pair_toi.setdefault(team_id, {}), player_toi.setdefault(team_id, {})

    # All teams' events in one period share a timeline, because strength
    # depends on both sides' counts at the same instant.
    period_ids = {period for period, _ in by_period}

    for period in period_ids:
        home_team, away_team = sorted({team for p, team in by_period if p == period})

        def _events(team_id):
            events = []
            for start, end, player_id in by_period[(period, team_id)]:
                events.append((start, 1, player_id))
                events.append((end, -1, player_id))
            return sorted(events)

        home_events, away_events = _events(home_team), _events(away_team)

        home_on, away_on = set(), set()
        merged = sorted(home_events + away_events)

        index = {"home": 0, "away": 0}
        bounds = sorted({t for t, _, _ in merged})

        for i, t_start in enumerate(bounds):
            t_end = bounds[i + 1] if i + 1 < len(bounds) else t_start

            while index["home"] < len(home_events) and home_events[index["home"]][0] <= t_start:
                _, delta, pid = home_events[index["home"]]
                (home_on.add(pid) if delta == 1 else home_on.discard(pid))
                index["home"] += 1
            while index["away"] < len(away_events) and away_events[index["away"]][0] <= t_start:
                _, delta, pid = away_events[index["away"]]
                (away_on.add(pid) if delta == 1 else away_on.discard(pid))
                index["away"] += 1

            if t_end <= t_start or not home_on or not away_on:
                continue

            duration = t_end - t_start
            strength = _classify_strength(len(home_on), len(away_on))

            for team_id, on_ice in ((home_team, home_on), (away_team, away_on)):
                # The short side of a power play accumulates into 'pk', which no
                # clustering reads: shorthanded time is real ice time together
                # but it is not a line, and folding it into 'even' would fuse
                # the penalty killers into a phantom even-strength unit.
                if strength == "even":
                    bucket = "even"
                elif strength == ("pp-home" if team_id == home_team else "pp-away"):
                    bucket = "pp"
                else:
                    bucket = "pk"

                maps_pairs, maps_players = _team_maps(team_id)

                for pid in on_ice:
                    bucket_totals = maps_players.setdefault(pid, {"even": 0, "pp": 0, "pk": 0})
                    bucket_totals[bucket] += duration

                skaters = sorted(on_ice)
                for a_index, a in enumerate(skaters):
                    for b in skaters[a_index + 1 :]:
                        key = frozenset((a, b))
                        entry = maps_pairs.setdefault(key, {"even": 0, "pp": 0, "pk": 0})
                        entry[bucket] += duration

    return pair_toi, player_toi


def _cluster(member_ids, pair_weights, target_count, max_size):
    """Agglomeratively merge ``member_ids`` toward ``target_count`` clusters.

    No merge may exceed ``max_size`` members. The loop stops early (leaving
    more clusters than the target) when every remaining merge would break the
    cap - an isolated skater or a short bench that cannot form one more full
    unit - rather than crashing on an empty candidate set.
    """
    clusters = [[pid] for pid in sorted(member_ids)]

    def _inter_weight(left, right):
        total = 0
        for a in left:
            for b in right:
                total += pair_weights.get(frozenset((a, b)), 0)
        return total

    while len(clusters) > target_count:
        best = None
        for i, left in enumerate(clusters):
            for j in range(i + 1, len(clusters)):
                right = clusters[j]
                if len(left) + len(right) > max_size:
                    continue
                weight = _inter_weight(left, right)
                # Ties break toward the cluster containing the smaller player
                # id, so two runs of the same game label identically.
                key = (weight, -min(left + right))
                if best is None or key > best[0]:
                    best = (key, i, j)

        if best is None:
            break

        _, i, j = best
        merged = sorted(clusters[i] + clusters[j])
        clusters = [c for k, c in enumerate(clusters) if k not in (i, j)] + [merged]
        clusters.sort(key=min)

    return clusters


def _rank_labels(clusters, player_toi, prefix):
    """Rank clusters by summed TOI and label them ``{prefix}1..``."""

    def _cluster_toi(cluster):
        return sum(player_toi.get(pid, {}).get("even", 0) + player_toi.get(pid, {}).get("pp", 0) for pid in cluster)

    ranked = sorted(clusters, key=lambda c: (-_cluster_toi(c), min(c)))
    labels = {}

    for index, cluster in enumerate(ranked, start=1):
        for pid in cluster:
            labels[pid] = f"{prefix}{index}"

    return labels


def reconstruct_game(shifts, positions):
    """Line and PP-unit assignments for every skater in one game's shifts.

    Args:
        shifts: raw shiftchart records (playerId, teamId, period, startTime,
            endTime).
        positions: ``{player_id: position_code}`` from the box score - the one
            input shift data does not carry.

    Returns:
        ``{player_id: {"lineup_unit", "lineup_position_group", "pp_unit",
        "lineup_status"}}``. lineup_unit is F1-F4 / D1-D3 by deployment rank;
        pp_unit is PP1/PP2 or None for skaters under the power-play floor.
    """
    # Goalies DO appear in shift data - one shift spanning their whole time in
    # net - so they must leave before the sweep: counting them inflates side
    # counts (a real 5v4 reads as 6v5 and classifies as even, killing PP
    # detection) and their ~60 shared minutes would glue every line together
    # and outrank them all. Nothing in the game needs their time in net.
    shifts = [shift for shift in shifts if positions.get(shift.get("playerId")) != "G"]

    pair_toi, player_toi = _accumulate(shifts)
    assignments = {}

    for team_id, skaters in player_toi.items():
        team_pairs = pair_toi.get(team_id, {})
        # The box score's D code splits pairs; anything not D counts as a
        # forward, so an odd code keeps the skater in the line pool rather
        # than dropping him from both.
        forwards = [pid for pid in skaters if positions.get(pid) not in _DEFENSE_POSITIONS]
        defense = [pid for pid in skaters if positions.get(pid) in _DEFENSE_POSITIONS]

        even_pairs = {key: value["even"] for key, value in team_pairs.items() if value["even"] >= MIN_PAIR_SECONDS}
        forward_set, defense_set = set(forwards), set(defense)
        forward_weights = {key: weight for key, weight in even_pairs.items() if key <= forward_set}
        defense_weights = {key: weight for key, weight in even_pairs.items() if key <= defense_set}

        target_lines = min(4, max(1, ceil(len(forwards) / 3)))
        target_pairs = min(3, max(1, ceil(len(defense) / 2)))

        line_labels = _rank_labels(_cluster(forwards, forward_weights, target_lines, 3), skaters, "F")
        pair_labels = _rank_labels(_cluster(defense, defense_weights, target_pairs, 2), skaters, "D")

        pp_eligible = [pid for pid, totals in skaters.items() if totals["pp"] >= MIN_PP_SECONDS]
        pp_set = set(pp_eligible)
        pp_pairs = {
            key: value["pp"] for key, value in team_pairs.items() if value["pp"] >= MIN_PAIR_SECONDS and key <= pp_set
        }
        pp_labels = {}
        if pp_eligible:
            target_units = 2 if len(pp_eligible) >= 8 else 1
            pp_labels = _rank_labels(_cluster(pp_eligible, pp_pairs, target_units, 5), skaters, "PP")

        for pid in skaters:
            if pid in line_labels:
                unit = line_labels[pid]
                group = "F"
            else:
                unit = pair_labels.get(pid)
                group = "D" if unit else None

            assignments[pid] = {
                "lineup_unit": unit,
                "lineup_position_group": group,
                "pp_unit": pp_labels.get(pid),
                "lineup_status": "ACTUAL" if unit else None,
            }

    return assignments
