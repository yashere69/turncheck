"""Runs the example suite against the reference agent over real HTTP."""

from pathlib import Path

import pytest

from turncheck.report import junit
from turncheck.runner import run_scenario
from turncheck.scenario import discover, load

ROOT = Path(__file__).resolve().parent.parent
AGENT = f"python {ROOT / 'examples/agents/shopagent.py'}"
SCENARIOS = [load(p) for p in discover(ROOT / "examples/scenarios")]


def _run(bugs: str):
    # the reference agent answers in milliseconds, so a short reply timeout keeps this fast
    return {
        s.path.stem: run_scenario(s, AGENT, {"SHOPAGENT_BUGS": bugs}, reply_timeout_ms=1500)
        for s in SCENARIOS
    }


def test_clean_agent_passes_everything():
    results = _run("")
    assert all(r.ok for r in results.values()), {k: r.first_failure for k, r in results.items()}


@pytest.mark.parametrize(
    "bug, scenario",
    [
        ("dedup", "03-duplicate-delivery"),
        ("window", "04-window"),
        ("orderid", "02-order-status"),
        ("signature", "05-signature"),
        ("status", "06-status-receipts"),
        ("noretry", "07-rate-limit"),
        ("batch", "08-batched-webhook"),
        ("slowack", "09-fast-ack"),
        ("phoneonly", "10-username-customer"),
    ],
)
def test_each_seeded_bug_is_caught_by_its_scenario(bug, scenario):
    results = _run(bug)
    assert not results[scenario].ok
    others = [k for k, r in results.items() if k != scenario and not r.ok]
    assert others == [], f"{bug} also broke {others}"


def test_agent_that_never_starts_is_reported_not_raised(tmp_path):
    r = run_scenario(SCENARIOS[0], "python -c 'raise SystemExit(3)'")
    assert not r.ok and "exited during startup" in r.first_failure
    assert "<failure" in junit([r])
