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
    assert value["messages"][0] == {
        "from": "919800000001",
        "id": "wamid.X",
        "timestamp": "1767261600",
        "type": "text",
        "text": {"body": "hi"},
    }
    assert value["contacts"][0]["profile"]["name"] == "Asha"


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
