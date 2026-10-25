"""Train and score a goal-probability model on the backtracked archive.

GOAL

``Player-Snapshots-backtrack-{ENV}`` reconstructs season-to-date player stats
from NHL game logs; ``Team-Stats-backtrack-{ENV}`` and
``Goalie-Snapshots-backtrack-{ENV}`` hold the team and opponent-goalie rates at
their honest grains. This script joins all three into one row per player per
game, fits a model of ``scored``, and evaluates it the only way that matters:
per game date, one pick from each of the three tims challenge groups, counted
by whether the picks scored.

THE JOIN

  * player -> club: `(date, player_id)` against ``player_games`` in the raw
    store (data/raw_nhl.sqlite). The Supabase name column cannot do this: both
    New York clubs publish "New York", which leaves 3.4% of rows ambiguous when
    they share a date. The raw store is the audit trail the migrations designate
    for exactly this - it is the one place the club per player per game exists;
    every feature itself still comes from the Supabase tables.
  * player -> team rates: (date, team_abbrev) against
    ``Team-Stats-backtrack-{ENV}`` for the club's own tgpg / otga / otshga.
  * player -> opposing goalie: (date, opponent_abbrev) against
    ``Goalie-Snapshots-backtrack-{ENV}``. A substitution gives that team-date
    two goalie rows; the one with the most games_played (the regular starter)
    is kept.

LEAKAGE POLICY

Every feature column is a pre-game value by construction (the reconstruction
cut-offs are strictly before the row's game), and the daily pick sim only
consumes the pre-game tims group membership. Two split modes:

  * kfold      - the default. Dates are shuffled and cut into ``--splits``
                 blocks; each fold trains on the other blocks and simulates
                 picks on its held-out dates. Blocking by date (not by row)
                 keeps one slate's team / opponent / goalie context on the same
                 side of the split, so a fold's headline reads like real
                 deployment on unseen days. The report gives per-fold accuracy,
                 the fold spread, and pooled out-of-fold hits.
  * seasons    - the deployment-shaped backtest: train everything before
                 TEST_START, test 2024-25 and 2025-26 only.

Either way the live-weights baseline normalises with train-fold extrema only.
Test categorical values unseen in a fold's training rows become NaN, which
HistGradientBoosting treats natively and the logistic pipeline imputes.

THE DAILY SIM

For every held-out date, keep rows whose ``tims`` is 1, 2 or 3, sort each group
by the metric being evaluated and pick the top one; the pick is a hit iff
``scored`` is 1. Baselines evaluated the same way:

  * gpg   - the straight GPG pick: highest season-to-date rate per group.
  * hgpg  - straight historic rate, same rule.
  * live  - the deployed hand-tuned weights (calculate_accuracy.py),
    min-max normalised with the fold's training extrema.
  * random- one uniformly random member per group (a floor).

USAGE

    cd SmartScore-backtrack
    uv run python smartscore/scripts/backtrack/model.py --env dev \
        [--refresh] [--split kfold|seasons] [--splits 5] [--test-start DATE] [--min-season YEAR] \
        [--train-subset challenge|all] [--model ensemble|hgb|lgbm|extratrees] [--lgbm-config a|b|c]
        [--lineup none|projected|oracle] [--seed N] [--ranker none|lambdarank] [--model-file PATH]

Defaults: kfold k=5, train-subset all (strongest under CV; see MODEL_NOTES.md).

``--model-file PATH`` saves the pooled out-of-fold rows with every method's
score appended. Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the
environment (from .env via config.py). The three tables are ~476k rows total;
downloads are cached as CSVs under data/ml/ so repeat runs never touch the
network.
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# config.py sits in smartscore/; pytest finds it via pythonpath = ["smartscore"],
# a direct run needs the same directory on sys.path.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from config import ENV, SUPABASE_ADMIN_CLIENT  # noqa: E402

sys.path.append(os.path.dirname(__file__))
from lineup_project import project as project_lineups  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "ml"
RAW_DB = Path(__file__).resolve().parents[3] / "data" / "raw_nhl.sqlite"
PAGE = 10_000

# Where the frame's shot/TOI block and club identity come from. "sqlite"
# computes them from the raw store via shot_features()/club_map() (the
# existing path); "supabase" reads the columns published into
# Player-Snapshots-backtrack-{ENV} by the shot-columns migration
# (docs/supabase_shot_columns.md) - the path the in-cloud ML Lambda uses.
FRAME_SOURCES = ("sqlite", "supabase")

# Hand-tuned weights the deployed scorer runs on; the baseline to beat.
LIVE_WEIGHTS = {
    "hgpg": 0.600,
    "gpg": 0.190,
    "tgpg": 0.110,
    "five_gpg": 0.060,
    "otga": 0.040,
}

# Backtest mode: train before the 2024-25 season, test 2024-25 and 2025-26.
TEST_START = "2024-08-01"

SHOT_FEATURES = [
    "sd_shots_pg",
    "sd_goals_pg",
    "sd_toi_pg",
    "sd_sh_pct",
    "r5_shots_pg",
    "r5_goals_pg",
    "r10_shots_pg",
    "r10_goals_pg",
    "r10_toi_pg",
    "ewm_goals",
    "ewm_shots",
    "career_games",
    "career_goals_pg",
    "career_shots_pg",
]

NUMERIC_FEATURES = [
    "gpg",
    "five_gpg",
    "ppg",
    "hgpg",
    "hppg",
    "tgpg",
    "otga",
    "otshga",
    "home",
    "opp_gaa",
    "opp_save_pct",
    "opp_shutouts",
    "opp_games_played",
    "opp_wins",
    "opp_losses",
    "opp_otl",
    # V2 relative-context features (all pre-game, see add_derived).
    "rank_gpg",
    "rank_hgpg",
    "rank_five_gpg",
    "rank_ppg",
    "prev2_goals",
    "prev3_goals",
    "games_since_goal",
    "days_since_game",
    "club_rest_days",
    "opp_rest_days",
] + SHOT_FEATURES

CATEGORICAL_FEATURES = ["lineup_position_group", "tims"]

PROJECTED_LINEUP_CATEGORICAL = ["proj_lineup_unit_b", "proj_pp_unit_b"]
PROJECTED_LINEUP_NUMERIC = [
    "proj_line_conf",
    "proj_pp_conf",
    "linemate_prior_gpg_mean",
    "linemate_prior_ppg_mean",
    "pp_unit_prior_ppg_mean",
    "line_intact",
]

DATASET_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

PICK_FILE_COLUMNS = [
    "date",
    "player_id",
    "name",
    "team_name",
    "home",
    "tims",
    "scored",
    "gpg",
    "hgpg",
    "live",
    "hgb",
    "logreg",
]


def player_table(env):
    return f"Player-Snapshots-backtrack-{env}"


def team_stats_table(env):
    return f"Team-Stats-backtrack-{env}"


def goalie_table(env):
    return f"Goalie-Snapshots-backtrack-{env}"


def _month_windows(min_date, max_date):
    """Half-open month windows covering [min_date, max_date]."""
    start = pd.Timestamp(min_date).normalize().replace(day=1)
    end = pd.Timestamp(max_date)
    while start <= end:
        nxt = start + pd.offsets.MonthBegin(1)
        yield start.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")
        start = nxt


def _fetch_window(table, order_cols, lo, hi, chunk):
    """Fetch one date window; small offsets avoid PostgREST statement timeouts."""
    query = SUPABASE_ADMIN_CLIENT.table(table).select("*").gte("date", lo).lt("date", hi)
    for col in order_cols:
        query = query.order(col)
    frames = []
    offset = 0
    while True:
        rows = query.range(offset, offset + chunk - 1).execute().data
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        offset += len(rows)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_table(table, order_cols, refresh=False, chunk=PAGE):
    """Download one table into a CSV cache and return it as a DataFrame."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{table.lower().replace('-', '_')}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path)

    print(f"fetching {table} ...", flush=True)
    start = time.time()
    first = SUPABASE_ADMIN_CLIENT.table(table).select("date").order("date").limit(1).execute().data
    last = SUPABASE_ADMIN_CLIENT.table(table).select("date").order("date", desc=True).limit(1).execute().data
    if not first or not last:
        raise RuntimeError(f"{table} is empty - wrong --env, or nothing was published to it")

    # A single ordered range over 600k+ rows trips PostgREST's statement
    # timeout (57014). Month windows keep each offset small while preserving
    # the CSV's columns and values.
    frames = []
    for lo, hi in _month_windows(first[0]["date"], last[0]["date"]):
        part = _fetch_window(table, order_cols, lo, hi, chunk)
        if not part.empty:
            frames.append(part)
    if not frames:
        raise RuntimeError(f"{table} is empty - wrong --env, or nothing was published to it")
    frame = pd.concat(frames, ignore_index=True)
    frame.to_csv(path, index=False)
    print(f"  {len(frame):,} rows in {time.time() - start:.0f}s -> {path.name}")
    return frame


