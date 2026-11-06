"""Console and JUnit XML reporting."""

from __future__ import annotations

from xml.sax.saxutils import escape

from .runner import ScenarioResult


def console(results: list[ScenarioResult]) -> str:
    lines = []
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        lines.append(f"  {mark}  {r.scenario.name}  ({r.seconds:.2f}s)")
        if not r.ok:
            lines.append(f"        -> {r.first_failure}")
    passed = sum(r.ok for r in results)
    lines.append("")
    lines.append(f"{passed}/{len(results)} scenarios passed")
    return "\n".join(lines)


def junit(results: list[ScenarioResult]) -> str:
    failures = sum(not r.ok for r in results)
    total = sum(r.seconds for r in results)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="turncheck" tests="{len(results)}" failures="{failures}" '
        f'time="{total:.3f}">',
    ]
    for r in results:
        name = escape(r.scenario.name, {'"': "&quot;"})
        out.append(
            f'  <testcase name="{name}" classname="{escape(str(r.scenario.path))}" '
            f'time="{r.seconds:.3f}">'
        )
        if not r.ok:
            msg = escape(r.first_failure, {'"': "&quot;"})
            out.append(f'    <failure message="{msg}"/>')
        out.append("  </testcase>")
    out.append("</testsuite>")
    return "\n".join(out) + "\n"
