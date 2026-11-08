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


# Dialling prefix -> ISO country, for the prefix of a derived business-scoped user ID.
_COUNTRY = {"91": "IN", "971": "AE", "44": "GB", "65": "SG", "61": "AU", "49": "DE", "1": "US"}


@dataclass
class Customer:
    """The person messaging the business.

    Since April 2026 Meta adds a business-scoped user ID (BSUID) to every webhook, and a
    customer who has set a WhatsApp username may arrive without a phone number at all
    (`share_phone: false`): no `wa_id`, no `from`, only `user_id` / `from_user_id`.
    """

    wa_id: str = "919800000001"
    name: str = "Test Customer"
    username: str | None = None
    user_id: str | None = None
    share_phone: bool = True

    def __post_init__(self) -> None:
        if not self.user_id:
            country = next(
                (
                    c
                    for prefix, c in sorted(_COUNTRY.items(), key=lambda kv: -len(kv[0]))
                    if self.wa_id.startswith(prefix)
                ),
                "IN",
            )
            digits = int(hashlib.sha256(self.wa_id.encode()).hexdigest()[:16], 16)
            self.user_id = f"{country}.{digits:020d}"

    def contact(self) -> dict:
        profile: dict = {"name": self.name}
        if self.username:
            profile["username"] = self.username
        out: dict = {"profile": profile}
        if self.share_phone:
            out["wa_id"] = self.wa_id
        out["user_id"] = self.user_id
        return out

    def sender(self) -> dict:
        out = {"from": self.wa_id} if self.share_phone else {}
        return {**out, "from_user_id": self.user_id}

    def addressed_by(self, body: dict) -> bool:
        """Did an outbound message go to this customer, by phone number or by BSUID?"""
        if body.get("to"):
            return self.share_phone and body["to"] == self.wa_id
        return body.get("recipient") == self.user_id

    @property
    def label(self) -> str:
        return self.wa_id if self.share_phone else str(self.user_id)


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
            "contacts": [customer.contact()],
            "messages": [
                {
                    **customer.sender(),
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
            "contacts": [customer.contact()],
            "messages": [
                {
                    **customer.sender(),
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


def status_update(
    customer: Customer, wamid: str, status: str, ts: int, error: dict | None = None
) -> dict:
    """A delivery receipt. Sent, delivered and read receipts carry the customer's contact
    and BSUID; a failed receipt carries the error instead, as Meta's do."""
    if status not in {"sent", "delivered", "read", "failed"}:
        raise ValueError(f"unknown status {status!r}")
    receipt: dict = {"id": wamid, "status": status, "timestamp": str(ts)}
    if customer.share_phone:
        receipt["recipient_id"] = customer.wa_id
    value: dict = {}
    if status != "failed":
        receipt["recipient_user_id"] = customer.user_id
        value["contacts"] = [customer.contact()]
    if error:
        receipt["errors"] = [
            {
                "code": error["code"],
                "title": error["title"],
                "message": error["title"],
                "error_data": {"details": error["details"]},
            }
        ]
    value["statuses"] = [receipt]
    return _wrap(value)


def batch(payloads: list[dict]) -> dict:
    """Several notifications in one POST, one `entry` each. Meta's webhook docs say
    notifications "are aggregated and sent in a batch with a maximum of 1000 updates", so an
    agent that only reads entry[0] silently loses the rest."""
    return {
        "object": "whatsapp_business_account",
        "entry": [json.loads(json.dumps(p["entry"][0])) for p in payloads],
    }


def encode(payload: dict) -> bytes:
    """Serialise once. The signature is computed over these exact bytes."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(body: bytes, app_secret: str) -> str:
    """Value for the X-Hub-Signature-256 header."""
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
