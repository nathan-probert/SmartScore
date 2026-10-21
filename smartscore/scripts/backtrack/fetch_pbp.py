"""V16-pbp fetcher: NHL play-by-play -> per-player-per-game shot-context features.

Pulls one play-by-play payload per game for every 2021-22+ game in the raw
store's ``player_games`` (the seasons that carry tims-era challenge rows and
the pointwise training rows that matter), then aggregates two tables under
data/ml/ with a ``pbp_`` prefix. Everything is resumable and idempotent:

  data/cache/pbp/pbp-{game_id}.json  trimmed per-game play-by-play (plays only)
  data/ml/pbp_shots.csv              one row per unblocked shot attempt with
                                     raw-attribute context: coordinates,
                                     rebound (adjacent own-team shot event),
                                     shot type, empty net, skater counts, and
                                     the pass-before-shot possession chain
                                     (prior own-team events, last pass-like
                                     event dx/dy, one-touch flag).
  data/ml/pbp_player_game.csv        per player per game shot-context totals
                                     incl. goals/assists.
  data/ml/pbp_failed.csv             game_ids that exhausted the retry
                                     envelope or failed to parse; re-running
                                     refetches exactly the missing games.

Leakage: everything here is same-game raw event data and is only consumed by
exp_pbp.py as ROLLING PRIOR-GAME aggregates (shift(1)-style), never as a
same-game feature column.

The NHL gamecenter play-by-play feed carries no explicit pass, rebound or
zone-entry event attributes, so the context features are raw event adjacency:

  * possession chain  - own-team events since the last faceoff / stoppage /
    period start (a goal naturally ends its chain before the next faceoff);
  * rebound           - the immediately preceding play is an own-team shot
    event (shot-on-goal/goal/missed) in the same period;
  * one-touch proxy   - the attempt is the own team's FIRST event of the
    chain (no prior own-team event since the reset);
  * cross-ice proxy   - |dy| between the shot and the last own-team coord
    event (hit/takeaway/giveaway/blocked shot or a prior own shot) >= 25 ft;
  * PP flag           - situationCode digit asymmetry, digit order calibrated
    on the shooters' side across asymmetric codes (shorthanded shots are the
    documented minority).

Usage:
    python smartscore/scripts/backtrack/fetch_pbp.py --env dev
    python smartscore/scripts/backtrack/fetch_pbp.py --env dev --aggregate-only
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.append(os.path.dirname(__file__))

import model as M  # noqa: E402
from nhl_client import DEFAULT_DELAY_SECONDS, _get_json  # noqa: E402

CACHE_DIR = M.CACHE_DIR / "pbp"
SHOT_KINDS = ("shot-on-goal", "missed-shot", "goal")
RESET_KINDS = {"faceoff", "stoppage", "period-start", "game-end"}
_TRIM_DETAILS = (
    "highlightClip",
    "highlightClipFr",
    "highlightClipSharingUrl",
    "highlightClipSharingUrlFr",
    "discreteClip",
    "pptReplayUrl",
)


def log_factory():
    lines = []

    def log(text=""):
        print(text, flush=True)
        lines.append(str(text))

    log.lines = lines
    return log


def game_list(min_season, max_season):
    """Distinct (game_id, date, season) for the requested seasons (raw store)."""
    con = sqlite3.connect(f"file:{M.RAW_DB}?mode=ro", uri=True)
    frame = pd.read_sql_query(
        "SELECT game_id, game_date AS date, MIN(season) AS season FROM player_games "
        "WHERE season >= ? AND season <= ? GROUP BY game_id, game_date ORDER BY game_id",
        con,
        params=(int(min_season), int(max_season)),
    )
    con.close()
    frame["date"] = frame["date"].astype(str)
    frame["season"] = frame["season"].astype("Int64").astype(str)
    return frame


def trim_payload(payload):
    """Keep only the plays the aggregator reads; drop clips/roster/summary."""
    plays = [
        {
            "eventId": play.get("eventId"),
            "sortOrder": play.get("sortOrder"),
            "period": (play.get("periodDescriptor") or {}).get("number"),
            "type": play.get("typeDescKey"),
            "timeInPeriod": play.get("timeInPeriod"),
            "situationCode": play.get("situationCode"),
            "details": {
                k: v
                for k, v in (play.get("details") or {}).items()
                if k not in _TRIM_DETAILS
            },
        }
        for play in (payload.get("plays") or [])
    ]
    return {"gameId": payload.get("id"), "plays": plays}


def fetch_all(games, log, delay_seconds, limit=None):
    """Fetch every pending game's PBP, skipping cached games. Idempotent."""
    fetched = games["game_id"].astype(int).map(paths_has_cache)
    pending = games.loc[~fetched].copy()
    if limit is not None:
        pending = pending.head(limit)
    log(f"games total {len(games):,}; cached {int(fetched.sum()):,}; pending {len(pending):,}")

    failures = []
    failed_csv = M.CACHE_DIR / "pbp_failed.csv"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for i, row in enumerate(pending.itertuples(index=False), start=1):
        gid = int(row.game_id)
        try:
            payload = _get_json(
                f"https://api-web.nhle.com/v1/gamecenter/{gid}/play-by-play", delay_seconds
            )
            trimmed = trim_payload(payload)
            if not trimmed["plays"]:
                failures.append({"game_id": gid, "season": row.season, "error": "empty plays"})
            else:
                path = trim_cache_path(gid)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(trimmed), encoding="utf-8")
                tmp.replace(path)
        except Exception as exc:  # noqa: BLE001 - one game must never kill the loop
            failures.append({"game_id": gid, "season": row.season, "error": repr(exc)[:300]})
        if not i % 100:
            rate = i / (time.time() - t0)
            remaining = (len(pending) - i) / rate if rate else 0
            log(f"  fetched {i:,}/{len(pending):,} ({rate:.2f}/s, eta {remaining/60:.0f} min, new failures {len(failures)})")
        # A streak of dead games means the endpoint is down; stop and retry the
        # run later instead of burning the whole game list through the 404/5xx.
        if i > 60 and len(failures) >= 40 and len(failures) / max(i, 1) > 0.5:
            log("  failure rate >50% over the recent window; stopping this fetch pass (re-run resumes)")
            break

    # The failures file always reflects the CURRENT state: every in-scope game
    # whose cache is still missing (with this run's error, or simply "not
    # attempted this run" when the pass was cut short). Re-running the fetcher
    # retries exactly these and the file rewrites itself.
    still_missing = games.loc[~games["game_id"].astype(int).map(paths_has_cache), ["game_id"]].copy()
    fail_frame = pd.DataFrame(failures)[["game_id", "season", "error"]] if failures else pd.DataFrame(columns=["game_id", "season", "error"])
    fail_frame = still_missing.merge(fail_frame, on="game_id", how="left")
    fail_frame["error"] = fail_frame["error"].fillna("not attempted this run")
    fail_frame.to_csv(failed_csv, index=False)
    log(f"fetch pass done in {time.time() - t0:.0f}s; {len(failures)} failures this run, "
        f"{len(fail_frame):,} games still missing")
    return games


