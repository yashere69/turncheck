"""A small WhatsApp shop agent used as turncheck's reference target.

It is deliberately ordinary: a webhook, a deterministic intent router standing in for the
LLM, one tool (lookup_order) and an order-shipped event hook. Set SHOPAGENT_BUGS to a
comma-separated list to switch on seeded defects that real agents ship with:

  dedup      no idempotency on wamid -> a Meta retry produces a second reply
  window     sends free-form text outside the 24-hour customer-service window
  orderid    order-id regex misses "#KC1042" -> wrong tool argument
  signature  skips X-Hub-Signature-256 verification
  status     treats delivery-status webhooks as customer messages

Stdlib only, so the example runs anywhere turncheck does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from turncheck import trace  # noqa: E402  (opt-in instrumentation; no-op in production)

BUGS = {b.strip() for b in os.environ.get("SHOPAGENT_BUGS", "").split(",") if b.strip()}
GRAPH = os.environ["GRAPH_API_BASE"].rstrip("/")
TOKEN = os.environ["WHATSAPP_TOKEN"]
SECRET = os.environ["APP_SECRET"].encode()
VERIFY = os.environ["VERIFY_TOKEN"]
PNID = os.environ["PHONE_NUMBER_ID"]
WINDOW_SECONDS = 24 * 3600

# Synthetic orders. No real customer data.
ORDERS = {
    "KC1042": {"status": "shipped", "courier": "Delhivery", "eta": "2 days"},
    "KC1043": {"status": "packed", "courier": None, "eta": "4 days"},
}

seen_wamids: set[str] = set()
last_inbound: dict[str, int] = {}  # wa_id -> unix ts of the customer's latest message
lock = threading.Lock()


def send(payload: dict) -> None:
    payload = {"messaging_product": "whatsapp", **payload}
    req = urllib.request.Request(
        f"{GRAPH}/v21.0/{PNID}/messages",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=5).read()


def send_text(to: str, body: str) -> None:
    send({"to": to, "type": "text", "text": {"body": body}})


def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> None:
    send(
        {
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": i, "title": t}} for i, t in buttons
                    ]
                },
            },
        }
    )


def send_template(to: str, name: str, params: list[str]) -> None:
    send(
        {
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": "en"},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
                ],
            },
        }
    )


ORDER_RE_BUGGY = re.compile(r"order\s+(KC\d{4})", re.I)  # misses "order #KC1042"
ORDER_RE = re.compile(r"#?\s*\b(KC\d{4})\b", re.I)


def lookup_order(order_id: str | None) -> dict | None:
    trace.tool_call("lookup_order", {"order_id": order_id})
    return ORDERS.get(order_id or "")


def route(to: str, name: str, text: str) -> None:
    """Stand-in for the LLM: a deterministic router so the example needs no API key."""
    t = text.lower()
    if re.search(r"\b(hi|hello|namaste|hey)\b", t):
        send_text(to, f"Namaste {name}! Ask me about an order, sizes, or type 'human'.")
        return
    if "order" in t or "kahan" in t or "where" in t:
        rx = ORDER_RE_BUGGY if "orderid" in BUGS else ORDER_RE
        m = rx.search(text)
        order_id = m.group(1).upper() if m else None
        order = lookup_order(order_id)
        if not order:
            send_text(to, "I couldn't find that order. Please share the ID, e.g. KC1042.")
            return
        eta = f", expected in {order['eta']}" if order["eta"] else ""
        courier = f" with {order['courier']}" if order["courier"] else ""
        send_buttons(
            to,
            f"Order {order_id} is {order['status']}{courier}{eta}.",
            [("track_more", "Another order"), ("human", "Talk to a person")],
        )
        return
    if "size" in t:
        send_text(to, "Kurta sizes: S 36, M 38, L 40, XL 42, XXL 44 (bust, inches).")
        return
    send_text(to, "Sorry, I can help with orders and sizes. Type 'human' for a person.")


def handle_payload(payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c["wa_id"]: c["profile"]["name"] for c in value.get("contacts", [])}
            if value.get("statuses") and "status" in BUGS:
                for st in value["statuses"]:
                    send_text(st["recipient_id"], "Sorry, I didn't understand that.")
            for msg in value.get("messages", []):
                with lock:
                    if "dedup" not in BUGS:
                        if msg["id"] in seen_wamids:
                            continue  # Meta retried; we already handled this one
                        seen_wamids.add(msg["id"])
                    last_inbound[msg["from"]] = int(msg["timestamp"])
                frm = msg["from"]
                if msg["type"] == "text":
                    route(frm, names.get(frm, "there"), msg["text"]["body"])
                elif msg["type"] == "interactive":
                    bid = msg["interactive"]["button_reply"]["id"]
                    if bid == "human":
                        send_text(frm, "Connecting you to a person. We reply within 2 hours.")
                    else:
                        send_text(frm, "Sure, send me the next order ID.")


def order_shipped(body: dict) -> None:
    to, order_id, now = body["wa_id"], body["order_id"], int(body["timestamp"])
    inside = now - last_inbound.get(to, 0) < WINDOW_SECONDS
    if inside or "window" in BUGS:
        send_text(to, f"Good news: order {order_id} has shipped.")
    else:
        send_template(to, "order_update", [order_id, "shipped"])  # pre-approved template


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code: int, text: str = "") -> None:
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        if q.get("hub.mode") == ["subscribe"] and q.get("hub.verify_token") == [VERIFY]:
            return self._reply(200, q["hub.challenge"][0])
        return self._reply(403)

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        path = urlparse(self.path).path
        if path == "/webhook":
            if "signature" not in BUGS:
                expected = "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
                got = self.headers.get("X-Hub-Signature-256", "")
                if not hmac.compare_digest(expected, got):
                    return self._reply(401)
            self._reply(200)  # acknowledge first: slow acks are what make Meta retry
            threading.Thread(target=handle_payload, args=(json.loads(raw),)).start()
            return
        if path == "/events/order-shipped":
            self._reply(202)
            threading.Thread(target=order_shipped, args=(json.loads(raw),)).start()
            return
        return self._reply(404)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