def load_frames(env, refresh=False):
    players = fetch_table(player_table(env), ["date", "player_id"], refresh)
    teams = fetch_table(team_stats_table(env), ["season", "team_abbrev", "game_id"], refresh)
    goalies = fetch_table(goalie_table(env), ["season", "player_id", "date"], refresh)
    return players, teams, goalies


def club_map():
    """(date, player_id) -> club, opponent, game_id, season, from the raw store.

    The Supabase name column renders both New York clubs as "New York", so a
    name merge cannot resolve which club a player skated for on a shared date.
    ``player_games`` holds team_abbrev / opponent_abbrev boxed per player per
    game - the audit trail the migrations designate as the source of truth.
    """
    con = sqlite3.connect(f"file:{RAW_DB}?mode=ro", uri=True)
    frame = pd.read_sql_query(
        "SELECT game_date AS date, player_id, game_id, season, team_abbrev AS club, "
        "opponent_abbrev AS opp FROM player_games",
        con,
    )
    con.close()
    # player_games.season is INTEGER; the Supabase CSVs carry it as TEXT.
    frame["season"] = frame["season"].astype("Int64").astype(str)
    return frame


def build_frame(players, teams, goalies, source="sqlite"):
    """Join the three Supabase tables into one row per player per game."""
    if source not in FRAME_SOURCES:
        raise ValueError(f"unknown frame source: {source}")
    players = players.copy()
    teams = teams.copy()
    goalies = goalies.copy()
    for copy in (players, teams, goalies):
        copy["date"] = copy["date"].astype(str)
        if "season" in copy:
            # CSV round-trips parse 20232024 as int64; keep it text like the DB.
            copy["season"] = copy["season"].astype("Int64").astype(str)
    # The player table carries tgpg / otga / otshga as null placeholders, moved
    # to Team-Stats at its own grain. Drop them so the merge owns the names.
    players = players.drop(columns=["tgpg", "otga", "otshga"], errors="ignore")

    if source == "supabase":
        # Club and opponent identity is published on the snapshot rows (the
        # shot-columns migration) - club := team_abbrev, opp := opponent_abbrev.
        # Never derive it from team_name: the place name collapses both New
        # York clubs. season is not a snapshot column; the team-stats join
        # below carries it in (one team season per (date, club) - the same
        # values club_map() would have supplied from player_games).
        missing = sorted({"team_abbrev", "opponent_abbrev"} - set(players.columns))
        if missing:
            raise RuntimeError(
                f"source=supabase but player snapshot lacks {missing}; "
                "the shot-columns migration/backfill has not run for this env"
            )
        players = players.rename(columns={"team_abbrev": "club", "opponent_abbrev": "opp"})
        players = players.merge(
            teams[["date", "team_abbrev", "season"]].rename(columns={"team_abbrev": "club"}),
            on=["date", "club"],
            how="left",
            validate="many_to_one",
        )
        players["season"] = players["season"].astype("Int64").astype(str)
    else:
        # Club and opponent identity per (date, player_id), from the raw store.
        players = players.merge(
            club_map(),
            on=["date", "player_id"],
            how="left",
            validate="many_to_one",
        )

    # Own-team rates. A club plays at most once a date, so (date, club) is one
    # team row despite the PK carrying game_id.
    own = teams[["date", "team_abbrev", "tgpg", "otga", "otshga"]]
    joined = players.merge(
        own.rename(columns={"team_abbrev": "club"}),
        on=["date", "club"],
        how="left",
        validate="many_to_one",
    )

    # Opposing goalie. A club plays once a date, so (date, opponent) is one
    # game; substitutions can still put two rows on one (date, team) - keep the
    # most experienced ledger, the regular starter's row.
    goalie_rows = goalies[["date", "team_abbrev", "gaa", "save_pct", "record", "shutouts", "games_played"]].sort_values(
        "games_played"
    )
    goalie_rows = goalie_rows.drop_duplicates(["date", "team_abbrev"], keep="last")
    joined = joined.merge(
        goalie_rows.rename(columns={"team_abbrev": "opp"}),
        on=["date", "opp"],
        how="left",
        validate="many_to_one",
    )

    joined = joined.rename(
        columns={
            "gaa": "opp_gaa",
            "save_pct": "opp_save_pct",
            "shutouts": "opp_shutouts",
            "games_played": "opp_games_played",
            "record": "opp_record",
        }
    )
    spread = joined["opp_record"].fillna("-").str.split("-", expand=True)
    joined["opp_wins"] = pd.to_numeric(spread[0], errors="coerce")
    joined["opp_losses"] = pd.to_numeric(spread[1], errors="coerce")
    joined["opp_otl"] = pd.to_numeric(spread[2], errors="coerce")
    joined = joined.drop(columns="opp_record")

    joined["home"] = joined["home"].astype(float)
    joined["tims"] = pd.to_numeric(joined["tims"], errors="coerce").fillna(0).astype(int)
    joined["scored"] = pd.to_numeric(joined["scored"], errors="coerce")

    return joined