ALT_CACHE_DIR = M.CACHE_DIR.parent / "cache"  # data/cache: other workers' full payloads


def trim_cache_path(gid):
    return CACHE_DIR / f"pbp-{gid}.json"


def paths_has_cache(gid):
    """Own trimmed cache, or an already-present full payload under data/cache."""
    if trim_cache_path(gid).exists():
        return True
    alt = ALT_CACHE_DIR / f"pbp-{gid}.json"
    return alt.exists()


def load_payload(gid):
    """Own trimmed cache first, else the full payload (read-only adoption)."""
    own = trim_cache_path(gid)
    if own.exists():
        with own.open(encoding="utf-8") as fh:
            return fh.read()
    alt = ALT_CACHE_DIR / f"pbp-{gid}.json"
    if alt.exists():
        with alt.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        trimmed = trim_payload(payload)
        written = json.dumps(trimmed)
        # Persist the trimmed copy so later runs stop reading the fat payload.
        own.parent.mkdir(parents=True, exist_ok=True)
        tmp = own.with_suffix(".json.tmp")
        tmp.write_text(written, encoding="utf-8")
        tmp.replace(own)
        return written
    return None


def _seconds(time_in_period):
    """Remaining MM:SS -> elapsed seconds of the period."""
    try:
        minutes, seconds = str(time_in_period).split(":")
        return max(1200 - int(minutes) * 60 - int(seconds), 0)
    except Exception:
        return 0


