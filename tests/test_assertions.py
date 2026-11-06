from turncheck.assertions import check_reply, check_tool

TEXT = {"to": "91", "type": "text", "text": {"body": "Order KC1042 is shipped"}}
TEMPLATE = {"to": "91", "type": "template", "template": {"name": "order_update"}}
BUTTONS = {
    "to": "91",
    "type": "interactive",
    "interactive": {
        "body": {"text": "Pick one"},
        "action": {"buttons": [{"reply": {"id": "a", "title": "A"}}]},
    },
}


def test_contains_is_case_insensitive():
    assert check_reply({"contains": ["kc1042", "SHIPPED"]}, TEXT, "91") is None


def test_wrong_recipient_fails():
    assert "addressed to" in check_reply({}, TEXT, "92")


def test_type_and_template():
    assert check_reply({"type": "template", "template": "order_update"}, TEMPLATE, "91") is None
    assert "expected a template" in check_reply({"type": "template"}, TEXT, "91")
    assert "expected template" in check_reply({"template": "x"}, TEMPLATE, "91")


def test_not_contains_and_matches():
    assert "must not contain" in check_reply({"not_contains": "shipped"}, TEXT, "91")
    assert check_reply({"matches": r"KC\d{4}"}, TEXT, "91") is None


def test_buttons():
    assert check_reply({"buttons": ["A"]}, BUTTONS, "91") is None
    assert "expected buttons" in check_reply({"buttons": ["B"]}, BUTTONS, "91")


def test_tool_args():
    call = {"name": "lookup_order", "args": {"order_id": "KC1042"}}
    assert check_tool({"name": "lookup_order", "args": {"order_id": "KC1042"}}, call) is None
    assert "arg order_id" in check_tool({"name": "lookup_order", "args": {"order_id": "X"}}, call)
    assert "expected tool" in check_tool({"name": "refund"}, call)