def _since_last_goal(history):
    """Consecutive past games without a goal, per player, capped at 15.

    ``history`` is the shift(1)ed scored series of one player, so value i is
    the label of the game immediately before feature row i. Never scored
    inside the coverage window -> NaN (mostly depth defencemen; NaN is the
    honest signal and HGB splits on it natively).
    """
    out = np.empty(len(history), dtype=float)
    streak = 0.0
    seen = False
    for i, value in enumerate(history):
        if value == 1:
            streak = 0.0
            seen = True
        elif seen:
            streak = min(streak + 1.0, 15.0)
        out[i] = streak if seen else np.nan
    return pd.Series(out, index=history.index)


def shot_features():
    """Prior-games-only shot / TOI / shooting-talent block from the raw store.

    Supabase player snapshots carry goals and points but not shots or ice
    time, and goals alone are a noisy talent estimate (~15% of rows score).
    ``player_games`` has the boxscore line per player per game, so every
    column here is an aggregate over that player's *strictly earlier* games:
    season-to-date (prefix sums minus the current row), last-5/10, exponentially
    weighted, and career-to-date. The current game's own line is never part of
    any feature, which keeps the block pre-game and fold-safe.
    """
    con = sqlite3.connect(f"file:{RAW_DB}?mode=ro", uri=True)
    games = pd.read_sql_query(
        "SELECT player_id, game_date AS date, season, goals, shots, toi FROM player_games",
        con,
    )
    con.close()
    games["date"] = games["date"].astype(str)
    games["season"] = games["season"].astype("Int64").astype(str)
    games = games.sort_values(["player_id", "date", "season"]).reset_index(drop=True)
    games["shots"] = pd.to_numeric(games["shots"], errors="coerce")
    games["goals"] = pd.to_numeric(games["goals"], errors="coerce")
    games["toi_min"] = games["toi"].apply(
        lambda value: (
            int(str(value).split(":")[0]) + int(str(value).split(":")[1]) / 60
            if isinstance(value, str) and ":" in value
            else np.nan
        )
    )

    season_group = games.groupby(["player_id", "season"], sort=False)
    prior_games = season_group.cumcount()
    goals_before = season_group["goals"].cumsum() - games["goals"]
    shots_before = season_group["shots"].cumsum() - games["shots"]
    toi_before = season_group["toi_min"].cumsum() - games["toi_min"]
    games["sd_goals_pg"] = goals_before / prior_games.replace(0, np.nan)
    games["sd_shots_pg"] = shots_before / prior_games.replace(0, np.nan)
    games["sd_toi_pg"] = toi_before / prior_games.replace(0, np.nan)
    games["sd_sh_pct"] = goals_before / shots_before.replace(0, np.nan)

    player_group = games.groupby("player_id", sort=False)
    for window in (5, 10):
        games[f"r{window}_shots_pg"] = player_group["shots"].transform(
            lambda series: series.shift(1).rolling(window, min_periods=1).mean()
        )
        games[f"r{window}_goals_pg"] = player_group["goals"].transform(
            lambda series: series.shift(1).rolling(window, min_periods=1).mean()
        )
    games["r10_toi_pg"] = player_group["toi_min"].transform(
        lambda series: series.shift(1).rolling(10, min_periods=1).mean()
    )
    games["ewm_goals"] = player_group["goals"].transform(
        lambda series: series.shift(1).ewm(halflife=5, min_periods=1).mean()
    )
    games["ewm_shots"] = player_group["shots"].transform(
        lambda series: series.shift(1).ewm(halflife=5, min_periods=1).mean()
    )
    games["career_games"] = player_group.cumcount()
    games["career_goals_pg"] = player_group["goals"].transform(lambda series: series.shift(1).expanding().mean())
    games["career_shots_pg"] = player_group["shots"].transform(lambda series: series.shift(1).expanding().mean())
    cols = ["date", "player_id"] + SHOT_FEATURES
    return games[cols]


