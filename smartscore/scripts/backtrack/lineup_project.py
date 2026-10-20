"""Pre-game lineup projector from prior games only.

For every (date, team, player) row in the backtrack archive, project the
lineup unit (F1-F4 / D1-D3) and power-play unit (PP1/PP2) using only games
strictly before the target date:

  a. last available prior unit;
  b. exponentially weighted mode over the last 10 prior games;
  c. team-constrained greedy assignment into the F1-F4 / D1-D3 / PP1-PP2
     slots by the same recency-weighted usage.

The module is standalone: it reads the cached player snapshot CSV and the raw
``player_games`` SQLite store, writes ``data/ml/projected_lineups.csv`` and
prints projection accuracy against the archive's actual units. It does not
touch ``model.py``.
"""

import argparse
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "data" / "ml"
RAW_DB = ROOT / "data" / "raw_nhl.sqlite"

LINE_UNITS = ["F1", "F2", "F3", "F4", "D1", "D2", "D3"]
PP_UNITS = ["PP1", "PP2"]
DECAY = 0.8


def load_frame(env="dev"):
    players = pd.read_csv(CACHE / f"player_snapshots_backtrack_{env}.csv")
    players["date"] = players["date"].astype(str)
    con = sqlite3.connect(f"file:{RAW_DB}?mode=ro", uri=True)
    clubs = pd.read_sql_query(
        "SELECT game_date AS date, player_id, team_abbrev AS team, season "
        "FROM player_games",
        con,
    )
    con.close()
    clubs["date"] = clubs["date"].astype(str)
    clubs["season"] = clubs["season"].astype("Int64").astype(str)
    clubs = clubs.drop_duplicates(["date", "player_id"])
    frame = players.merge(
        clubs, on=["date", "player_id"], how="left", validate="many_to_one"
    )
    return frame.sort_values(["player_id", "date"]).reset_index(drop=True)


def _weighted_mode(values):
    scores = defaultdict(float)
    size = len(values)
    for age, value in enumerate(values):
        if pd.notna(value):
            scores[value] += DECAY ** (size - 1 - age)
    return (max(scores, key=scores.get) if scores else np.nan), dict(scores)


def project(frame):
    """Return the projected table; every label comes from rows [i-10, i)."""
    f = frame.sort_values(["player_id", "date"]).reset_index(drop=True)
    n = len(f)
    pos = f["lineup_position_group"].to_numpy(dtype=object)
    date = f["date"].to_numpy(dtype=object)
    team = f["team"].to_numpy(dtype=object)
    units = f["lineup_unit"].to_numpy(dtype=object)
    pps = f["pp_unit"].to_numpy(dtype=object)

    a_u = np.full(n, np.nan, dtype=object)
    a_p = np.full(n, np.nan, dtype=object)
    b_u = np.full(n, np.nan, dtype=object)
    b_p = np.full(n, np.nan, dtype=object)
    scores_u = [None] * n
    scores_p = [None] * n
    prior5_u = [None] * n
    prior5_p = [None] * n

    for _, positions in f.groupby("player_id", sort=False).indices.items():
        player_u = units[positions]
        player_p = pps[positions]
        for i, row in enumerate(positions):
            recent_u = player_u[max(0, i - 10) : i]
            recent_p = player_p[max(0, i - 10) : i]
            if recent_u.size and pd.notna(recent_u[-1]):
                a_u[row] = recent_u[-1]
            if recent_p.size and pd.notna(recent_p[-1]):
                a_p[row] = recent_p[-1]
            b_u[row], scores_u[row] = _weighted_mode(recent_u)
            b_p[row], scores_p[row] = _weighted_mode(recent_p)
            prior5_u[row] = [v for v in player_u[max(0, i - 5) : i] if pd.notna(v)]
            prior5_p[row] = [v for v in player_p[max(0, i - 5) : i] if pd.notna(v)]

    c_u = np.full(n, np.nan, dtype=object)
    c_p = np.full(n, np.nan, dtype=object)
    for _, positions in f.groupby(["date", "team"], sort=False).indices.items():
        rows = list(positions)
        for group, group_units, capacity in (
            ("F", ["F1", "F2", "F3", "F4"], 3),
            ("D", ["D1", "D2", "D3"], 2),
        ):
            candidates = [r for r in rows if pos[r] == group]
            pairs = [
                (scores_u[r].get(unit, 0.0), r, unit)
                for r in candidates
                for unit in group_units
            ]
            pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
            counts = {unit: 0 for unit in group_units}
            assigned = {}
            for _, row, unit in pairs:
                if row in assigned or counts[unit] >= capacity:
                    continue
                assigned[row] = unit
                counts[unit] += 1
            for row in candidates:
                c_u[row] = assigned.get(row, np.nan)

        pairs = [
            (scores_p[r].get(unit, 0.0), r, unit)
            for r in rows
            for unit in PP_UNITS
        ]
        pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
        counts = {unit: 0 for unit in PP_UNITS}
        assigned = {}
        for _, row, unit in pairs:
            if row in assigned or counts[unit] >= 5:
                continue
            assigned[row] = unit
            counts[unit] += 1
        for row in rows:
            c_p[row] = assigned.get(row, np.nan)

    conf = {}
    for tag, projection, prior in (
        ("a", a_u, prior5_u),
        ("b", b_u, prior5_u),
        ("c", c_u, prior5_u),
    ):
        values = np.full(n, np.nan)
        for row in range(n):
            if pd.notna(projection[row]):
                values[row] = sum(1 for v in prior[row] if v == projection[row]) / 5
        conf[f"proj_line_confidence_{tag}"] = values
    for tag, projection, prior in (
        ("a", a_p, prior5_p),
        ("b", b_p, prior5_p),
        ("c", c_p, prior5_p),
    ):
        values = np.full(n, np.nan)
        for row in range(n):
            if pd.notna(projection[row]):
                values[row] = sum(1 for v in prior[row] if v == projection[row]) / 5
        conf[f"proj_pp_confidence_{tag}"] = values

    out = pd.DataFrame(
        {
            "date": date,
            "player_id": f["player_id"].to_numpy(),
            "team": team,
            "season": f["season"].to_numpy(dtype=object),
            "lineup_position_group": pos,
            "lineup_unit": f["lineup_unit"].to_numpy(dtype=object),
            "pp_unit": f["pp_unit"].to_numpy(dtype=object),
            "proj_lineup_unit_a": a_u,
            "proj_lineup_unit_b": b_u,
            "proj_lineup_unit_c": c_u,
            "proj_pp_unit_a": a_p,
            "proj_pp_unit_b": b_p,
            "proj_pp_unit_c": c_p,
            **conf,
        }
    )
    return out


