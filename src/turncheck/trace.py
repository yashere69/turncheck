"""Agent-side helper: report a tool call to turncheck.

Opt-in instrumentation. When TURNCHECK_TRACE_URL is unset (production), this is a no-op,
so it is safe to leave in shipped code.
"""

from __future__ import annotations

import json
import os
import urllib.request


def tool_call(name: str, args: dict) -> None:
    url = os.environ.get("TURNCHECK_TRACE_URL")
    if not url:
        return
    data = json.dumps({"type": "tool_call", "name": name, "args": args}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError:
        pass  # tracing must never break the agent