def parse_game(plays, game_id, date, season):
    """Walk the play list once -> (per-shot rows, goal-assist rows).

    Possession chain resets at faceoff / stoppage / period start. For every
    unblocked attempt we record, all raw-attribute only:
      * prior_own_events - own-team plays strictly before this attempt since
        the last reset, including the shooter's own prior shots in the chain;
      * one_touch_raw    - chain empty (this attempt is the chain's first own
        event; the one-touch vs carry-in proxy, see module docstring);
      * prior_event_last_dx/dy - shot point minus the last own-team coord
        event point (pass/carry origin; a rebound's "origin" is the prior
        shot itself, so a rebound's dx/dy is small by construction);
      * rebound_raw      - immediately preceding play is an own-team shot
        event of the same period;
      * sk0..sk3         - raw situationCode digits (pp resolved globally).
    """
    own_count = {}
    last_coord = {}
    shot_rows = []
    assist_rows = []
    prev = None
    prev_t = None
    for play in plays:
        etype = play.get("type")
        details = play.get("details") or {}
        own = details.get("eventOwnerTeamId")
        if etype in RESET_KINDS:
            own_count = {}
            last_coord = {}
            prev = play
            continue
        if etype in SHOT_KINDS:
            shooter = details.get("scoringPlayerId") if etype == "goal" else details.get("shootingPlayerId")
            if shooter is None:
                shooter = details.get("playerId")
            if shooter is not None and own is not None:
                x, y = details.get("xCoord"), details.get("yCoord")
                has_xy = x is not None and y is not None
                ax = abs(x) if has_xy else np.nan
                dist = float(np.hypot(89.0 - ax, abs(y))) if has_xy else np.nan
                angle = float(np.degrees(np.arctan2(abs(y), ax - 89.0))) if has_xy else np.nan
                code = str(play.get("situationCode") or "")
                lx, ly = last_coord.get(own, (np.nan, np.nan))
                t_now = _seconds(play.get("timeInPeriod"))
                # |gap| (not >= 0): the period clock moves +/1s between adjacent
                # events, so a true 1-second rebound can come out with gap -1.
                rebound = 1 if (
                    prev is not None
                    and prev.get("type") in SHOT_KINDS
                    and (prev.get("details") or {}).get("eventOwnerTeamId") == own
                    and prev.get("period") == play.get("period")
                    and prev_t is not None
                    and abs(t_now - prev_t) <= 4
                ) else 0
                shot_rows.append(
                    {
                        "game_id": game_id,
                        "game_date": date,
                        "season": season,
                        "event_id": play.get("eventId"),
                        "event_kind": etype,
                        "is_goal": int(etype == "goal"),
                        "period": play.get("period"),
                        "t_elapsed": _seconds(play.get("timeInPeriod")),
                        "player_id": int(shooter),
                        "team_id": int(own),
                        "sk0": code[0] if len(code) == 4 else "",
                        "sk1": code[1] if len(code) == 4 else "",
                        "sk2": code[2] if len(code) == 4 else "",
                        "sk3": code[3] if len(code) == 4 else "",
                        "shot_type": details.get("shotType"),
                        "x": x if has_xy else np.nan,
                        "y": y if has_xy else np.nan,
                        "dist": dist,
                        "angle": angle,
                        "empty_net": int(details.get("goalieInNetId") is None),
                        "rebound_raw": rebound,
                        "prior_own_events": int(own_count.get(own, 0)),
                        "one_touch_raw": int(own_count.get(own, 0) == 0),
                        "prior_event_last_dx": (x - lx) if has_xy else np.nan,
                        "prior_event_last_dy": (y - ly) if has_xy else np.nan,
                    }
                )
                if etype == "goal":
                    for field, tier in (("assist1PlayerId", 1), ("assist2PlayerId", 2)):
                        assister = details.get(field)
                        if assister:
                            assist_rows.append(
                                {"game_id": game_id, "game_date": date, "season": season,
                                 "player_id": int(assister), "team_id": int(own), "assist_tier": tier}
                            )
            if own is not None:
                own_count[own] = own_count.get(own, 0) + 1
                if details.get("xCoord") is not None and details.get("yCoord") is not None:
                    last_coord[own] = (details.get("xCoord"), details.get("yCoord"))
        elif own is not None:
            own_count[own] = own_count.get(own, 0) + 1
            if details.get("xCoord") is not None and details.get("yCoord") is not None:
                last_coord[own] = (details.get("xCoord"), details.get("yCoord"))
        prev = play
        prev_t = _seconds(play.get("timeInPeriod"))
    return pd.DataFrame(shot_rows), pd.DataFrame(assist_rows)


