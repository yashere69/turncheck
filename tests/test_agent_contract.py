"""Behaviour that depends on how a real agent is wired: custom paths, its own env names,
double replies, and transcript output."""

import json
import textwrap

from turncheck.report import transcripts
from turncheck.runner import run_scenario
from turncheck.scenario import load

# A minimal agent that does NOT use turncheck's env names or the /webhook path, the way a
# client's existing bot wouldn't.
AGENT = textwrap.dedent("""
    import hashlib, hmac, json, os, threading, urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    API, SECRET = os.environ["WA_API_URL"], os.environ["META_SECRET"].encode()
    TOKEN, REPLIES = os.environ["WA_TOKEN"], int(os.environ.get("REPLIES", "1"))

    def send(to, text):
        body = json.dumps({"messaging_product": "whatsapp", "to": to, "type": "text",
                           "text": {"body": text}}).encode()
        req = urllib.request.Request(API + "/v21.0/1/messages", data=body,
                                     headers={"Authorization": "Bearer " + TOKEN})
        urllib.request.urlopen(req).read()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            ok = urlparse(self.path).path == "/hooks/wa"
            self.send_response(200 if ok else 404); self.end_headers()
            if ok: self.wfile.write(q["hub.challenge"][0].encode())
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            sig = "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, self.headers.get("X-Hub-Signature-256", "")):
                self.send_response(401); self.end_headers(); return
            self.send_response(200); self.end_headers()
            for m in json.loads(raw)["entry"][0]["changes"][0]["value"].get("messages", []):
                for i in range(REPLIES):
                    threading.Thread(target=send, args=(m["from"], f"pong {i}")).start()

    ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
""")

ENV = {
    "WA_API_URL": "{graph_api_base}",
    "META_SECRET": "{app_secret}",
    "WA_TOKEN": "{access_token}",
}


def scenario(tmp_path, body):
    p = tmp_path / "s.yaml"
    p.write_text(body, encoding="utf-8")
    return load(p)


def agent(tmp_path):
    a = tmp_path / "agent.py"
    a.write_text(AGENT, encoding="utf-8")
    return f"python {a}"


def test_custom_path_and_env_names(tmp_path):
    sc = scenario(tmp_path, "steps:\n  - send: ping\n  - expect_reply: {contains: pong}\n")
    r = run_scenario(sc, agent(tmp_path), ENV, webhook_path="hooks/wa")
    assert r.ok, r.first_failure


def test_unasserted_second_reply_fails(tmp_path):
    sc = scenario(tmp_path, "steps:\n  - send: ping\n  - expect_reply: {contains: pong}\n")
    r = run_scenario(sc, agent(tmp_path), {**ENV, "REPLIES": "2"}, webhook_path="/hooks/wa")
    assert not r.ok
    assert "reply not covered by any expect" in r.first_failure


def test_extra_replies_can_be_allowed(tmp_path):
    sc = scenario(
        tmp_path,
        "allow_extra_replies: true\nsteps:\n  - send: ping\n  - expect_reply: {contains: pong}\n",
    )
    r = run_scenario(sc, agent(tmp_path), {**ENV, "REPLIES": "2"}, webhook_path="/hooks/wa")
    assert r.ok, r.first_failure


def test_bad_placeholder_is_reported(tmp_path):
    sc = scenario(tmp_path, "steps:\n  - send: ping\n")
    r = run_scenario(sc, agent(tmp_path), {"X": "{nope}"})
    assert "unknown placeholder" in r.first_failure


def test_transcripts_are_chatlens_traces(tmp_path):
    sc = scenario(tmp_path, "steps:\n  - send: ping\n  - expect_reply: {contains: pong}\n")
    r = run_scenario(sc, agent(tmp_path), ENV, webhook_path="/hooks/wa")
    row = json.loads(transcripts([r]))
    assert row["id"] == "s" and row["meta"]["passed"] is True
    assert [t["role"] for t in row["turns"]] == ["customer", "agent"]
    assert row["turns"][1]["text"] == "text: 'pong 0'"
