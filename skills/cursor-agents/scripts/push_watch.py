#!/usr/bin/env python3
"""Wake-up check for a fleet, with no LLM and no Cursor call: which worker branches stopped moving.

  push_watch.py --remote <url-or-name> --state <file.json> --stale-minutes N BRANCH [BRANCH...]

One `git ls-remote --heads` per run; the state file keeps, per branch, its SHA and when that SHA
was first seen, since git gives no push date. A branch is stale when its SHA has not changed for
N minutes, or when it is still absent N minutes after it was first watched. Name every expected
branch (`fleet-x/w01 fleet-x/w02 ...`): only a named branch can be reported missing. A BRANCH
ending in `*` is a prefix (`fleet-x/w*`) that matches branches that exist now, and the ones of
earlier runs kept in the state file, so a branch deleted after it was seen still counts.

Prints one JSON object (branches, stale, missing). Exit 0: nothing stale; 2: something is stale or
missing past N minutes (the moment to wake the coordinator); 1: git or usage error. Run it from cron
or a loop, and only start an LLM session on exit 2. Stdlib only; uses the caller's git credentials.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

STALE_EXIT = 2


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remote", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--stale-minutes", type=float, required=True)
    parser.add_argument("branches", nargs="+")
    args = parser.parse_args(argv)
    try:
        listed = subprocess.run(
            ["git", "ls-remote", "--heads", args.remote],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": f"git ls-remote failed: {type(exc).__name__}"}))
        return 1
    heads = {}
    for line in listed.splitlines():
        sha, _tab, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha
    state = _load(args.state)
    watched = set()
    for pattern in args.branches:
        if pattern.endswith("*"):
            watched.update(name for name in {*heads, *state} if name.startswith(pattern[:-1]))
        else:
            watched.add(pattern)
    now = dt.datetime.now(dt.UTC)
    limit = args.stale_minutes * 60
    report = {}
    for name in sorted(watched):
        sha = heads.get(name)
        seen = state.get(name)
        if seen is None or seen.get("sha") != sha or _since(seen) is None:
            seen = {"sha": sha, "since": now.isoformat(timespec="seconds")}
            state[name] = seen
        age = (now - (_since(seen) or now)).total_seconds()
        report[name] = {"sha": sha, "since": seen["since"], "age_minutes": round(age / 60, 1), "stale": age >= limit}
    _save(args.state, state)
    stale = [name for name, row in report.items() if row["stale"]]
    missing = [name for name, row in report.items() if row["sha"] is None]
    print(json.dumps({"checked_at": now.isoformat(timespec="seconds"), "branches": report, "stale": stale, "missing": missing}))
    return STALE_EXIT if stale else 0


def _since(seen: object) -> dt.datetime | None:
    """A state entry's clock, or None if the entry is damaged (it then restarts)."""
    raw = seen.get("since") if isinstance(seen, dict) else None
    try:
        parsed = dt.datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A damaged state only restarts the clocks: never a false alarm.
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: seen for name, seen in data.items() if isinstance(seen, dict)}


def _save(path: str, state: dict) -> None:
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