def projected_lineup_features(frame):
    """Method-b projected units + prior-only linemate quality.

    ``lineup_project.project`` supplies the a/b/c projections; this wrapper
    keeps method b and computes line/PP mate means from the projected groups
    using the frame's pre-game gpg/ppg. ``line_intact`` compares the projected
    forward trio set with the player's previous-game actual trio.
    """
    source = frame.rename(columns={"club": "team"})[
        [
            "date",
            "player_id",
            "team",
            "season",
            "lineup_position_group",
            "lineup_unit",
            "pp_unit",
        ]
    ]
    projection = project_lineups(source)
    projected = projection[
        [
            "date",
            "player_id",
            "proj_lineup_unit_b",
            "proj_pp_unit_b",
            "proj_line_confidence_b",
            "proj_pp_confidence_b",
        ]
    ].rename(
        columns={
            "proj_line_confidence_b": "proj_line_conf",
            "proj_pp_confidence_b": "proj_pp_conf",
        }
    )
    merged = (
        frame.merge(projected, on=["date", "player_id"], how="left", validate="many_to_one")
        .sort_values(["player_id", "date"])
        .reset_index(drop=True)
    )

    n = len(merged)
    pos = merged["lineup_position_group"].to_numpy(dtype=object)
    gpg = pd.to_numeric(merged["gpg"], errors="coerce").to_numpy(dtype=float)
    ppg = pd.to_numeric(merged["ppg"], errors="coerce").to_numpy(dtype=float)
    date = merged["date"].to_numpy(dtype=object)
    club = merged["club"].to_numpy(dtype=object)
    unit = merged["lineup_unit"].to_numpy(dtype=object)
    proj_u = merged["proj_lineup_unit_b"].to_numpy(dtype=object)
    proj_p = merged["proj_pp_unit_b"].to_numpy(dtype=object)
    pid = merged["player_id"].to_numpy()

    line_gpg = np.full(n, np.nan)
    line_ppg = np.full(n, np.nan)
    pp_ppg = np.full(n, np.nan)
    proj_frame = pd.DataFrame({"date": date, "club": club, "unit": proj_u})
    proj_indices = proj_frame.groupby(["date", "club", "unit"], sort=False).indices
    for (_, _, group_unit), group_idx in proj_indices.items():
        if pd.isna(group_unit):
            continue
        for row in group_idx:
            others = [other for other in group_idx if other != row and pos[other] == pos[row]]
            if others:
                line_gpg[row] = np.nanmean(gpg[others])
                line_ppg[row] = np.nanmean(ppg[others])

    pp_frame = pd.DataFrame({"date": date, "club": club, "unit": proj_p})
    pp_indices = pp_frame.groupby(["date", "club", "unit"], sort=False).indices
    for (_, _, group_unit), group_idx in pp_indices.items():
        if pd.isna(group_unit):
            continue
        for row in group_idx:
            others = [other for other in group_idx if other != row]
            if others:
                pp_ppg[row] = np.nanmean(ppg[others])

    actual_sets = {}
    for (day, team, group_unit), idxs in merged.groupby(["date", "club", "lineup_unit"], sort=False).indices.items():
        if pd.notna(group_unit):
            actual_sets[(day, team, group_unit)] = set(pid[idxs])
    proj_sets = {}
    for (day, team, group_unit), idxs in proj_indices.items():
        if pd.notna(group_unit):
            proj_sets[(day, team, group_unit)] = set(pid[idxs])

    previous = np.full(n, -1, dtype=int)
    for _, positions in merged.groupby("player_id", sort=False).indices.items():
        for earlier, later in zip(positions[:-1], positions[1:]):
            previous[later] = earlier
    intact = np.zeros(n, dtype=float)
    for row in range(n):
        prev = previous[row]
        if prev < 0 or pd.isna(proj_u[row]):
            continue
        prev_unit = unit[prev]
        if pd.isna(prev_unit) or not str(prev_unit).startswith("F"):
            continue
        if not str(proj_u[row]).startswith("F"):
            continue
        old = actual_sets.get((date[prev], club[prev], prev_unit))
        new = proj_sets.get((date[row], club[row], proj_u[row]))
        intact[row] = int(old is not None and old == new)

    return pd.DataFrame(
        {
            "date": date,
            "player_id": pid,
            "proj_lineup_unit_b": proj_u,
            "proj_pp_unit_b": proj_p,
            "proj_line_conf": merged["proj_line_conf"].to_numpy(),
            "proj_pp_conf": merged["proj_pp_conf"].to_numpy(),
            "linemate_prior_gpg_mean": line_gpg,
            "linemate_prior_ppg_mean": line_ppg,
            "pp_unit_prior_ppg_mean": pp_ppg,
            "line_intact": intact,
        }
    )


