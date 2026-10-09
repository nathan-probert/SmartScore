#!/usr/bin/env python
"""Run a backtrack reconstruction as a detached background task.

A full season is roughly half an hour of HTTP requests (discovery plus per-player
game logs), which outlives any single command invocation here. This wrapper runs
the crawl in its own process with a log file and a PID file, so it survives the
caller being interrupted and can be polled afterwards.

Mirrors the repo's background-task convention: state in ``data/bg/<name>.json``,
output in ``data/bg/<name>.log``.

Usage::

    python smartscore/scripts/backtrack/run_background.py --name bt-2023 --season 20232024 --write
    python smartscore/scripts/backtrack/run_background.py --name build-2023 --cmd build --season 20232024
    python smartscore/scripts/backtrack/run_background.py --name bt-2023 --status
    python smartscore/scripts/backtrack/run_background.py --name bt-2023 --tail 40
    python smartscore/scripts/backtrack/run_background.py --name bt-2023 --stop
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BG_DIR = REPO_ROOT / "data" / "bg"
RECONSTRUCT = REPO_ROOT / "smartscore" / "scripts" / "backtrack" / "reconstruct.py"
LOCAL_STORE = REPO_ROOT / "smartscore" / "scripts" / "backtrack" / "local_store.py"


def _state_path(name):
    return BG_DIR / f"{name}.json"


def _log_path(name):
    return BG_DIR / f"{name}.log"


def _read_state(name):
    path = _state_path(name)

    if not path.exists():
        return None

    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid):
    """True if ``pid`` is still running.

    ``os.kill(pid, 0)`` never delivers a signal - it only asks whether the
    process exists, and Python implements that probe on Windows as well as POSIX.
    A dead pid raises OSError, which reads as not running; any unexpected failure
    falls through to the conservative answer (assume alive) so the caller does
    not start a duplicate of a task that may still be going. A missing pid (no
    state, or a truncated state file) is not running.
    """
    if not pid:
        return False

    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except Exception:  # noqa: BLE001
        return True

    return True


def cmd_start(args):
    BG_DIR.mkdir(parents=True, exist_ok=True)

    existing = _read_state(args.name)
    if existing and _pid_alive(existing.get("pid")):
        print(f"'{args.name}' is already running (pid {existing['pid']}). Use --stop first.")
        return 1

    if args.cmd == "build":
        # local_store --build crawls whatever the cache misses (schedule, box
        # scores, game logs) and then derives, so it needs only the season.
        command = [sys.executable, str(LOCAL_STORE), "--build", args.season]
    else:
        command = [sys.executable, str(RECONSTRUCT), "--season", args.season]

        if args.write:
            command.append("--write")

        if args.delay is not None:
            command += ["--delay", str(args.delay)]

        if args.players:
            command += ["--players", args.players]

    # unbuffered so the log is readable while the task runs rather than only at
    # exit - a long crawl that buffers is indistinguishable from a hung one.
    log = _log_path(args.name).open("w", encoding="utf-8")

    env = os.environ.copy()
    env["ENV"] = args.env
    env["PYTHONUNBUFFERED"] = "1"

    # S603: subprocess with a shell-less argument list. Every element of `command`
    # is built from sys.executable and the flags parsed above - no shell, no
    # string interpolation into a command line, so there is no injection surface
    # here. This wrapper only ever launches reconstruct.py from the same repo.
    process = subprocess.Popen(  # noqa: S603
        command,
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    _state_path(args.name).write_text(
        json.dumps(
            {
                "name": args.name,
                "pid": process.pid,
                "command": command,
                "cmd": args.cmd,
                "env": args.env,
                "season": args.season,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"started '{args.name}' pid={process.pid} cmd={args.cmd} env={args.env} season={args.season}")
    print(f"log: {_log_path(args.name)}")
    print(f"tail it with: python {Path(__file__).name} --name {args.name} --tail 40")

    return 0


def cmd_status(args):
    state = _read_state(args.name)

    if not state:
        print(f"'{args.name}' has no state file - never started, or already cleared.")
        return 1

    alive = _pid_alive(state.get("pid"))
    print(f"pid     : {state.get('pid')} ({'running' if alive else 'not running'})")
    print(f"cmd     : {state.get('cmd', 'reconstruct')}")
    print(f"env     : {state.get('env')}")
    print(f"season  : {state.get('season')}")
    print(f"started : {state.get('started_at')}")
    print(f"log     : {_log_path(args.name)}")

    return 0 if alive else 1


def cmd_tail(args):
    path = _log_path(args.name)

    if not path.exists():
        print(f"no log for '{args.name}'")
        return 1

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.tail :]:
        print(line)

    return 0


def cmd_stop(args):
    state = _read_state(args.name)

    if not state:
        print(f"'{args.name}' has no state file.")
        return 1

    pid = state.get("pid")

    if not _pid_alive(pid):
        print(f"pid {pid} is not running.")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"could not signal pid {pid}: {e}")
        return 1

    print(f"sent SIGTERM to pid {pid}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--name", required=True, help="Task name; state and log files are named after it.")
    parser.add_argument(
        "--cmd",
        choices=("reconstruct", "build"),
        default="reconstruct",
        help="reconstruct = discovery crawl into Supabase; build = local_store --build into SQLite.",
    )
    parser.add_argument("--season", default="20232024")
    parser.add_argument("--env", default="dev", help="ENV for the target tables (default dev).")
    parser.add_argument("--delay", type=float, default=None)
    parser.add_argument("--players", default=None)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--tail", type=int, metavar="N")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()

    # Checked in priority order so --status after --start reads as status rather
    # than launching a second crawl.
    if args.stop:
        return cmd_stop(args)
    if args.status:
        return cmd_status(args)
    if args.tail is not None:
        return cmd_tail(args)

    return cmd_start(args)


if __name__ == "__main__":
    sys.exit(main())
