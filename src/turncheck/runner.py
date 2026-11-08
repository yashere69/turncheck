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
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import envelope
from .assertions import check_reply, check_tool, describe_message, transcript_text
from .clock import SimClock
from .graph_mock import GraphMock, error_for
from .scenario import Scenario

APP_SECRET = "turncheck-test-app-secret"
ACCESS_TOKEN = "turncheck-test-token"
VERIFY_TOKEN = "turncheck-verify"
WINDOW = 24 * 3600  # the customer-service window, in seconds


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
    agent_log: str = ""  # the tail of the agent's stderr, shown when a scenario fails

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


def split_command(cmd: str, windows: bool = os.name == "nt") -> list[str]:
    """Split --agent into argv. POSIX-mode shlex treats backslashes as escapes, which
    mangles Windows paths, so on Windows split in non-POSIX mode and strip the quotes."""
    if not windows:
        return shlex.split(cmd)
    parts = shlex.split(cmd, posix=False)
    return [x[1:-1] if len(x) >= 2 and x[0] == x[-1] and x[0] in "\"'" else x for x in parts]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 5.0,
) -> tuple[int | None, str, float]:
    """Returns (status, body, elapsed ms). Status is None when the agent did not answer in time."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(), (time.monotonic() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), (time.monotonic() - t0) * 1000
    except TimeoutError:
        return None, "", (time.monotonic() - t0) * 1000
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            return None, "", (time.monotonic() - t0) * 1000
        raise


class Session:
    """One scenario run: owns the agent process, the mock and the simulated clock."""

    def __init__(
        self,
        scenario: Scenario,
        agent_cmd: str,
        extra_env: dict,
        settle_ms: int,
        webhook_path: str = "/webhook",
        reply_timeout_ms: int = 5000,
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
        self.last_ack_ms = 0.0
        self.reply_timeout = reply_timeout_ms / 1000
        self.last_inbound: int | None = None  # simulated time of the customer's last message
        self.rejected: list[dict] = []  # sends WhatsApp refused since the last stimulus

    # -- lifecycle -------------------------------------------------------------------------
    def start(self, mock: GraphMock) -> None:
        self.mock = mock
        mock.recorder.policy = self._window_policy
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
        args = split_command(self.agent_cmd)
        # a file, not a pipe: nobody reads a pipe while the agent runs, so a chatty agent
        # would fill it and block mid-request
        self.log = tempfile.TemporaryFile()
        if args and args[0] in {"python", "python3"}:
            args[0] = sys.executable
        try:
            self.proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=self.log)
        except (FileNotFoundError, PermissionError) as e:
            what = "not found" if isinstance(e, FileNotFoundError) else "not executable"
            raise RuntimeError(f"can't start the agent: {args[0]!r} {what} (check --agent)") from e
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = self.log_tail(500)
                raise RuntimeError(f"agent exited during startup: {err}")
            try:
                code, body, _ = _http(
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

    def log_tail(self, limit: int = 2000) -> str:
        log = getattr(self, "log", None)
        if log is None or log.closed:
            return ""
        log.flush()
        size = log.seek(0, os.SEEK_END)
        log.seek(max(0, size - limit))
        return log.read().decode("utf-8", "replace")

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

    def _window_policy(self, body: dict) -> tuple[dict, str] | None:
        """Free-form messages need a customer message in the last 24 hours (error 131047)."""
        if self.sc.window_errors == "off" or body.get("type") == "template":
            return None
        if self.last_inbound is not None and self.clock.now - self.last_inbound <= WINDOW:
            return None
        return error_for(131047), self.sc.window_errors

    def _post_webhook(
        self, payload: dict, signature: str | None = None, retry: bool = True
    ) -> None:
        body = envelope.encode(payload)
        sig = signature if signature is not None else envelope.sign(body, APP_SECRET)
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sig}
        url = f"{self.base}{self.webhook_path}"
        timeout = self.sc.webhook_timeout_ms / 1000
        self.last_http, _, self.last_ack_ms = _http("POST", url, body, headers, timeout)
        code, attempts = self.last_http, 0
        # Meta delivers at least once: a webhook that is not acknowledged with a 200 in time
        # is sent again, byte for byte. Agents that process before acking reply twice.
        while (
            retry and (code is None or not 200 <= code < 300) and attempts < self.sc.webhook_retries
        ):
            attempts += 1
            why = "no answer" if code is None else f"HTTP {code}"
            self.transcript.append(
                {
                    "role": "system",
                    "text": f"turncheck: webhook got {why} within {self.sc.webhook_timeout_ms} ms;"
                    " redelivered, as Meta does",
                    "at": self.clock.now,
                }
            )
            code, _, _ = _http("POST", url, body, headers, timeout)
        self._settle()

    def _settle(self) -> None:
        """Wait until the agent has gone quiet, then collect what it sent."""
        for _ in range(4):  # a failed receipt can make the agent send again (e.g. a template)
            self._wait_quiet()
            if not self._collect():
                break

    def _wait_quiet(self) -> None:
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

    def _collect(self) -> bool:
        """Move new sends and tool calls into the pending lists. Returns True if WhatsApp
        rejected a send through a failed receipt, which the agent may react to."""
        with self.mock.recorder.lock:
            new_out = self.mock.recorder.outbound[self.cursor_out :]
            new_trace = self.mock.recorder.traces[self.cursor_trace :]
            self.cursor_out = len(self.mock.recorder.outbound)
            self.cursor_trace = len(self.mock.recorder.traces)
        self.pending_trace.extend(new_trace)
        # tool calls come before the reply they produced
        for t in new_trace:
            self.transcript.append({"role": "tool", "text": json.dumps(t), "at": self.clock.now})
        receipts = []
        for m in new_out:
            said = transcript_text(m["body"])
            if m["rejected"]:
                self.rejected.append(m)
                err = m["rejected"]
                said = f"(not delivered: {err['code']} {err['title']}) {said}"
                if m["via"] == "webhook":
                    receipts.append(m)
            else:
                self.pending_out.append(m)
            self.transcript.append({"role": "agent", "text": said, "at": self.clock.now})
        for m in receipts:
            failed = envelope.status_update(
                self.customer, m["wamid"], "failed", self.clock.step(), m["rejected"]
            )
            body = envelope.encode(failed)
            headers = {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": envelope.sign(body, APP_SECRET),
            }
            _http("POST", f"{self.base}{self.webhook_path}", body, headers)
        return bool(receipts)

    def _await(self, ready) -> None:
        """Real agents reply asynchronously (an LLM call can take seconds), so expectations
        wait for the reply instead of failing the moment the webhook is acknowledged."""
        deadline = time.time() + self.reply_timeout
        while not ready() and time.time() < deadline:
            self._settle()

    def stim(self, kind: str, arg, ref: str | None) -> None:
        if kind not in {"advance", "graph_error"}:
            self.rejected = []
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
            if kind == "send_tampered":
                # a forgery is not from Meta: nothing retries it, and it opens no window
                self._post_webhook(payload, signature="sha256=" + "0" * 64, retry=False)
            else:
                self.last_inbound = ts
                self._post_webhook(payload)
            return
        if kind == "send_batch":
            parts = []
            for text in arg:
                ts = self.clock.step()
                parts.append(envelope.text_message(self.customer, self._next_wamid(), text, ts))
                self.transcript.append({"role": "customer", "text": text, "at": ts})
            payload = envelope.batch(parts)
            if ref:
                self.sent_wamids[ref] = payload
            self.last_inbound = ts
            self._post_webhook(payload)
            return
        if kind == "graph_error":
            err = error_for(arg["code"], arg.get("message"))
            self.mock.arm(err, times=arg["times"], via=arg["via"], http=arg["http"])
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
            if self.customer.share_phone:
                body.setdefault("wa_id", self.customer.wa_id)
            body.setdefault("user_id", self.customer.user_id)
            body["timestamp"] = self.clock.now
            data = json.dumps(body).encode()
            self.last_http, _, self.last_ack_ms = _http(
                "POST", f"{self.base}{arg['path']}", data, {"Content-Type": "application/json"}
            )
            self._settle()
            return
        raise ValueError(kind)

    def _last_reply_wamid(self) -> str:
        with self.mock.recorder.lock:
            ids = [m["wamid"] for m in self.mock.recorder.outbound if m["wamid"]]
        return ids[-1] if ids else "wamid.OUT000000"

    def _rejections(self) -> str:
        if not self.rejected:
            return ""
        parts = []
        for m in self.rejected:
            err = m["rejected"]
            parts.append(
                f"{describe_message(m['body'])} was rejected with {err['code']} {err['title']}"
            )
        return "; WhatsApp refused what it did send: " + "; ".join(parts)

    # -- expectations ----------------------------------------------------------------------
    def expect(self, kind: str, arg) -> str | None:
        if kind == "expect_reply":
            self._await(lambda: bool(self.pending_out))
            if not self.pending_out:
                return "expected a reply, agent sent nothing" + self._rejections()
            msg = self.pending_out.pop(0)
            if not msg["auth"]:
                return "agent called the Graph API without the access token"
            return check_reply(arg, msg["body"], self.customer)
        if kind == "expect_no_reply":
            if self.pending_out:
                extra = "; ".join(describe_message(m["body"]) for m in self.pending_out)
                self.pending_out = []
                return f"expected no reply, agent sent: {extra}"
            return None
        if kind == "expect_tool":
            self._await(lambda: any(t.get("type") == "tool_call" for t in self.pending_trace))
            calls = [t for t in self.pending_trace if t.get("type") == "tool_call"]
            if not calls:
                return f"expected tool call {arg['name']!r}, agent made none"
            call = calls[0]
            self.pending_trace.remove(call)
            return check_tool(arg, call)
        if kind == "expect_http":
            want, within = arg["status"], arg["within_ms"]
            got = self.last_http
            if within is not None and (got is None or self.last_ack_ms > within):
                took = "no answer" if got is None else f"{self.last_ack_ms:.0f} ms"
                return (
                    f"expected the webhook to be acknowledged within {within} ms, got {took}"
                    " (Meta redelivers webhooks it considers unacknowledged)"
                )
            if isinstance(want, int) and got != want:
                return f"expected HTTP {want}, got {got}"
            if isinstance(want, str):
                low = int(want[0]) * 100
                if got is None or not low <= got < low + 100:
                    return f"expected an HTTP {want} answer, got {got}"
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
    reply_timeout_ms: int = 5000,
) -> ScenarioResult:
    result = ScenarioResult(scenario=sc)
    t0 = time.time()
    with GraphMock(ACCESS_TOKEN) as mock:
        session = Session(sc, agent_cmd, extra_env or {}, settle_ms, webhook_path, reply_timeout_ms)
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
            result.agent_log = session.log_tail()
            if getattr(session, "log", None):
                session.log.close()
    result.seconds = time.time() - t0
    return result