def add_derived(frame, teams, source="sqlite"):
    """V2 features layered onto the joined frame. Everything stays pre-game.

    * rank_*       - percentile of the rate within the day's tims group. Only
                     that day's pre-game stats enter the ranking, and a
                     date-blocked fold holds the whole slate out together, so
                     no train row ever contributes to a test row's rank.
    * prevN_goals / games_since_goal - past scored labels shifted one row per
                     player, strictly earlier games only.
    * days_since_game (per player), club_rest_days / opp_rest_days (per club)
    * shot-* / rN_* / ewm_* / career_* - prior-games-only shot, TOI and
                     shooting-talent block from the raw boxscore store (see
                     shot_features); never includes the current game's line.
    """
    frame = frame.sort_values(["player_id", "date"]).reset_index(drop=True)
    n_rows = len(frame)
    player = frame.groupby("player_id", sort=False)

    shifted_label = player["scored"].shift(1)
    for window in (2, 3):
        frame[f"prev{window}_goals"] = shifted_label.groupby(frame["player_id"], sort=False).transform(
            lambda series: series.rolling(window, min_periods=1).sum()
        )
    frame["games_since_goal"] = shifted_label.groupby(frame["player_id"], sort=False).transform(_since_last_goal)
    frame["days_since_game"] = (pd.to_datetime(frame["date"]) - pd.to_datetime(player["date"].shift(1))).dt.days

    # Rest per club appearance; a club's rate context already carried its own
    # table rows, the rest days are what the player rows were missing.
    schedule = (
        teams[["date", "team_abbrev"]]
        .drop_duplicates(["date", "team_abbrev"])
        .sort_values(["team_abbrev", "date"])
        .reset_index(drop=True)
    )
    rest = schedule.groupby("team_abbrev", sort=False)["date"].transform(
        lambda series: (pd.to_datetime(series) - pd.to_datetime(series.shift(1))).dt.days
    )
    schedule = schedule.assign(rest_days=rest)

    frame = frame.merge(
        schedule[["date", "team_abbrev", "rest_days"]].rename(columns={"team_abbrev": "club"}),
        on=["date", "club"],
        how="left",
        validate="many_to_one",
    )
    frame = frame.rename(columns={"rest_days": "club_rest_days"})
    opp_rest = schedule[["date", "team_abbrev", "rest_days"]].rename(
        columns={"team_abbrev": "opp", "rest_days": "opp_rest_days"}
    )
    frame = frame.merge(
        opp_rest,
        on=["date", "opp"],
        how="left",
        validate="many_to_one",
    )

    # Prior-games-only shot / TOI / shooting block. On the sqlite path it is
    # computed from the raw boxscore layer; on the supabase path the same
    # columns were published on the snapshot rows (shot-columns migration),
    # so the merge is skipped - the frame already carries them.
    if source == "supabase":
        missing = sorted(set(SHOT_FEATURES) - set(frame.columns))
        if missing:
            raise RuntimeError(
                f"source=supabase but snapshot rows lack {missing}; "
                "the shot-columns backfill has not populated this env"
            )
    else:
        # The snapshot rows may already carry the published shot columns (the
        # shot-columns backfill writes them); the sqlite source computes the
        # block itself, so never merge onto its own names.
        frame = frame.drop(columns=SHOT_FEATURES, errors="ignore")
        frame = frame.merge(
            shot_features(),
            on=["date", "player_id"],
            how="left",
            validate="many_to_one",
        )

    # V9 stage 2: method-b projected units + prior-only linemate quality.
    frame = frame.merge(
        projected_lineup_features(frame),
        on=["date", "player_id"],
        how="left",
        validate="many_to_one",
    )

    # Within (date, tims group) percentiles of the four rates.
    groups = frame[frame["tims"].isin([1, 2, 3])]
    for col in ("gpg", "hgpg", "five_gpg", "ppg"):
        frame[f"rank_{col}"] = groups.groupby(["date", "tims"])[col].rank(pct=True)

    if len(frame) != n_rows:
        raise RuntimeError(f"add_derived changed row count: {n_rows} -> {len(frame)}")
    suffixes = [col for col in frame.columns if col.endswith(("_x", "_y"))]
    if suffixes:
        raise RuntimeError(f"add_derived leaked merge suffix columns: {suffixes}")
    return frame


def categorise(train, test):
    """Feature matrices built on the fold's training vocabulary only.

    An unseen test category maps to NaN, which HistGradientBoosting splits on
    natively; the logistic pipeline sees only the numeric block anyway.
    """
    x_train = train[DATASET_FEATURES].copy()
    x_test = test[DATASET_FEATURES].copy()
    for col in CATEGORICAL_FEATURES:
        cdict = {value: value for value in sorted(set(x_train[col].dropna().astype(str)))}
        x_train[col] = x_train[col].astype(str).map(cdict).astype("category")
        x_test[col] = x_test[col].astype(str).map(cdict).astype("category")
        x_test.loc[~x_test[col].isin(x_train[col].cat.categories), col] = np.nan
    return x_train, x_test


def daily_picks(sim, score_col):
    """Per date per tims group: the row with the highest score_col.

    Rows whose score is NaN (a player-side column genuinely missing, e.g. the
    opposing stats of a team-debut row) cannot be ranked; a group whose every
    member is NaN produces no pick.
    """
    picks = []
    for _, day in sim.groupby("date", sort=True):
        for tims in (1, 2, 3):
            members = day[day["tims"] == tims]
            finite = members[members[score_col].notna()]
            if finite.empty:
                continue
            picks.append(finite.loc[finite[score_col].idxmax()])
    return pd.DataFrame(picks)


def score_picks(picks, tag):
    hits = int((picks["scored"] == 1).sum())
    total = len(picks)
    per_day = picks.groupby("date")["scored"].sum()
    return {
        "method": tag,
        "total": total,
        "hits": hits,
        "acc": 100 * hits / total if total else np.nan,
        "hits_day": per_day.mean() if len(per_day) else np.nan,
        "days3": int((per_day == 3).sum()),
        "days0": int((per_day == 0).sum()),
        "days": len(per_day),
    }


def live_score(frame, train_frame):
    """Min-max normalisation of the hand-tuned weights, like the live scorer.

    Extrema come from the training rows only; pass the training frame, not a
    mask onto ``frame`` - a mask applied to the test frame selects nothing and
    silently degrades every score to 0.0.
    """
    score = pd.Series(0.0, index=frame.index)
    for col, weight in LIVE_WEIGHTS.items():
        lo, hi = train_frame[col].min(), train_frame[col].max()
        if hi > lo:
            score += weight * (frame[col] - lo) / (hi - lo)
    return score


LGBM_CONFIGS = {
    "a": dict(n_estimators=400, learning_rate=0.03, num_leaves=31, min_child_samples=50),
    "b": dict(
        n_estimators=600,
        learning_rate=0.02,
        num_leaves=15,
        min_child_samples=50,
        reg_lambda=5.0,
    ),
    "c": dict(n_estimators=800, learning_rate=0.02, num_leaves=63, min_child_samples=20),
}


