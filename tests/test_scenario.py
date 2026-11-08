from pathlib import Path

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


def test_lint_takes_several_targets_and_reports_every_error(tmp_path, capsys):
    from turncheck.cli import main

    examples = Path(__file__).resolve().parent.parent / "examples" / "scenarios"
    good = sorted(examples.glob("*.yaml"))[:2]
    assert main(["lint", *map(str, good)]) == 0
    assert capsys.readouterr().out.count("  ok  ") == 2

    (tmp_path / "a.yaml").write_text("name: a\nsteps: []\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("name: b\nsteps: [{teleport: 1}]\n", encoding="utf-8")
    missing = tmp_path / "nope"
    assert main(["lint", str(tmp_path), str(missing), str(good[0])]) == 2
    err = capsys.readouterr().err
    assert "a.yaml" in err and "b.yaml" in err
    assert "nope: no such file or directory" in err


def test_lint_rejects_a_folder_without_scenarios(tmp_path, capsys):
    from turncheck.cli import main

    assert main(["lint", str(tmp_path)]) == 2
    assert "no scenario files" in capsys.readouterr().err
