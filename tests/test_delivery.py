"""Meta's delivery behaviour: errors from the Graph API, the 24-hour window, batched
webhooks, slow acknowledgements and agents that reply seconds later."""

import json
import textwrap
from pathlib import Path

import pytest

from turncheck import envelope
from turncheck.runner import run_scenario
from turncheck.scenario import ScenarioError, load

ROOT = Path(__file__).resolve().parent.parent
SHOPAGENT = f"python {ROOT / 'examples/agents/shopagent.py'}"
SCENARIOS = ROOT / "examples/scenarios"


def scenario(tmp_path, body: str):
    p = tmp_path / "s.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return load(p)


def failure(result) -> str:
    return result.error or result.first_failure


def test_rate_limited_send_must_be_retried():
    sc = load(SCENARIOS / "07-rate-limit.yaml")
    assert run_scenario(sc, SHOPAGENT).ok
    broken = run_scenario(sc, SHOPAGENT, {"SHOPAGENT_BUGS": "noretry"})
    assert not broken.ok
    assert "130429 Rate limit hit" in failure(broken)


def test_free_form_text_outside_the_window_is_rejected_like_whatsapp_does(tmp_path):
    body = """
        customer: {wa_id: "919800000004", name: Kabir}
        window_errors: %s
        steps:
          - send: "order KC1043 status"
          - expect_tool: {name: lookup_order}
          - expect_reply: {contains: "packed"}
          - advance: 25h
          - event: {path: /events/order-shipped, json: {order_id: KC1043}}
          - expect_reply: {type: template, template: order_update}
    """
    for mode in ("webhook", "response"):
        sc = scenario(tmp_path, body % mode)
        assert run_scenario(sc, SHOPAGENT).ok, mode
        broken = run_scenario(sc, SHOPAGENT, {"SHOPAGENT_BUGS": "window"})
        assert "rejected with 131047 Re-engagement message" in failure(broken), mode
        assert any("not delivered: 131047" in t["text"] for t in broken.transcript), mode
    # with enforcement off, only the template assertion notices
    off = run_scenario(scenario(tmp_path, body % "off"), SHOPAGENT, {"SHOPAGENT_BUGS": "window"})
    assert "expected a template" in failure(off)


def test_every_message_in_a_batch_is_handled():
    sc = load(SCENARIOS / "08-batched-webhook.yaml")
    assert run_scenario(sc, SHOPAGENT).ok
    assert not run_scenario(sc, SHOPAGENT, {"SHOPAGENT_BUGS": "batch"}).ok


def test_slow_ack_fails_the_budget_and_is_redelivered():
    sc = load(SCENARIOS / "09-fast-ack.yaml")
    assert run_scenario(sc, SHOPAGENT).ok
    slow = run_scenario(sc, SHOPAGENT, {"SHOPAGENT_BUGS": "slowack"})
    assert "acknowledged within 500 ms" in failure(slow)
    assert any(t["role"] == "system" and "redelivered" in t["text"] for t in slow.transcript)


LATE = textwrap.dedent("""
    import json, os, threading, time, urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from socketserver import TCPServer
    from urllib.parse import parse_qs, urlparse

    def reply(to):
        time.sleep(float(os.environ["DELAY"]))  # an LLM taking its time
        body = json.dumps({"to": to, "type": "text", "text": {"body": "done"}}).encode()
        req = urllib.request.Request(os.environ["GRAPH_API_BASE"] + "/v21.0/1/messages",
            data=body, headers={"Authorization": "Bearer " + os.environ["WHATSAPP_TOKEN"]})
        urllib.request.urlopen(req).read()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(parse_qs(urlparse(self.path).query)["hub.challenge"][0].encode())
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200); self.end_headers()
            for m in json.loads(raw)["entry"][0]["changes"][0]["value"].get("messages", []):
                threading.Thread(target=reply, args=(m["from"],)).start()

    class Server(ThreadingHTTPServer):
        def server_bind(self):  # no reverse-DNS lookup; see LocalServer in graph_mock.py
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    Server(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
""")


def test_expectations_wait_for_an_agent_that_replies_seconds_later(tmp_path):
    """Regression: a reply that arrived after the 150 ms settle window used to count as
    'agent sent nothing', which failed every agent that acks first and thinks later."""
    agent = tmp_path / "late.py"
    agent.write_text(LATE, encoding="utf-8")
    sc = scenario(tmp_path, 'steps:\n  - send: "hi"\n  - expect_reply: {contains: "done"}\n')
    assert run_scenario(sc, f"python {agent}", {"DELAY": "1.2"}).ok
    short = run_scenario(sc, f"python {agent}", {"DELAY": "1.2"}, reply_timeout_ms=300)
    assert "agent sent nothing" in failure(short)


def test_envelope_batch_and_failed_receipt_shapes():
    c = envelope.Customer("919800000001", "Asha")
    a = envelope.text_message(c, "wamid.A", "one", 1)
    b = envelope.text_message(c, "wamid.B", "two", 2)
    entries = envelope.batch([a, b])["entry"]
    assert [e["changes"][0]["value"]["messages"][0]["id"] for e in entries] == [
        "wamid.A",
        "wamid.B",
    ]
    assert a["entry"][0]["changes"][0]["value"]["messages"][0]["id"] == "wamid.A"  # untouched
    err = {"code": 131047, "title": "Re-engagement message", "details": "x"}
    st = envelope.status_update(c, "wamid.OUT1", "failed", 3, err)
    receipt = st["entry"][0]["changes"][0]["value"]["statuses"][0]
    assert receipt["errors"][0]["code"] == 131047
    assert json.loads(envelope.encode(st)) == st


@pytest.mark.parametrize(
    "body, message",
    [
        ("steps:\n  - graph_error: {times: 2}\n", "integer 'code'"),
        ("steps:\n  - graph_error: {code: 130429, via: email}\n", "via must be"),
        ("steps:\n  - graph_error: {code: 130429, http: 200}\n", "4xx or 5xx"),
        ("steps:\n  - graph_error: {code: 1, retry: true}\n", "unknown key"),
        ("steps:\n  - send_batch: [only one]\n", "two or more"),
        ("window_errors: maybe\nsteps:\n  - send: hi\n", "window_errors"),
        ("webhook_timeout_ms: -5\nsteps:\n  - send: hi\n", "webhook_timeout_ms"),
        ("webhook_retries: yes\nsteps:\n  - send: hi\n", "webhook_retries"),
        ("steps: [send: hi\n", "not valid YAML"),
    ],
)
def test_lint_rejects_bad_delivery_options(tmp_path, body, message):
    with pytest.raises(ScenarioError, match=message):
        scenario(tmp_path, body)


def test_agent_command_keeps_windows_paths_intact():
    """Regression: POSIX shlex turned C:\\Users\\bot.py into C:Usersbot.py."""
    from turncheck.runner import split_command

    cmd = r'python "C:\Program Files\bot\app.py" --port 8080'
    assert split_command(cmd, windows=True) == [
        "python",
        r"C:\Program Files\bot\app.py",
        "--port",
        "8080",
    ]
    assert split_command("python 'my bot.py' -v", windows=False) == ["python", "my bot.py", "-v"]


def test_expect_http_accepts_a_status_class(tmp_path):
    """Regression: the README's `expect_http: 4xx` crashed the runner."""
    sc = scenario(
        tmp_path,
        """
        steps:
          - send_tampered: "hi"
          - expect_http: 4xx
          - expect_no_reply: {}
        """,
    )
    assert run_scenario(sc, SHOPAGENT).ok
