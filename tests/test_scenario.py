import pytest

from turncheck.scenario import ScenarioError, load


def write(tmp_path, text):
    p = tmp_path / "s.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_steps_and_refs(tmp_path):
    sc = load(
        write(
            tmp_path,
            """
name: x
customer: {wa_id: "91", name: A}
steps:
  - send: hi
    as: first
  - expect_reply: {contains: hi}
  - redeliver: first
  - advance: 25h
""",
        )
    )
    assert [s.kind for s in sc.steps] == ["send", "expect_reply", "redeliver", "advance"]
    assert sc.steps[0].ref == "first"
    assert sc.steps[3].arg == 90000


@pytest.mark.parametrize(
    "steps, message",
    [
        ("  - expect_reply: {}\n", "first step must be a stimulus"),
        ("", "is empty"),
        ("  - send: hi\n    expect_reply: {}\n", "exactly one action"),
        ("  - shout: hi\n", "unknown key"),
        ("  - redeliver: nope\n", "unknown message"),
        ("  - status: {of: last_reply}\n", "needs a 'state'"),
        ("  - advance: soon\n", "bad duration"),
    ],
)
def test_rejects_bad_scenarios(tmp_path, steps, message):
    with pytest.raises((ScenarioError, ValueError), match=message):
        load(write(tmp_path, "steps:\n" + steps if steps else "steps: []\n"))
