"""Build WhatsApp Cloud API webhook envelopes exactly as Meta sends them.

Shapes follow the `whatsapp_business_account` webhook: entry[].changes[].value with either
`messages[]` (inbound) or `statuses[]` (delivery receipts for outbound messages).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

WABA_ID = "100000000000001"
PHONE_NUMBER_ID = "200000000000002"
DISPLAY_NUMBER = "15550000000"


@dataclass
class Customer:
    wa_id: str
    name: str = "Test Customer"


def _wrap(value: dict) -> dict:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": DISPLAY_NUMBER, "phone_number_id": PHONE_NUMBER_ID},
        **value,
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": WABA_ID, "changes": [{"value": value, "field": "messages"}]}],
    }


def text_message(customer: Customer, wamid: str, body: str, ts: int) -> dict:
    return _wrap(
        {
            "contacts": [{"profile": {"name": customer.name}, "wa_id": customer.wa_id}],
            "messages": [
                {
                    "from": customer.wa_id,
                    "id": wamid,
                    "timestamp": str(ts),
                    "type": "text",
                    "text": {"body": body},
                }
            ],
        }
    )


def button_reply(customer: Customer, wamid: str, button_id: str, title: str, ts: int) -> dict:
    return _wrap(
        {
            "contacts": [{"profile": {"name": customer.name}, "wa_id": customer.wa_id}],
            "messages": [
                {
                    "from": customer.wa_id,
                    "id": wamid,
                    "timestamp": str(ts),
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": button_id, "title": title},
                    },
                }
            ],
        }
    )


def status_update(customer: Customer, wamid: str, status: str, ts: int) -> dict:
    if status not in {"sent", "delivered", "read", "failed"}:
        raise ValueError(f"unknown status {status!r}")
    return _wrap(
        {
            "statuses": [
                {
                    "id": wamid,
                    "status": status,
                    "timestamp": str(ts),
                    "recipient_id": customer.wa_id,
                }
            ]
        }
    )


def encode(payload: dict) -> bytes:
    """Serialise once. The signature is computed over these exact bytes."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(body: bytes, app_secret: str) -> str:
    """Value for the X-Hub-Signature-256 header."""
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