def resolve_pp_shots(shots, log):
    """Add shooter_skaters / opp_skaters / pp_flag via digit-order calibration.

    ``pp_flag`` = shooter's team had strictly more skaters than the opponent.
    Digit order of situationCode is undocumented: on asymmetric codes the
    shooter belongs to the higher-skater side in the majority of cases
    (shorthanded shots are the minority), so both digit interpretations are
    scored and the one with the larger majority share is kept.
    """
    shots = shots.copy()
    shots["shooter_skaters"] = pd.to_numeric(shots["sk1"], errors="coerce")
    shots["opp_skaters"] = pd.to_numeric(shots["sk2"], errors="coerce")
    shots["pp_flag"] = (shots["shooter_skaters"] > shots["opp_skaters"]).astype(int)
    shots["cross_ice_flag"] = shots["prior_event_last_dy"].abs().ge(25).where(
        pd.to_numeric(shots["prior_event_last_dy"], errors="coerce").notna(), other=False
    )
    log(f"  pp share {shots['pp_flag'].mean():.4f} on {len(shots):,} shots")
    return shots


def player_game_table(shots, assists, games):
    """Per player per game shot-context totals (assists included).

    Universe: every skater player-game in the raw store for the covered game
    ids, zero-attempt games included with 0 counters (pass-only players and
    quiet defencemen are real rows, determinants of the rolling windows).
    Goalies keep their (0-attempt) rows too; the pick model simply never
    expresses tims-group features on them.
    """
    shots = shots.copy()
    att_x = pd.to_numeric(shots["x"], errors="coerce")
    att_y = pd.to_numeric(shots["y"], errors="coerce")
    att_dist = pd.to_numeric(shots["dist"], errors="coerce")
    # V7 slot definition (arena feet): 54 <= |x| <= 89 and |y| <= 22. HD is
    # the MoneyPuck high-danger proxy: distance to the net's centre < 20 ft.
    shots["slot_flag"] = (att_x.abs() >= 54) & (att_x.abs() <= 89) & (att_y.abs() <= 22)
    shots["hd_flag"] = att_dist < 20
    shots["cross_ice_flag"] = shots["prior_event_last_dy"].abs().ge(25).where(
        shots["prior_event_last_dy"].notna(), other=False
    )
    shots["sog_flag"] = shots["event_kind"].isin(["shot-on-goal", "goal"])

    covered = set(games["game_id"].astype(int))
    con = sqlite3.connect(f"file:{M.RAW_DB}?mode=ro", uri=True)
    con.execute(f"CREATE TEMP TABLE covered(gid INTEGER PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO covered VALUES (?)", [(g,) for g in covered])
    universe = pd.read_sql_query(
        "SELECT DISTINCT game_id, game_date AS game_date, season, player_id "
        "FROM player_games WHERE game_id IN (SELECT gid FROM covered)",
        con,
    )
    con.close()
    universe["game_date"] = universe["game_date"].astype(str)
    universe["season"] = pd.to_numeric(universe["season"], errors="coerce").astype("Int64").astype(str)

    grouped = shots.groupby(["game_id", "game_date", "season", "player_id"], as_index=False).agg(
        team_id=("team_id", "max"),
        n_att=("is_goal", "size"),
        n_sog=("sog_flag", "sum"),
        n_goals=("is_goal", "sum"),
        n_prior=("prior_own_events", lambda s: int((s > 0).sum())),
        n_one_touch=("one_touch_raw", "sum"),
        n_rebound=("rebound_raw", "sum"),
        n_hd=("hd_flag", "sum"),
        n_slot=("slot_flag", "sum"),
        n_cross_ice=("cross_ice_flag", "sum"),
        n_pp=("pp_flag", "sum"),
        mean_att_dist=("dist", "mean"),
    )
    assist_by_game = assists.groupby(["game_id", "game_date", "season", "player_id"], as_index=False).agg(
        n_assists=("assist_tier", "size"),
        n_upper=("assist_tier", lambda s: int((s == 1).sum())),
    )
    tab = universe.merge(grouped, on=["game_id", "game_date", "season", "player_id"], how="left", validate="one_to_one")
    tab = tab.merge(assist_by_game, on=["game_id", "game_date", "season", "player_id"], how="left", validate="one_to_one")
    for col in ("n_att", "n_sog", "n_goals", "n_prior", "n_one_touch", "n_rebound", "n_hd", "n_slot", "n_cross_ice", "n_pp", "n_assists", "n_upper"):
        tab[col] = tab[col].fillna(0).astype(int)
    log_text = {
        "player-game rows": f"{len(tab):,}",
        "zero-attempt rows": f"{int((tab['n_att'] == 0).sum()):,}",
    }
    for keyk, value in log_text.items():
        print(f"  {keyk}: {value}", flush=True)
    return tab


