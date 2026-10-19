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
        [--refresh] [--split kfold|seasons] [--splits 5] \
        [--train-subset challenge|all] [--model-file PATH]

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

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "ml"
RAW_DB = Path(__file__).resolve().parents[3] / "data" / "raw_nhl.sqlite"
PAGE = 10_000

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
]

CATEGORICAL_FEATURES = ["lineup_unit", "lineup_position_group", "pp_unit", "tims"]

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


def fetch_table(table, order_cols, refresh=False, chunk=PAGE):
    """Download one table into a CSV cache and return it as a DataFrame."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{table.lower().replace('-', '_')}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path)

    print(f"fetching {table} ...", flush=True)
    start = time.time()
    query = SUPABASE_ADMIN_CLIENT.table(table).select("*")
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


def build_frame(players, teams, goalies):
    """Join the three Supabase tables into one row per player per game."""
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


def add_derived(frame, teams):
    """V2 features layered onto the joined frame. Everything stays pre-game.

    * rank_*       - percentile of the rate within the day's tims group. Only
                     that day's pre-game stats enter the ranking, and a
                     date-blocked fold holds the whole slate out together, so
                     no train row ever contributes to a test row's rank.
    * prevN_goals / games_since_goal - past scored labels shifted one row per
                     player, strictly earlier games only.
    * days_since_game (per player), club_rest_days / opp_rest_days (per club)
    """
    frame = frame.sort_values(["player_id", "date"]).reset_index(drop=True)
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

    # Within (date, tims group) percentiles of the four rates.
    groups = frame[frame["tims"].isin([1, 2, 3])]
    for col in ("gpg", "hgpg", "five_gpg", "ppg"):
        frame[f"rank_{col}"] = groups.groupby(["date", "tims"])[col].rank(pct=True)

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


def fit_logreg(train, test):
    model = make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        LogisticRegression(max_iter=2000),
    )
    model.fit(train[NUMERIC_FEATURES], train["scored"].astype(int))
    return pd.Series(model.predict_proba(test[NUMERIC_FEATURES])[:, 1], index=test.index)


def fold_blocks(dates, splits_count):
    """Shuffled, contiguous date blocks - one block per fold's test side."""
    dates = np.array(sorted(dates))
    detector = np.random.default_rng(0)
    detector.shuffle(dates)
    return np.array_split(dates, splits_count)


def run_folds(frame, args):
    """Train + simulate each fold; collect per-method pick tables and stats."""
    challenge = frame["tims"].isin([1, 2, 3]) & frame["scored"].notna()

    if args.split == "seasons":
        blocks = [frame.loc[frame["date"] >= TEST_START, "date"].unique()]
    else:
        challenge_dates = frame.loc[challenge, "date"].unique()
        blocks = fold_blocks(challenge_dates, args.splits)

    stats = []
    pooled = {method: [] for method in ("gpg", "hgpg", "live", "hgb", "logreg", "random")}
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
            train_all = frame[(frame["date"] < TEST_START) & frame["scored"].notna()]
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

        prob_hgb = fit_hgb(train, test)
        sim["hgb"] = prob_hgb.fillna(0).values
        pooled["hgb"].append(daily_picks(sim, "hgb"))
        aucs.append(roc_auc_score(test["scored"].astype(int), prob_hgb))

        prob_logreg = fit_logreg(train, test)
        sim["logreg"] = prob_logreg.fillna(0).values
        pooled["logreg"].append(daily_picks(sim, "logreg"))

        for method, picks in pooled.items():
            stats.append({"fold": number, "method": method, **score_picks(picks[-1], method)})
        out_rows.append(sim[PICK_FILE_COLUMNS + ["random"]].assign(fold=number))
        print(f"  fold {number}: {len(sim):,} sim rows across {test['date'].nunique()} dates", flush=True)

    return stats, pooled, pd.concat(out_rows, ignore_index=True), aucs


def report(stats, pooled, aucs, cover, args, out_rows=None):
    tag = f"{args.split} (k={args.splits})" if args.split == "kfold" else args.split
    print("\n=== join coverage ===")
    for key, value in cover.items():
        print(f"  {key}: {value}")

    print(f"\n=== {tag} | mean held-out AUC: {np.mean(aucs):.4f} +- {np.std(aucs):.4f} ===")

    per_fold = pd.DataFrame(stats).pivot(index="fold", columns="method", values="acc")
    per_fold = per_fold[["gpg", "hgpg", "live", "hgb", "logreg", "random"]]
    print("\nper-fold pick accuracy (%):")
    print(per_fold.round(1).to_string())

    pooled_picks = {method: pd.concat(parts, ignore_index=True) for method, parts in pooled.items()}
    summary_rows = []
    for method in ("gpg", "hgpg", "live", "hgb", "logreg", "random"):
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
    for method in ("gpg", "hgpg", "live", "hgb", "logreg", "random"):
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
        "--train-subset",
        choices=["challenge", "all"],
        default="all",
        help="train on every scored row ('all', default - stronger under CV) or only tims-group rows "
        "('challenge', distribution-matched but weaker)",
    )
    parser.add_argument("--model-file", type=Path, help="save pooled out-of-fold rows with predictions here")
    args = parser.parse_args()

    players, teams, goalies = load_frames(args.env, refresh=args.refresh)
    frame = build_frame(players, teams, goalies)
    frame = add_derived(frame, teams)

    cover = {
        "date range": f"{frame['date'].min()} .. {frame['date'].max()}",
        "player rows": f"{len(frame):,}",
        "rows with team stats": f"{frame['tgpg'].notna().sum():,}",
        "rows with opp goalie": f"{frame['opp_gaa'].notna().sum():,}",
        "rows with hgpg": f"{frame['hgpg'].notna().sum():,}",
        "rows with rank features": f"{frame['rank_gpg'].notna().sum():,}",
        "rows with clubs joined": f"{frame['club'].notna().sum():,}",
        "rows in tims groups": f"{(frame['tims'].isin([1, 2, 3])).sum():,}",
    }

    stats, pooled, out_rows, aucs = run_folds(frame, args)
    report(stats, pooled, aucs, cover, args, out_rows if args.model_file else None)
    print("\npicks hit = the chosen player scored in that game; 3 picks per date (one per tims group).")


if __name__ == "__main__":
    main()
