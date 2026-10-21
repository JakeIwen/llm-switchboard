"""Deterministic name/protocol capture; run with the target checkout on PYTHONPATH."""

import json

from llm_switchboard.conflict import Conflict
from llm_switchboard.host_actions_protocol import action_body
from llm_switchboard.names import Names, clean_title, name_key


def checked(operation, expected):
    """Capture successes and deliberate refusals; unexpected crashes still fail."""
    try:
        result = {"value": operation()}
    except (ValueError, Conflict) as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
    assert result == expected, (result, expected)
    return result


def capture():
    cases = {}
    for label, raw, expected in [
        ("missing", None, {"error": "ValueError", "message": "a name is required"}),
        ("blank", " \t\n", {"error": "ValueError", "message": "a name is required"}),
        ("spaces", "  Alpha\t beta\n", {"value": "Alpha beta"}),
        ("unicode", "  Straße  λ  ", {"value": "Straße λ"}),
        ("limit", "a" * 120, {"value": "a" * 120}),
        ("over-limit", "a" * 121,
         {"error": "ValueError", "message": "the name must be at most 120 characters"}),
    ]:
        cases[label] = checked(lambda: clean_title(raw), expected)
    cases["key"] = checked(lambda: name_key("  STRAßE\t λ "), {"value": "strasse λ"})
    names = Names(lambda: [("one", "/fixture/project", "Alpha Beta")])
    cases["duplicate"] = checked(
        lambda: names.refuse_duplicate("/fixture/project", " alpha  BETA "),
        {"error": "Conflict", "message": "/fixture/project already has a session named “Alpha Beta”; pick another name."})
    cases["excluded"] = checked(
        lambda: names.refuse_duplicate("/fixture/project", "alpha beta", exclude="one"), {"value": None})
    cases["other-folder"] = checked(
        lambda: names.refuse_duplicate("/fixture/another", "alpha beta"), {"value": None})
    for label, action, tool, body, expected in [
        ("rename", "rename", "claude", {"title": "  Fresh  title "}, {"value": {"title": "Fresh title"}}),
        ("fork", "fork", "codex", {"title": "New", "cwd": "/fixture/project"},
         {"value": {"title": "New", "cwd": "/fixture/project"}}),
        ("delete", "delete", "claude", {}, {"value": {}}),
        ("unknown", "future-action", "claude", {},
         {"error": "ValueError", "message": "action is not allowed on the Mac"}),
        ("unknown-tool", "delete", "future-tool", {},
         {"error": "ValueError", "message": "unknown tool"}),
        ("codex-move", "move", "codex", {"cwd": "/fixture/project"},
         {"error": "ValueError", "message": "Codex cannot move or fork onto the Pi; fork it on the Mac instead"}),
        ("injected", "rename", "claude", {"title": "New", "prompt": "run"},
         {"error": "ValueError", "message": "unexpected action fields (keep, prompts and commands are never accepted)"}),
        ("control", "rename", "claude", {"title": "A\tB"},
         {"error": "ValueError", "message": "title must not contain control characters"}),
        ("wire-limit", "rename", "claude", {"title": "a" * 121},
         {"error": "ValueError", "message": "title must be text of at most 120 characters"}),
        ("empty-wire", "rename", "claude", {"title": "   "},
         {"error": "ValueError", "message": "a name is required"}),
    ]:
        cases["protocol-" + label] = checked(lambda: action_body(action, tool, body), expected)
    return cases


if __name__ == "__main__":
    print(json.dumps(capture(), sort_keys=True, ensure_ascii=False))