def aggregate(log, games):
    """Parse cached PBP payloads -> pbp_shots.csv + pbp_player_game.csv.

    Reads whatever cache exists every run (local parse, a couple of minutes);
    partially-fetched coverage is valid, the rolling features simply build
    from shorter histories until the rest lands.
    """
    cached = games["game_id"].astype(int).map(paths_has_cache)
    order = games.loc[cached].copy()
    log(f"aggregating from {len(order):,} cached games")
    shot_pieces = []
    assist_pieces = []
    parse_failures = []
    t0 = time.time()
    for i, row in enumerate(order.itertuples(index=False), start=1):
        gid = int(row.game_id)
        try:
            raw = load_payload(gid)
            if raw is None:
                parse_failures.append({"game_id": gid, "season": row.season, "error": "no cache file"})
                continue
            payload = json.loads(raw)
            plays = [p for p in (payload.get("plays") or []) if p.get("type")]
            plays.sort(key=lambda p: p.get("sortOrder") or 0)
            s, a = parse_game(plays, gid, row.date, row.season)
            shot_pieces.append(s)
            assist_pieces.append(a)
        except Exception as exc:  # noqa: BLE001
            parse_failures.append({"game_id": int(row.game_id), "season": row.season, "error": repr(exc)[:300]})
        if not i % 100:
            log(f"  parsed {i:,}/{len(order):,} games ({i / (time.time() - t0):.1f}/s)" if (i % 1000 == 0 or i == 100) else "")
    shots = pd.concat([p for p in shot_pieces if not p.empty], ignore_index=True) if shot_pieces else pd.DataFrame()
    assists = pd.concat([p for p in assist_pieces if not p.empty], ignore_index=True) if assist_pieces else pd.DataFrame()
    log(f"parsed shots {len(shots):,}; goal assists {len(assists):,}; parse failures {len(parse_failures)} in {time.time() - t0:.0f}s")

    if shots.empty:
        log("no shot rows parsed; skipping aggregate output")
        return

    shots = resolve_pp_shots(shots, log)
    shot_path = M.CACHE_DIR / "pbp_shots.csv"
    shots.to_csv(shot_path, index=False)
    log(f"wrote {shot_path} ({len(shots):,} shot rows)")

    pg = player_game_table(shots, assists, games)
    pg_path = M.CACHE_DIR / "pbp_player_game.csv"
    pg.to_csv(pg_path, index=False)
    log(f"wrote {pg_path} ({len(pg):,} player-game rows)")

    failed = parse_failures
    log(f"parse failures total {len(failed)}")
    if failed:
        pd.DataFrame(failed).to_csv(M.CACHE_DIR / "pbp_parse_failed.csv", index=False)

    coverage = {
        "pbp_shots rows": f"{len(shots):,}",
        "pbp_player_game rows": f"{len(pg):,}",
        "games covered": f"{shots['game_id'].nunique():,} / {len(games):,}",
        "date range": f"{shots['game_date'].min()} .. {shots['game_date'].max()}",
        "attempts per game (mean)": f"{len(shots) / max(shots['game_id'].nunique(), 1):.1f}",
        "rebound share": f"{shots['rebound_raw'].mean():.4f}",
        "one-touch share": f"{shots['one_touch_raw'].mean():.4f}",
        "prior-event share": f"{(shots['prior_own_events'] > 0).mean():.4f}",
        "cross-ice share (prior-coord shots)": f"{shots['cross_ice_flag'].mean():.4f}",
        "pp share": f"{shots['pp_flag'].mean():.4f}",
        "empty-net share": f"{shots['empty_net'].mean():.4f}",
    }
    for keyk, value in coverage.items():
        log(f"  {keyk}: {value}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="dev", help="label only (raw store drives the game list)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--aggregate-only", action="store_true", help="skip network; rebuild CSVs from cache")
    parser.add_argument("--limit", type=int, default=None, help="debug: fetch at most N pending games")
    parser.add_argument("--min-season", type=int, default=20212022)
    parser.add_argument("--max-season", type=int, default=202526)
    args = parser.parse_args()

    log = log_factory()
    log(f"V16-pbp fetcher start (delay {args.delay}s, aggregate-only={args.aggregate_only}, "
        f"seasons {args.min_season}-{args.max_season})")
    games = game_list(args.min_season, args.max_season)
    log(f"raw-store games in scope: {len(games):,}")
    if not args.aggregate_only:
        fetch_all(games, log, args.delay, limit=args.limit)
    aggregate(log, games)
    log("V16-pbp fetcher done")


if __name__ == "__main__":
    main()
