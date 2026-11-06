"""Run scenarios against an agent process over real HTTP.

Per scenario: start a Graph API mock, start a fresh agent process pointed at it, check the
webhook verification handshake, then play the steps. The agent is restarted for every
scenario so in-memory state cannot leak between tests.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import envelope
from .assertions import check_reply, check_tool, describe_message
from .clock import SimClock
from .graph_mock import GraphMock
from .scenario import Scenario

APP_SECRET = "turncheck-test-app-secret"
ACCESS_TOKEN = "turncheck-test-token"
VERIFY_TOKEN = "turncheck-verify"


@dataclass
class StepResult:
    line: int
    kind: str
    ok: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    scenario: Scenario
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    transcript: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and all(s.ok for s in self.steps)

    @property
    def first_failure(self) -> str:
        if self.error:
            return self.error
        for s in self.steps:
            if not s.ok:
                return f"step {s.line} ({s.kind}): {s.detail}"
        return ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class Session:
    """One scenario run: owns the agent process, the mock and the simulated clock."""

    def __init__(
        self,
        scenario: Scenario,
        agent_cmd: str,
        extra_env: dict,
        settle_ms: int,
        webhook_path: str = "/webhook",
    ):
        self.sc = scenario
        self.agent_cmd = agent_cmd
        self.extra_env = extra_env
        self.webhook_path = "/" + webhook_path.lstrip("/")
        self.transcript: list[dict] = []
        self.settle = settle_ms / 1000
        self.clock = SimClock()
        self.customer = envelope.Customer(**scenario.customer)
        self.sent_wamids: dict[str, dict] = {}
        self._msg_counter = 0
        self.cursor_out = 0
        self.cursor_trace = 0
        self.pending_out: list[dict] = []
        self.pending_trace: list[dict] = []
        self.last_http: int | None = None

    # -- lifecycle -------------------------------------------------------------------------
    def start(self, mock: GraphMock) -> None:
        self.mock = mock
        self.port = _free_port()
        values = {
            "port": str(self.port),
            "graph_api_base": mock.base_url,
            "access_token": ACCESS_TOKEN,
            "app_secret": APP_SECRET,
            "verify_token": VERIFY_TOKEN,
            "phone_number_id": envelope.PHONE_NUMBER_ID,
            "trace_url": f"{mock.base_url}/__turncheck/trace",
        }
        # Agents name these settings differently; --env META_SECRET={app_secret} maps them.
        custom = {**{k: str(v) for k, v in self.sc.env.items()}, **self.extra_env}
        env = {
            **os.environ,
            "PORT": values["port"],
            "GRAPH_API_BASE": values["graph_api_base"],
            "WHATSAPP_TOKEN": ACCESS_TOKEN,
            "APP_SECRET": APP_SECRET,
            "VERIFY_TOKEN": VERIFY_TOKEN,
            "PHONE_NUMBER_ID": envelope.PHONE_NUMBER_ID,
            "TURNCHECK_TRACE_URL": values["trace_url"],
            **{k: _fill(v, values) for k, v in custom.items()},
        }
        args = shlex.split(self.agent_cmd)
        if args and args[0] in {"python", "python3"}:
            args[0] = sys.executable
        self.proc = subprocess.Popen(
            args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode()[-500:] if self.proc.stderr else ""
                raise RuntimeError(f"agent exited during startup: {err}")
            try:
                code, body = _http(
                    "GET",
                    f"{self.base}{self.webhook_path}?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}"
                    "&hub.challenge=4242",
                )
                if code == 200 and body.strip() == "4242":
                    return
                raise RuntimeError(f"verification handshake failed: HTTP {code} {body[:80]!r}")
            except urllib.error.URLError:
                time.sleep(0.05)
            except ConnectionError:
                time.sleep(0.05)
        raise RuntimeError("agent did not start listening within 10s")

    def stop(self) -> None:
        if getattr(self, "proc", None) and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # -- stimuli ---------------------------------------------------------------------------
    def _next_wamid(self) -> str:
        self._msg_counter += 1
        return f"wamid.TC{self._msg_counter:06d}"

    def _post_webhook(self, payload: dict, signature: str | None = None) -> None:
        body = envelope.encode(payload)
        sig = signature if signature is not None else envelope.sign(body, APP_SECRET)
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sig}
        self.last_http, _ = _http("POST", f"{self.base}{self.webhook_path}", body, headers)
        self._settle()

    def _settle(self) -> None:
        """Wait until the agent has gone quiet, then collect what it sent."""
        last = self.mock.recorder.snapshot()
        quiet_since = time.time()
        deadline = time.time() + 5
        while time.time() < deadline:
            time.sleep(0.02)
            now = self.mock.recorder.snapshot()
            if now != last:
                last, quiet_since = now, time.time()
            elif time.time() - quiet_since >= self.settle:
                break
        with self.mock.recorder.lock:
            self.pending_out = self.mock.recorder.outbound[self.cursor_out :]
            self.pending_trace = self.mock.recorder.traces[self.cursor_trace :]
            self.cursor_out = len(self.mock.recorder.outbound)
            self.cursor_trace = len(self.mock.recorder.traces)
        for m in self.pending_out:
            self.transcript.append(
                {"role": "agent", "text": describe_message(m["body"]), "at": self.clock.now}
            )
        for t in self.pending_trace:
            self.transcript.append({"role": "tool", "text": json.dumps(t), "at": self.clock.now})

    def stim(self, kind: str, arg, ref: str | None) -> None:
        if kind == "advance":
            self.clock.advance(arg)
            return
        if kind in {"send", "send_tampered", "press"}:
            ts = self.clock.step()
            wamid = self._next_wamid()
            if kind == "press":
                payload = envelope.button_reply(
                    self.customer, wamid, arg["id"], arg.get("title", arg["id"]), ts
                )
            else:
                payload = envelope.text_message(self.customer, wamid, str(arg), ts)
            if ref:
                self.sent_wamids[ref] = payload
            said = arg.get("title", arg["id"]) if kind == "press" else str(arg)
            self.transcript.append({"role": "customer", "text": said, "at": ts})
            sig = "sha256=" + "0" * 64 if kind == "send_tampered" else None
            self._post_webhook(payload, signature=sig)
            return
        if kind == "redeliver":
            self._post_webhook(self.sent_wamids[arg])  # identical bytes, identical wamid
            return
        if kind == "status":
            wamid = arg.get("wamid") or self._last_reply_wamid()
            ts = self.clock.step()
            self._post_webhook(envelope.status_update(self.customer, wamid, arg["state"], ts))
            return
        if kind == "event":
            body = dict(arg.get("json") or {})
            body.setdefault("wa_id", self.customer.wa_id)
            body["timestamp"] = self.clock.now
            data = json.dumps(body).encode()
            self.last_http, _ = _http(
                "POST", f"{self.base}{arg['path']}", data, {"Content-Type": "application/json"}
            )
            self._settle()
            return
        raise ValueError(kind)

    def _last_reply_wamid(self) -> str:
        with self.mock.recorder.lock:
            n = len(self.mock.recorder.outbound)
        return f"wamid.OUT{n:06d}" if n else "wamid.OUT000000"

    # -- expectations ----------------------------------------------------------------------
    def expect(self, kind: str, arg) -> str | None:
        if kind == "expect_reply":
            if not self.pending_out:
                return "expected a reply, agent sent nothing"
            msg = self.pending_out.pop(0)
            if not msg["auth"]:
                return "agent called the Graph API without the access token"
            return check_reply(arg, msg["body"], self.customer.wa_id)
        if kind == "expect_no_reply":
            if self.pending_out:
                extra = "; ".join(describe_message(m["body"]) for m in self.pending_out)
                self.pending_out = []
                return f"expected no reply, agent sent: {extra}"
            return None
        if kind == "expect_tool":
            calls = [t for t in self.pending_trace if t.get("type") == "tool_call"]
            if not calls:
                return f"expected tool call {arg['name']!r}, agent made none"
            call = calls[0]
            self.pending_trace.remove(call)
            return check_tool(arg, call)
        if kind == "expect_http":
            want = arg if isinstance(arg, int) else arg.get("status")
            if isinstance(want, int) and self.last_http != want:
                return f"expected HTTP {want}, got {self.last_http}"
            if (
                isinstance(want, str)
                and want == "4xx"
                and not (self.last_http and 400 <= self.last_http < 500)
            ):
                return f"expected a 4xx rejection, got HTTP {self.last_http}"
            return None
        raise ValueError(kind)


def _fill(value: str, values: dict[str, str]) -> str:
    try:
        return value.format_map(values)
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"--env value {value!r}: unknown placeholder {e}") from e


def _unasserted(session: Session, line: int, before: str) -> StepResult | None:
    """Replies nobody asserted on are a failure: a second, unexpected message is a bug."""
    if session.sc.allow_extra_replies or not session.pending_out:
        return None
    extra = "; ".join(describe_message(m["body"]) for m in session.pending_out)
    session.pending_out = []
    return StepResult(
        line, "unasserted", False, f"reply not covered by any expect {before}: {extra}"
    )


def run_scenario(
    sc: Scenario,
    agent_cmd: str,
    extra_env: dict | None = None,
    settle_ms: int = 150,
    webhook_path: str = "/webhook",
) -> ScenarioResult:
    result = ScenarioResult(scenario=sc)
    t0 = time.time()
    with GraphMock(ACCESS_TOKEN) as mock:
        session = Session(sc, agent_cmd, extra_env or {}, settle_ms, webhook_path)
        try:
            session.start(mock)
            for step in sc.steps:
                if step.kind.startswith("expect"):
                    err = session.expect(step.kind, step.arg)
                    result.steps.append(StepResult(step.line, step.kind, err is None, err or ""))
                else:
                    leftover = _unasserted(session, step.line, f"(before step {step.line})")
                    if leftover:
                        result.steps.append(leftover)
                    session.stim(step.kind, step.arg, step.ref)
                    result.steps.append(StepResult(step.line, step.kind, True))
            leftover = _unasserted(session, len(sc.steps), "(at end of scenario)")
            if leftover:
                result.steps.append(leftover)
        except Exception as e:  # an agent crash is a test failure, not a turncheck crash
            result.error = f"{type(e).__name__}: {e}"
        finally:
            session.stop()
            result.transcript = session.transcript
    result.seconds = time.time() - t0
    return result
