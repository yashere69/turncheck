"""A stand-in for graph.facebook.com that records what the agent sends.

The agent under test is pointed at this server via GRAPH_API_BASE. It also exposes
/__turncheck/trace so instrumented agents can report tool calls.

It can also fail the way the real API fails: a scenario can arm the next few sends to be
rejected with a Cloud API error code, and the runner installs a policy that rejects
free-form messages sent outside the 24-hour window.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer

_MESSAGES_PATH = re.compile(r"^/v\d+\.\d+/(?P<pnid>\d+)/messages$")

# Codes from Meta's Cloud API error-code reference. The details are paraphrased from it.
# The reference does not list HTTP statuses, so scenarios can set `http:` themselves.
KNOWN_ERRORS: dict[int, tuple[str, str]] = {
    1: ("API Unknown", "Invalid request or possible server error."),
    2: ("API Service", "Temporary problem caused by downtime or overload."),
    4: ("API Too Many Calls", "The app has reached its API call rate limit."),
    80007: ("Rate limit issues", "The WhatsApp Business Account has reached its rate limit."),
    130429: ("Rate limit hit", "Cloud API message throughput has been reached."),
    131000: ("Something went wrong", "Message failed to send because of an unknown error."),
    131026: ("Message undeliverable", "Unable to deliver the message to this recipient."),
    131047: (
        "Re-engagement message",
        "More than 24 hours have passed since the recipient last replied to the sender number.",
    ),
    131048: ("Spam rate limit hit", "Sending is restricted because of the number's quality."),
    131049: ("Meta chose not to deliver", "Not delivered to maintain healthy engagement."),
    131056: (
        "Pair rate limit hit",
        "Too many messages from this sender to the same recipient in a short period.",
    ),
    133010: ("Phone number not registered", "The phone number is not registered."),
}


def error_for(code: int, message: str | None = None) -> dict:
    title, details = KNOWN_ERRORS.get(code, ("Error", message or f"error {code}"))
    return {"code": code, "title": message or title, "details": details}


def error_body(err: dict) -> dict:
    """The JSON body the Graph API returns for a failed call."""
    return {
        "error": {
            "message": f"(#{err['code']}) {err['title']}",
            "type": "OAuthException",
            "code": err["code"],
            "error_data": {"messaging_product": "whatsapp", "details": err["details"]},
            "fbtrace_id": "turncheck",
        }
    }


# A policy looks at an outbound body and returns (error, via) to reject it, or None.
Policy = Callable[[dict], "tuple[dict, str] | None"]


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # each: {"body": {...}, "auth": bool, "seq": int, "wamid": str|None,
        #        "rejected": error dict or None, "via": "response"|"webhook"|None}
        self.outbound: list[dict] = []
        self.traces: list[dict] = []
        self.armed: list[dict] = []  # failures queued by `graph_error` steps
        self.policy: Policy | None = None
        self._seq = 0

    def add_outbound(self, body: dict, auth_ok: bool) -> dict:
        with self.lock:
            self._seq += 1
            rec: dict = {"body": body, "auth": auth_ok, "seq": self._seq}
            rejection = None
            if self.armed:
                spec = self.armed[0]
                spec["times"] -= 1
                if spec["times"] <= 0:
                    self.armed.pop(0)
                rejection = (spec["error"], spec["via"], spec["http"])
            elif self.policy:
                hit = self.policy(body)
                if hit:
                    rejection = (hit[0], hit[1], 400)
            if rejection is None or rejection[1] == "webhook":
                rec["wamid"] = f"wamid.OUT{self._seq:06d}"  # accepted, maybe failed later
            else:
                rec["wamid"] = None
            if rejection:
                rec["rejected"], rec["via"], rec["http"] = rejection
            else:
                rec["rejected"], rec["via"], rec["http"] = None, None, 200
            self.outbound.append(rec)
            return rec

    def add_trace(self, event: dict) -> None:
        with self.lock:
            self.traces.append(event)

    def snapshot(self) -> tuple[int, int]:
        with self.lock:
            return len(self.outbound), len(self.traces)


def _handler(recorder: Recorder, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr logging
            pass

        def _json(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802 (http.server API)
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": {"message": "invalid JSON"}})
            if self.path == "/__turncheck/trace":
                recorder.add_trace(body)
                return self._json(200, {"ok": True})
            if _MESSAGES_PATH.match(self.path):
                auth_ok = self.headers.get("Authorization") == f"Bearer {token}"
                rec = recorder.add_outbound(body, auth_ok)
                if rec["rejected"] and rec["via"] == "response":
                    return self._json(rec["http"], error_body(rec["rejected"]))
                return self._json(
                    200,
                    {
                        "messaging_product": "whatsapp",
                        "contacts": [_contact_ack(body)],
                        "messages": [{"id": rec["wamid"]}],
                    },
                )
            return self._json(404, {"error": {"message": f"unknown path {self.path}"}})

    return Handler


def _contact_ack(body: dict) -> dict:
    """The `contacts` element of a send response: echoes how the recipient was addressed."""
    if body.get("to"):
        return {"input": body["to"], "wa_id": body["to"]}
    return {"input": body.get("recipient"), "user_id": body.get("recipient")}


class LocalServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in server_bind().

    http.server resolves the bind address with socket.getfqdn() before it starts
    listening. On GitHub's macOS runners that call takes about 35 seconds for 127.0.0.1,
    and nothing here uses the name it returns.
    """

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


class GraphMock:
    def __init__(self, token: str):
        self.recorder = Recorder()
        self.server = LocalServer(("127.0.0.1", 0), _handler(self.recorder, token))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        port = self.server.server_address[1]
        return f"http://127.0.0.1:{port}"

    def arm(self, error: dict, times: int = 1, via: str = "response", http: int = 400) -> None:
        with self.recorder.lock:
            self.recorder.armed.append({"error": error, "times": times, "via": via, "http": http})

    def __enter__(self) -> GraphMock:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
