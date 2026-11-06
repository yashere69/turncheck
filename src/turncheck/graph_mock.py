"""A stand-in for graph.facebook.com that records what the agent sends.

The agent under test is pointed at this server via GRAPH_API_BASE. It also exposes
/__turncheck/trace so instrumented agents can report tool calls.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_MESSAGES_PATH = re.compile(r"^/v\d+\.\d+/(?P<pnid>\d+)/messages$")


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.outbound: list[dict] = []  # each: {"body": {...}, "auth": bool, "seq": int}
        self.traces: list[dict] = []
        self._seq = 0

    def add_outbound(self, body: dict, auth_ok: bool) -> str:
        with self.lock:
            self._seq += 1
            self.outbound.append({"body": body, "auth": auth_ok, "seq": self._seq})
            return f"wamid.OUT{self._seq:06d}"

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
                wamid = recorder.add_outbound(body, auth_ok)
                return self._json(
                    200,
                    {
                        "messaging_product": "whatsapp",
                        "contacts": [{"input": body.get("to"), "wa_id": body.get("to")}],
                        "messages": [{"id": wamid}],
                    },
                )
            return self._json(404, {"error": {"message": f"unknown path {self.path}"}})

    return Handler


class GraphMock:
    def __init__(self, token: str):
        self.recorder = Recorder()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.recorder, token))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> GraphMock:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