def fit_hgb(train, test):
    x_train, x_test = categorise(train, test)
    model = HistGradientBoostingClassifier(
        # Small grid on data/ml caches: lowest leaves + gentle LR won over
        # deeper trees and l2; differences were within ~1pp (pick noise).
        learning_rate=0.03,
        max_iter=1200,
        max_leaf_nodes=31,
        l2_regularization=1.0,
        categorical_features="from_dtype",
        early_stopping=False,
        random_state=0,
    )
    model.fit(x_train, train["scored"].astype(int))
    prob = pd.Series(model.predict_proba(x_test)[:, 1], index=test.index)
    return prob


def fit_lgbm(train, test, config="a"):
    """LightGBM binary classifier on the same feature encoding as HGB."""
    from lightgbm import LGBMClassifier  # noqa: PLC0415

    x_train, x_test = categorise(train, test)
    # LightGBM reads pandas category codes; align test categories to train.
    for col in CATEGORICAL_FEATURES:
        x_test[col] = x_test[col].cat.set_categories(x_train[col].cat.categories)
    model = LGBMClassifier(
        objective="binary",
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=0,
        n_jobs=int(os.environ.get("LGBM_N_JOBS", "4")),
        verbose=-1,
        **LGBM_CONFIGS[config],
    )
    model.fit(x_train, train["scored"].astype(int))
    return pd.Series(model.predict_proba(x_test)[:, 1], index=test.index)


def fit_extratrees(train, test):
    """ExtraTrees on the same features, with categoricals as ordinal codes."""
    from sklearn.ensemble import ExtraTreesClassifier  # noqa: PLC0415

    x_train, x_test = categorise(train, test)
    for col in CATEGORICAL_FEATURES:
        x_test[col] = x_test[col].cat.set_categories(x_train[col].cat.categories)
        x_train[col] = x_train[col].cat.codes.astype(float)
        x_test[col] = x_test[col].cat.codes.astype(float)
    model = ExtraTreesClassifier(
        n_estimators=400,
        min_samples_leaf=20,
        n_jobs=-1,
        random_state=0,
    )
    model.fit(x_train, train["scored"].astype(int))
    return pd.Series(model.predict_proba(x_test)[:, 1], index=test.index)


def fit_logreg(train, test):
    model = make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        LogisticRegression(max_iter=2000),
    )
    model.fit(train[NUMERIC_FEATURES], train["scored"].astype(int))
    return pd.Series(model.predict_proba(test[NUMERIC_FEATURES])[:, 1], index=test.index)


def _group_order(frame):
    """Row order and group sizes for (date, tims) groups.

    tims=0 rows group together per date, separately from the 1/2/3 challenge
    groups, which is the grain the daily pick sim ranks inside.
    """
    order = np.lexsort((frame["tims"].to_numpy(), frame["date"].to_numpy()))
    sizes = frame.iloc[order].groupby(["date", "tims"], sort=False).size().to_numpy()
    return order, sizes


def _fit_pairwise_logistic(x_train, y_train, groups, x_test):
    """Fallback ranker: logistic on within-group positive/negative pairs.

    A pair (winner, loser) becomes the feature difference; a linear model on
    differences ranks by w . x, so ``decision_function`` is the score.
    """
    from sklearn.linear_model import SGDClassifier  # noqa: PLC0415

    rng = np.random.default_rng(0)
    max_pairs = 120
    diffs, labels = [], []
    for _, idx in groups:
        y = y_train[idx]
        pos = idx[y == 1]
        neg = idx[y == 0]
        if len(pos) == 0 or len(neg) == 0:
            continue
        n = min(max_pairs, len(pos) * len(neg))
        left = rng.choice(pos, size=n)
        right = rng.choice(neg, size=n)
        diff = x_train[left] - x_train[right]
        # Both directions: (winner-loser, 1) and (loser-winner, 0), so the
        # logistic loss has two classes and w . x orders the group.
        diffs.append(np.vstack([diff, -diff]))
        labels.append(np.concatenate([np.ones(n, dtype=int), np.zeros(n, dtype=int)]))
    if not diffs:
        return pd.Series(0.0, index=getattr(x_test, "index", None))
    model = make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        SGDClassifier(loss="log_loss", alpha=1e-4, max_iter=30, tol=None, random_state=0),
    )
    model.fit(np.vstack(diffs), np.concatenate(labels))
    scores = model.decision_function(x_test)
    return pd.Series(scores, index=getattr(x_test, "index", None))


def fit_ranker(train, test, kind):
    """Within-(date, tims)-group ranker over the same V2 feature set.

    LightGBM's LGBMRanker is used when importable; otherwise the pairwise
    logistic fallback keeps the flag usable on a bare install. Scores are
    higher-is-better, so ``daily_picks`` consumes them unchanged.
    """
    x_train, x_test = categorise(train, test)
    # LightGBM reads pandas category codes; align the test categories to train.
    for col in CATEGORICAL_FEATURES:
        x_test[col] = x_test[col].cat.set_categories(x_train[col].cat.categories)

    order, sizes = _group_order(train)
    x_ordered = x_train.iloc[order]
    y_ordered = train["scored"].astype(int).to_numpy()[order]

    try:
        from lightgbm import LGBMRanker  # noqa: PLC0415
    except ImportError:
        print("  ranker: lightgbm unavailable, using pairwise logistic fallback", flush=True)
        x_num = x_ordered.copy()
        x_test_num = x_test.copy()
        for col in CATEGORICAL_FEATURES:
            x_num[col] = x_num[col].cat.codes.astype(float).replace(-1, np.nan)
            x_test_num[col] = x_test_num[col].cat.codes.astype(float).replace(-1, np.nan)
        groups = train.iloc[order].groupby(["date", "tims"], sort=False).indices
        positional = [(key, np.asarray(vals)) for key, vals in groups.items()]
        return _fit_pairwise_logistic(x_num.to_numpy(dtype=np.float32), y_ordered, positional, x_test_num)

    model = LGBMRanker(
        objective="lambdarank",
        n_estimators=400,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=50,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=0,
        n_jobs=4,
        verbose=-1,
    )
    model.fit(x_ordered, y_ordered, group=sizes)
    return pd.Series(model.predict(x_test), index=test.index)


