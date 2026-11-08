"""Scenario files: a customer, and an ordered list of steps.

Stimulus steps (send, send_batch, press, redeliver, status, event, send_tampered, advance,
graph_error) change the world. Expectation steps (expect_reply, expect_no_reply, expect_tool,
expect_http) check what the agent did in response to the most recent stimulus.

Top-level options control how closely the run imitates Meta's delivery behaviour:
webhook_timeout_ms and webhook_retries (slow or failed acks are redelivered), and
window_errors (how a free-form send outside the 24-hour window is rejected).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .clock import parse_duration

STIMULI = {
    "send",
    "send_batch",
    "press",
    "redeliver",
    "status",
    "event",
    "send_tampered",
    "advance",
    "graph_error",
}
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
    allow_extra_replies: bool = False
    webhook_timeout_ms: int = 10_000
    webhook_retries: int = 1
    window_errors: str = "webhook"  # "webhook" (failed receipt), "response" (HTTP error), "off"


def _positive_int(path: Path, data: dict, key: str, default: int, minimum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScenarioError(f"{path}: {key} must be an integer >= {minimum}, got {value!r}")
    return value


def _graph_error(path: Path, i: int, arg) -> dict:
    if isinstance(arg, int) and not isinstance(arg, bool):
        arg = {"code": arg}
    if not isinstance(arg, dict) or not isinstance(arg.get("code"), int):
        raise ScenarioError(f"{path}: step {i} graph_error needs an integer 'code'")
    extra = set(arg) - {"code", "times", "via", "http", "message"}
    if extra:
        raise ScenarioError(f"{path}: step {i} graph_error has unknown key(s) {sorted(extra)}")
    times, via, http = arg.get("times", 1), arg.get("via", "response"), arg.get("http", 400)
    if not isinstance(times, int) or times < 1:
        raise ScenarioError(f"{path}: step {i} graph_error times must be >= 1")
    if via not in {"response", "webhook"}:
        raise ScenarioError(f"{path}: step {i} graph_error via must be 'response' or 'webhook'")
    if not isinstance(http, int) or not 400 <= http <= 599:
        raise ScenarioError(f"{path}: step {i} graph_error http must be a 4xx or 5xx status")
    return {**arg, "times": times, "via": via, "http": http}


def _expect_http(path: Path, i: int, arg) -> dict:
    """`expect_http: 401`, `expect_http: 4xx` or `{status: 200, within_ms: 500}`."""
    spec = arg if isinstance(arg, dict) else {"status": arg}
    status, within = spec.get("status"), spec.get("within_ms")
    ok_status = status is None or (
        isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
    )
    if isinstance(status, str):
        ok_status = status in {"2xx", "4xx", "5xx"}
    if not ok_status or set(spec) - {"status", "within_ms"}:
        raise ScenarioError(
            f"{path}: step {i} expect_http takes a status (401, 4xx) and/or within_ms"
        )
    if within is not None and (not isinstance(within, int) or within <= 0):
        raise ScenarioError(f"{path}: step {i} expect_http within_ms must be a positive integer")
    return {"status": status, "within_ms": within}


def load(path: str | Path) -> Scenario:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        at = f" at line {mark.line + 1}" if mark else ""
        why = getattr(e, "problem", None)
        raise ScenarioError(f"{path}: not valid YAML{at}" + (f": {why}" if why else "")) from e
    if not isinstance(data, dict) or "steps" not in data:
        raise ScenarioError(f"{path}: needs a top-level 'steps' list")
    customer = data.get("customer") or {"wa_id": "919800000001", "name": "Test Customer"}
    if not isinstance(customer, dict) or "wa_id" not in customer:
        raise ScenarioError(f"{path}: customer.wa_id is required")
    unknown_keys = set(customer) - {"wa_id", "name", "username", "user_id", "share_phone"}
    if unknown_keys:
        raise ScenarioError(f"{path}: customer has unknown key(s) {sorted(unknown_keys)}")
    customer = {k: v if k == "share_phone" else str(v) for k, v in customer.items()}
    if not isinstance(customer.get("share_phone", True), bool):
        raise ScenarioError(f"{path}: customer.share_phone must be true or false")
    if not customer["wa_id"].isdigit():
        raise ScenarioError(f"{path}: customer.wa_id must be digits, e.g. 919800000001")
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
        if kind == "send_batch" and not (
            isinstance(arg, list) and len(arg) >= 2 and all(isinstance(t, str) for t in arg)
        ):
            raise ScenarioError(f"{path}: step {i} send_batch needs a list of two or more texts")
        if kind == "graph_error":
            arg = _graph_error(path, i, arg)
        if kind == "expect_http":
            arg = _expect_http(path, i, arg)
        if ref:
            refs.add(ref)
        steps.append(Step(kind=kind, arg=arg, ref=ref, line=i))
    if not steps:
        raise ScenarioError(f"{path}: 'steps' is empty")
    if steps[0].kind in EXPECTATIONS:
        raise ScenarioError(f"{path}: the first step must be a stimulus")
    return Scenario(
        name=data.get("name", path.stem),
        path=path,
        customer=customer,
        steps=steps,
        env=data.get("env", {}),
        allow_extra_replies=bool(data.get("allow_extra_replies", False)),
        webhook_timeout_ms=_positive_int(path, data, "webhook_timeout_ms", 10_000, 50),
        webhook_retries=_positive_int(path, data, "webhook_retries", 1, 0),
        window_errors=_window_mode(path, data),
    )


def _window_mode(path: Path, data: dict) -> str:
    mode = data.get("window_errors", "webhook")
    if mode is False:  # YAML 1.1 reads a bare `off` as false
        mode = "off"
    if mode not in {"webhook", "response", "off"}:
        raise ScenarioError(f"{path}: window_errors must be webhook, response or off")
    return mode


def discover(target: str | Path) -> list[Path]:
    target = Path(target)
    if target.is_file():
        return [target]
    if not target.exists():
        raise ScenarioError(f"{target}: no such file or directory")
    return sorted(p for p in target.rglob("*.y*ml") if p.suffix in {".yml", ".yaml"})
