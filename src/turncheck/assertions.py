"""Expectation checks. Each returns None on success or a human-readable failure string."""

from __future__ import annotations

import re

from .envelope import Customer


def describe_message(body: dict) -> str:
    t = body.get("type")
    if t == "text":
        return f"text: {body.get('text', {}).get('body', '')!r}"
    if t == "template":
        return f"template: {body.get('template', {}).get('name')!r}"
    if t == "interactive":
        return f"interactive: {body.get('interactive', {}).get('body', {}).get('text', '')!r}"
    return f"{t}: {body}"


def message_text(body: dict) -> str:
    t = body.get("type")
    if t == "text":
        return body.get("text", {}).get("body", "")
    if t == "interactive":
        return body.get("interactive", {}).get("body", {}).get("text", "")
    return ""


def check_reply(spec: dict | None, body: dict, customer: Customer | str) -> str | None:
    if isinstance(customer, str):  # 0.2 callers passed the wa_id
        customer = Customer(wa_id=customer)
    spec = spec or {}
    if not customer.addressed_by(body):
        got = body.get("to") or body.get("recipient")
        if customer.share_phone:
            want = f"{customer.wa_id!r} (or BSUID {customer.user_id!r} as recipient)"
        else:
            want = f"BSUID {customer.user_id!r} as recipient: this customer shared no phone number"
        return f"reply addressed to {got!r}, expected {want}"
    want_type = spec.get("type")
    if want_type and body.get("type") != want_type:
        return f"expected a {want_type} message, got {describe_message(body)}"
    if "template" in spec:
        name = body.get("template", {}).get("name")
        if name != spec["template"]:
            return f"expected template {spec['template']!r}, got {name!r}"
    text = message_text(body)
    for needle in _as_list(spec.get("contains")):
        if needle.lower() not in text.lower():
            return f"reply {text!r} does not contain {needle!r}"
    for needle in _as_list(spec.get("not_contains")):
        if needle.lower() in text.lower():
            return f"reply {text!r} must not contain {needle!r}"
    if "matches" in spec and not re.search(spec["matches"], text):
        return f"reply {text!r} does not match /{spec['matches']}/"
    if "buttons" in spec:
        got = [
            b.get("reply", {}).get("title")
            for b in body.get("interactive", {}).get("action", {}).get("buttons", [])
        ]
        if got != spec["buttons"]:
            return f"expected buttons {spec['buttons']}, got {got}"
    return None


def check_tool(spec: dict, call: dict) -> str | None:
    if call.get("name") != spec["name"]:
        return f"expected tool {spec['name']!r}, got {call.get('name')!r}"
    for key, want in (spec.get("args") or {}).items():
        got = (call.get("args") or {}).get(key)
        if got != want:
            return f"tool {spec['name']} arg {key}={got!r}, expected {want!r}"
    return None


def _as_list(v) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)