def fold_blocks(dates, splits_count, seed):
    """Shuffled, contiguous date blocks - one block per fold's test side."""
    dates = np.array(sorted(dates))
    detector = np.random.default_rng(seed)
    detector.shuffle(dates)
    return np.array_split(dates, splits_count)


def run_folds(frame, args):
    """Train + simulate each fold; collect per-method pick tables and stats."""
    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()

    if args.split == "seasons":
        test_start = getattr(args, "test_start", TEST_START)
        blocks = [frame.loc[frame["date"] >= test_start, "date"].unique()]
    else:
        challenge_dates = frame.loc[challenge, "date"].unique()
        blocks = fold_blocks(challenge_dates, args.splits, args.seed)

    stats = []
    point_name = args.model
    if point_name == "ensemble":
        method_order = ["gpg", "hgpg", "live", "hgb", "logreg", "lgbm", "ensemble"]
    else:
        method_order = ["gpg", "hgpg", "live", point_name, "logreg"]
    if args.ranker != "none":
        method_order.append("ranker")
    method_order.append("random")
    pooled = {method: [] for method in method_order}
    out_rows = []
    aucs = []

    for number, block_dates in enumerate(blocks, start=1):
        test_dates = pd.Index(block_dates)
        test_all = frame[frame["date"].isin(test_dates)]
        # Sim rows: challenge rows on the held-out dates.
        test = test_all[test_all["tims"].isin([1, 2, 3]) & test_all["scored"].notna()].copy()
        if args.split == "kfold":
            train_all = frame[~frame["date"].isin(test_dates) & frame["scored"].notna()]
        else:
            train_all = frame[(frame["date"] < getattr(args, "test_start", TEST_START)) & frame["scored"].notna()]
            if getattr(args, "min_season", None) is not None:
                season_start = pd.to_numeric(train_all["season"].astype(str).str[:4], errors="coerce")
                train_all = train_all[season_start >= args.min_season]
        train = train_all[train_all["tims"].isin([1, 2, 3])] if args.train_subset == "challenge" else train_all

        sim = test.copy()
        for tag, col in (("gpg", "gpg"), ("hgpg", "hgpg")):
            baseline = test[col].astype(float).fillna(-np.inf)
            sim[tag] = baseline
            pooled[tag].append(daily_picks(sim, tag))

        rng = np.random.default_rng(1000 + number)
        sim["random"] = rng.random(len(sim))
        pooled["random"].append(daily_picks(sim, "random"))

        sim["live"] = live_score(test, train)
        pooled["live"].append(daily_picks(sim, "live"))

        if point_name == "ensemble":
            prob_hgb = fit_hgb(train, test)
            sim["hgb"] = prob_hgb.fillna(0).values
            prob_lgbm = fit_lgbm(train, test, args.lgbm_config)
            sim["lgbm"] = prob_lgbm.fillna(0).values
            prob_logreg = fit_logreg(train, test)
            sim["logreg"] = prob_logreg.fillna(0).values
            ranks = sim.groupby(["date", "tims"])[["hgb", "lgbm", "logreg"]].rank(pct=True)
            sim["ensemble"] = ranks.mean(axis=1)
            for method in ("hgb", "logreg", "lgbm", "ensemble"):
                pooled[method].append(daily_picks(sim, method))
            aucs.append(roc_auc_score(test["scored"].astype(int), sim["ensemble"]))
        else:
            fit_pointwise = {"hgb": fit_hgb, "lgbm": fit_lgbm, "extratrees": fit_extratrees}[point_name]
            if point_name == "lgbm":
                prob_point = fit_lgbm(train, test, args.lgbm_config)
            else:
                prob_point = fit_pointwise(train, test)
            sim[point_name] = prob_point.fillna(0).values
            pooled[point_name].append(daily_picks(sim, point_name))
            aucs.append(roc_auc_score(test["scored"].astype(int), prob_point))

            prob_logreg = fit_logreg(train, test)
            sim["logreg"] = prob_logreg.fillna(0).values
            pooled["logreg"].append(daily_picks(sim, "logreg"))

        if args.ranker != "none":
            prob_ranker = fit_ranker(train, test, args.ranker)
            sim["ranker"] = prob_ranker.fillna(0).values
            pooled["ranker"].append(daily_picks(sim, "ranker"))

        for method, picks in pooled.items():
            stats.append({"fold": number, "method": method, **score_picks(picks[-1], method)})
        if point_name == "ensemble":
            out_cols = [col for col in PICK_FILE_COLUMNS if col != "hgb"] + ["hgb", "lgbm", "ensemble"]
        else:
            out_cols = [point_name if col == "hgb" else col for col in PICK_FILE_COLUMNS]
        out_cols += (["ranker"] if args.ranker != "none" else []) + ["random"]
        out_rows.append(sim[out_cols].assign(fold=number))
        print(f"  fold {number}: {len(sim):,} sim rows across {test['date'].nunique()} dates", flush=True)

    return stats, pooled, pd.concat(out_rows, ignore_index=True), aucs


