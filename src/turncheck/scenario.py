"""Scenario files: a customer, and an ordered list of steps.

Stimulus steps (send, press, redeliver, status, event, send_tampered, advance) change the world.
Expectation steps (expect_reply, expect_no_reply, expect_tool, expect_http) check what the agent
did in response to the most recent stimulus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .clock import parse_duration

STIMULI = {"send", "press", "redeliver", "status", "event", "send_tampered", "advance"}
EXPECTATIONS = {"expect_reply", "expect_no_reply", "expect_tool", "expect_http"}


class ScenarioError(ValueError):
    pass


@dataclass
class Step:
    kind: str
    arg: object
    ref: str | None = None  # label used by `redeliver` and `status`
    line: int = 0


@dataclass
class Scenario:
    name: str
    path: Path
    customer: dict
    steps: list[Step] = field(default_factory=list)
    env: dict = field(default_factory=dict)


def load(path: str | Path) -> Scenario:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "steps" not in data:
        raise ScenarioError(f"{path}: needs a top-level 'steps' list")
    customer = data.get("customer") or {"wa_id": "919800000001", "name": "Test Customer"}
    if "wa_id" not in customer:
        raise ScenarioError(f"{path}: customer.wa_id is required")
    steps: list[Step] = []
    refs: set[str] = set()
    for i, raw in enumerate(data["steps"], start=1):
        if not isinstance(raw, dict):
            raise ScenarioError(f"{path}: step {i} must be a mapping")
        ref = raw.pop("as", None)
        kinds = [k for k in raw if k in STIMULI | EXPECTATIONS]
        unknown = [k for k in raw if k not in STIMULI | EXPECTATIONS]
        if unknown:
            raise ScenarioError(f"{path}: step {i} has unknown key(s) {unknown}")
        if len(kinds) != 1:
            raise ScenarioError(f"{path}: step {i} must have exactly one action, got {kinds}")
        kind = kinds[0]
        arg = raw[kind]
        if kind == "advance":
            arg = parse_duration(arg)
        if kind in {"redeliver"} and arg not in refs:
            raise ScenarioError(f"{path}: step {i} redelivers unknown message {arg!r}")
        if kind == "status" and not (isinstance(arg, dict) and "state" in arg):
            raise ScenarioError(f"{path}: step {i} status needs a 'state' (sent/delivered/read)")
        if ref:
            refs.add(ref)
        steps.append(Step(kind=kind, arg=arg, ref=ref, line=i))
    if steps and steps[0].kind in EXPECTATIONS:
        raise ScenarioError(f"{path}: the first step must be a stimulus")
    return Scenario(
        name=data.get("name", path.stem),
        path=path,
        customer=customer,
        steps=steps,
        env=data.get("env", {}),
    )


def discover(target: str | Path) -> list[Path]:
    target = Path(target)
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*.y*ml") if p.suffix in {".yml", ".yaml"})
