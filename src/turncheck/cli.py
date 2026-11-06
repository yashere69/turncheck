"""turncheck command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, report
from .runner import run_scenario
from .scenario import ScenarioError, discover, load


def _parse_env(pairs: list[str]) -> dict:
    env = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"--env expects KEY=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        env[k] = v
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="turncheck", description=__doc__)
    ap.add_argument("--version", action="version", version=f"turncheck {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run scenarios against an agent")
    run.add_argument("target", help="scenario file or directory")
    run.add_argument("--agent", required=True, help='command that starts the agent, e.g. '
                     '"python examples/agents/shopagent.py"')
    run.add_argument("--env", action="append", default=[], help="extra KEY=VALUE for the agent")
    run.add_argument("--junit", type=Path, help="write JUnit XML here")
    run.add_argument("--settle-ms", type=int, default=150,
                     help="how long the agent must be quiet before expectations are checked")

    lint = sub.add_parser("lint", help="validate scenario files without running them")
    lint.add_argument("target")

    args = ap.parse_args(argv)
    try:
        paths = discover(args.target)
        scenarios = [load(p) for p in paths]
    except ScenarioError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not scenarios:
        print(f"no scenarios found in {args.target}", file=sys.stderr)
        return 2

    if args.cmd == "lint":
        for s in scenarios:
            print(f"  ok  {s.path}  ({len(s.steps)} steps)")
        return 0

    env = _parse_env(args.env)
    results = [run_scenario(s, args.agent, env, args.settle_ms) for s in scenarios]
    print(report.console(results))
    if args.junit:
        args.junit.write_text(report.junit(results), encoding="utf-8")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
