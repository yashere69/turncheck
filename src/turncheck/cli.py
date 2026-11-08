"""turncheck command line."""

from __future__ import annotations

import argparse
import io
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


def _utf8_output() -> None:
    """Write UTF-8 even when output is redirected on Windows, where it would otherwise use
    the locale's code page and fail on Devanagari or Hinglish text."""
    for stream in (sys.stdout, sys.stderr):
        enc = (stream.encoding or "").lower().replace("-", "")
        if enc != "utf8" and isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    ap = argparse.ArgumentParser(prog="turncheck", description=__doc__)
    ap.add_argument("--version", action="version", version=f"turncheck {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run scenarios against an agent")
    run.add_argument("targets", nargs="+", metavar="target", help="scenario files or directories")
    run.add_argument(
        "--agent",
        required=True,
        help='command that starts the agent, e.g. "python examples/agents/shopagent.py"',
    )
    run.add_argument(
        "--env",
        action="append",
        default=[],
        help="extra KEY=VALUE for the agent; VALUE may use {graph_api_base}, {app_secret}, "
        "{access_token}, {verify_token}, {phone_number_id}, {port}, {trace_url}",
    )
    run.add_argument(
        "--webhook-path", default="/webhook", help="path the agent serves its webhook on"
    )
    run.add_argument("--junit", type=Path, help="write JUnit XML here")
    run.add_argument(
        "--transcripts", type=Path, help="write each conversation as JSONL (chatlens traces)"
    )
    run.add_argument(
        "--reply-timeout-ms",
        type=int,
        default=5000,
        help="how long an expect_reply or expect_tool waits for a slow agent (default 5000)",
    )
    run.add_argument(
        "--settle-ms",
        type=int,
        default=150,
        help="how long the agent must be quiet before expectations are checked",
    )

    lint = sub.add_parser("lint", help="validate scenario files without running them")
    lint.add_argument("targets", nargs="+", metavar="target", help="scenario files or directories")

    args = ap.parse_args(argv)
    # Load everything before running anything, and report every broken file at once, so a
    # folder with three mistakes takes one edit-and-lint cycle rather than three.
    paths: list[Path] = []
    errors: list[str] = []
    for t in args.targets:
        try:
            found = discover(t)
        except ScenarioError as e:
            errors.append(str(e))
            continue
        if not found:
            errors.append(f"{t}: no scenario files (*.yaml, *.yml)")
        paths += [p for p in found if p not in paths]
    scenarios = []
    for p in paths:
        try:
            scenarios.append(load(p))
        except ScenarioError as e:
            errors.append(str(e))
    if errors:
        for msg in errors:
            print(f"error: {msg}", file=sys.stderr)
        return 2

    if args.cmd == "lint":
        for s in scenarios:
            print(f"  ok  {s.path}  ({len(s.steps)} steps)")
        return 0

    env = _parse_env(args.env)
    results = [
        run_scenario(s, args.agent, env, args.settle_ms, args.webhook_path, args.reply_timeout_ms)
        for s in scenarios
    ]
    print(report.console(results))
    if args.junit:
        args.junit.write_text(report.junit(results), encoding="utf-8")
    if args.transcripts:
        args.transcripts.write_text(report.transcripts(results), encoding="utf-8")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