def accuracy_table(table):
    rows = []
    for method in ("a", "b", "c"):
        line_col = f"proj_lineup_unit_{method}"
        pp_col = f"proj_pp_unit_{method}"
        for group, label in (("F", "Forwards"), ("D", "Defence")):
            data = table[
                table["lineup_unit"].notna()
                & (table["lineup_position_group"] == group)
            ]
            rows.append(
                {
                    "method": method,
                    "group": label,
                    "unit_match_pct": 100 * (data[line_col] == data["lineup_unit"]).mean(),
                    "n": len(data),
                }
            )
        data = table[table["pp_unit"].notna()]
        rows.append(
            {
                "method": method,
                "group": "PP",
                "unit_match_pct": 100 * (data[pp_col] == data["pp_unit"]).mean(),
                "n": len(data),
            }
        )
    return pd.DataFrame(rows)


def season_table(table):
    rows = []
    for season, data in table.groupby("season"):
        for method in ("a", "b", "c"):
            line_col = f"proj_lineup_unit_{method}"
            pp_col = f"proj_pp_unit_{method}"
            for group, label in (("F", "F"), ("D", "D")):
                sub = data[
                    data["lineup_unit"].notna()
                    & (data["lineup_position_group"] == group)
                ]
                rows.append(
                    {
                        "season": season,
                        "method": method,
                        "group": label,
                        "match_pct": 100 * (sub[line_col] == sub["lineup_unit"]).mean(),
                    }
                )
            sub = data[data["pp_unit"].notna()]
            rows.append(
                {
                    "season": season,
                    "method": method,
                    "group": "PP",
                    "match_pct": 100 * (sub[pp_col] == sub["pp_unit"]).mean(),
                }
            )
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev", help="backtrack table suffix")
    parser.add_argument(
        "--out",
        type=Path,
        default=CACHE / "projected_lineups.csv",
        help="output CSV path",
    )
    args = parser.parse_args()

    start = time.time()
    frame = load_frame(args.env)
    table = project(frame)
    table.to_csv(args.out, index=False)
    elapsed = time.time() - start

    nan_counts = {
        column: int(table[column].isna().sum())
        for column in table.columns
        if column.startswith("proj_")
    }
    print(f"rows: {len(table):,}")
    print(f"elapsed: {elapsed:.1f}s")
    print(f"output: {args.out}")
    print("\ncoverage / NaN:")
    for column, count in nan_counts.items():
        print(f"  {column}: {len(table) - count:,} non-null, {count:,} NaN")
    print("\noverall accuracy (%):")
    print(accuracy_table(table).to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print("\nby season (%):")
    print(season_table(table).pivot(index="season", columns=["group", "method"], values="match_pct").round(1).to_string())


if __name__ == "__main__":
    main()
