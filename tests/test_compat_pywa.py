"""turncheck against a bot built on pywa, an independent WhatsApp framework.

pywa parses the webhooks itself (signature check, duplicate skipping, BSUIDs), so these
tests fail if turncheck's payloads drift from what real consumers accept. Skipped unless
pywa, fastapi and uvicorn are installed (CI installs them in the `compat` job).
"""

from pathlib import Path

import pytest

pytest.importorskip("pywa")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from turncheck.runner import run_scenario  # noqa: E402
from turncheck.scenario import load  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BOT = f"python {ROOT / 'examples/agents/pywa_bot.py'}"
COMPAT = ROOT / "examples/compat"


@pytest.mark.parametrize("name", ["pywa-basics", "pywa-username"])
def test_pywa_bot(name):
    result = run_scenario(load(COMPAT / f"{name}.yaml"), BOT)
    assert result.ok, f"{result.first_failure}\n{result.agent_log}"


# Two things pywa 4.5.0 (the version CI pins) does differently from Meta's documented
# format. If either test starts failing, pywa changed: update the README's compat notes.


def test_pywa_reads_only_the_first_entry_of_a_batch():
    result = run_scenario(load(COMPAT / "pywa-batch.yaml"), BOT)
    assert not result.ok
    assert "step 3" in result.first_failure


def test_pywa_cannot_parse_a_failed_receipt_without_contacts():
    """Meta omits contacts[] from failed receipts; pywa 4.5.0 indexes it unconditionally,
    so on_message_status(filters.failed) handlers never run."""
    result = run_scenario(load(COMPAT / "pywa-failed-receipt.yaml"), BOT)
    assert result.ok  # the customer still received nothing, as expected
    assert "KeyError: 'contacts'" in result.agent_log
    assert "undelivered" not in result.agent_log
