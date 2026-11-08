import hashlib
import hmac
import json

from turncheck import envelope
from turncheck.clock import SimClock, parse_duration

C = envelope.Customer(wa_id="919800000001", name="Asha")


def test_text_message_shape_matches_cloud_api():
    p = envelope.text_message(C, "wamid.X", "hi", 1767261600)
    value = p["entry"][0]["changes"][0]["value"]
    assert p["object"] == "whatsapp_business_account"
    bsuid = value["contacts"][0]["user_id"]
    assert value["messages"][0] == {
        "from": "919800000001",
        "from_user_id": bsuid,
        "id": "wamid.X",
        "timestamp": "1767261600",
        "type": "text",
        "text": {"body": "hi"},
    }
    assert value["contacts"][0] == {
        "profile": {"name": "Asha"},
        "wa_id": "919800000001",
        "user_id": bsuid,
    }
    # BSUIDs: ISO country code, a period, then the ID (Meta's example: US.13491208655302741918)
    country, _, digits = bsuid.partition(".")
    assert country == "IN" and digits.isdigit()


def test_username_customer_arrives_without_a_phone_number():
    c = envelope.Customer("919800000010", "Ira", username="ira.styles", share_phone=False)
    value = envelope.text_message(c, "wamid.U", "hi", 1)["entry"][0]["changes"][0]["value"]
    assert "from" not in value["messages"][0]
    assert value["messages"][0]["from_user_id"] == c.user_id
    assert value["contacts"][0] == {
        "profile": {"name": "Ira", "username": "ira.styles"},
        "user_id": c.user_id,
    }
    assert c.addressed_by({"recipient": c.user_id})
    assert not c.addressed_by({"to": "919800000010"})  # the agent can't know the number


def test_receipts_follow_metas_bsuid_format():
    c = envelope.Customer("919800000001", "Asha")
    delivered = envelope.status_update(c, "wamid.O", "delivered", 1)["entry"][0]["changes"][0]
    assert delivered["value"]["contacts"][0]["user_id"] == c.user_id
    assert delivered["value"]["statuses"][0]["recipient_user_id"] == c.user_id
    err = {"code": 131026, "title": "Message undeliverable", "details": "x"}
    failed = envelope.status_update(c, "wamid.O", "failed", 2, err)["entry"][0]["changes"][0]
    assert "contacts" not in failed["value"]  # Meta omits contacts from failed receipts
    assert "recipient_user_id" not in failed["value"]["statuses"][0]


def test_signature_is_hmac_over_exact_bytes():
    body = envelope.encode(envelope.text_message(C, "wamid.X", "नमस्ते", 1))
    expected = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert envelope.sign(body, "secret") == f"sha256={expected}"
    # re-serialising with different spacing changes the bytes and must break the signature
    reencoded = json.dumps(json.loads(body)).encode()
    assert envelope.sign(reencoded, "secret") != envelope.sign(body, "secret")


def test_encode_keeps_unicode_unescaped():
    body = envelope.encode(envelope.text_message(C, "w", "कुर्ता", 1))
    assert "कुर्ता".encode() in body


def test_status_rejects_unknown_state():
    import pytest

    with pytest.raises(ValueError):
        envelope.status_update(C, "w", "seen", 1)


def test_clock_and_durations():
    assert parse_duration("25h") == 90000
    assert parse_duration("90s") == 90
    assert parse_duration(30) == 30
    c = SimClock(start=100, tick=5)
    assert c.step() == 105
    assert c.advance(parse_duration("1d")) == 105 + 86400