def report(stats, pooled, aucs, cover, args, out_rows=None):
    tag = f"{args.split} (k={args.splits})" if args.split == "kfold" else args.split
    print("\n=== join coverage ===")
    for key, value in cover.items():
        print(f"  {key}: {value}")

    print(f"\n=== {tag} | mean held-out AUC: {np.mean(aucs):.4f} +- {np.std(aucs):.4f} ===")

    per_fold = pd.DataFrame(stats).pivot(index="fold", columns="method", values="acc")
    methods = list(pooled.keys())
    per_fold = per_fold[methods]
    print("\nper-fold pick accuracy (%):")
    print(per_fold.round(1).to_string())

    pooled_picks = {method: pd.concat(parts, ignore_index=True) for method, parts in pooled.items()}
    summary_rows = []
    for method in methods:
        picks = pooled_picks[method]
        row = score_picks(picks, method)
        row["acc_std"] = per_fold[method].std()
        row["acc_mean"] = per_fold[method].mean()
        summary_rows.append(row)
    summary = pd.DataFrame(summary_rows).set_index("method")
    cols = ["acc_mean", "acc_std", "acc", "hits", "total", "hits_day", "days3", "days0", "days"]
    print("\npooled out-of-fold picks (acc_mean +/- acc_std over folds; pooled = all folds):")
    print(summary[cols].round(3).to_string())

    print("\nper-group (pooled picks, hit-%):")
    for method in methods:
        picks = pooled_picks[method]
        groups = picks.groupby("tims")["scored"].agg(["count", "sum"])
        g_txt = " ".join(f"g{int(tims)}={100 * row['sum'] / row['count']:.1f}" for tims, row in groups.iterrows())
        years = picks.assign(year=picks["date"].str[:4]).groupby("year")["scored"].agg(["count", "sum"])
        y_txt = " ".join(f"{year}={100 * row['sum'] / row['count']:.1f}" for year, row in years.iterrows())
        print(f"  {method:7s} {g_txt} | {y_txt}")

    if out_rows is not None:
        args.model_file.parent.mkdir(parents=True, exist_ok=True)
        out_rows.to_csv(args.model_file, index=False)
        print(f"\nsaved {len(out_rows):,} out-of-fold rows to {args.model_file}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="re-download the cached table CSVs")
    parser.add_argument(
        "--env",
        default=(ENV or "dev"),
        help="environment suffix of the backtrack tables (dev or prod)",
    )
    parser.add_argument("--split", choices=["kfold", "seasons"], default="kfold", help="evaluation split")
    parser.add_argument("--splits", type=int, default=5, help="number of k-fold blocks (kfold mode)")
    parser.add_argument(
        "--test-start",
        default=TEST_START,
        help="seasons split: first held-out date (default 2024-08-01).",
    )
    parser.add_argument(
        "--min-season",
        type=int,
        default=None,
        help="seasons split: drop training seasons starting before this year.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="date-shuffle seed for kfold (default 42; V3d and earlier used 0)",
    )
    parser.add_argument(
        "--train-subset",
        choices=["challenge", "all"],
        default="all",
        help="train on every scored row ('all', default - stronger under CV) or only tims-group rows "
        "('challenge', distribution-matched but weaker)",
    )
    parser.add_argument(
        "--model",
        choices=["ensemble", "hgb", "lgbm", "extratrees"],
        default="ensemble",
        help="rank ensemble hgb+logreg+lgbm (default), or a single pointwise model.",
    )
    parser.add_argument(
        "--lgbm-config",
        choices=["a", "b", "c"],
        default="a",
        help="LightGBM config when --model lgbm: a=400/0.03/31 (default), b=600/0.02/15/reg5, c=800/0.02/63.",
    )
    parser.add_argument(
        "--lineup",
        choices=["none", "projected", "oracle"],
        default="none",
        help="lineup block: none (default), projected method b, or oracle (actual same-game units, upper bound only).",
    )
    parser.add_argument(
        "--ranker",
        choices=["none", "lambdarank"],
        default="none",
        help="add a within-(date,tims)-group ranking model. 'lambdarank' uses LightGBM's "
        "LGBMRanker when installed, else pairwise logistic on positive/negative pairs.",
    )
    parser.add_argument(
        "--source",
        choices=FRAME_SOURCES,
        default="sqlite",
        help="where the shot/TOI block and club identity come from: sqlite "
        "(default; shot_features()/club_map() over the raw store) or supabase "
        "(the columns published by the shot-columns migration).",
    )
    parser.add_argument("--model-file", type=Path, help="save pooled out-of-fold rows with predictions here")
    args = parser.parse_args()

    global NUMERIC_FEATURES, CATEGORICAL_FEATURES, DATASET_FEATURES  # noqa: PLW0603
    if args.lineup == "projected":
        NUMERIC_FEATURES = NUMERIC_FEATURES + PROJECTED_LINEUP_NUMERIC
        CATEGORICAL_FEATURES = CATEGORICAL_FEATURES + PROJECTED_LINEUP_CATEGORICAL
    elif args.lineup == "oracle":
        CATEGORICAL_FEATURES = CATEGORICAL_FEATURES + ["lineup_unit", "pp_unit"]
    DATASET_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

    players, teams, goalies = load_frames(args.env, refresh=args.refresh)
    frame = build_frame(players, teams, goalies, source=args.source)
    frame = add_derived(frame, teams, source=args.source)

    cover = {
        "date range": f"{frame['date'].min()} .. {frame['date'].max()}",
        "player rows": f"{len(frame):,}",
        "rows with team stats": f"{frame['tgpg'].notna().sum():,}",
        "rows with opp goalie": f"{frame['opp_gaa'].notna().sum():,}",
        "rows with hgpg": f"{frame['hgpg'].notna().sum():,}",
        "rows with rank features": f"{frame['rank_gpg'].notna().sum():,}",
        "rows with clubs joined": f"{frame['club'].notna().sum():,}",
        "rows with shot features": f"{frame['sd_shots_pg'].notna().sum():,}",
        "rows in tims groups": f"{(frame['tims'].isin([1, 2, 3])).sum():,}",
    }

    stats, pooled, out_rows, aucs = run_folds(frame, args)
    report(stats, pooled, aucs, cover, args, out_rows if args.model_file else None)
    print("\npicks hit = the chosen player scored in that game; 3 picks per date (one per tims group).")


if __name__ == "__main__":
    main()
