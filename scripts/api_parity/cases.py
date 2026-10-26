"""Route × case inventory for the old/new HTTP oracle."""

from dataclasses import dataclass

from .fixtures import MISSING, REMOTE_IDS


@dataclass(frozen=True)
class Case:
    label: str
    path: str
    method: str = "GET"
    body: object = None
    status: int = 200
    content_type: str = "application/json"
    raw: bytes | None = None
    save: str = ""


def get_cases(sid):
    cases = [Case("double-slash prefix", "//api/settings"),
             Case("sessions with Mac metadata", "/api/sessions"), Case("folders", "/api/folders"),
             Case("local and Mac trash", "/api/trash"), Case("settings", "/api/settings"),
             Case("Claude messages", f"/api/sessions/{sid}/messages"),
             Case("Codex messages", "/api/sessions/{codex}/messages"),
             Case("screen without window", f"/api/sessions/{sid}/screen"),
             Case("screen missing session", f"/api/sessions/{MISSING}/screen"),
             Case("missing messages", f"/api/sessions/{MISSING}/messages", status=404),
             Case("malformed messages id", "/api/sessions/nope/messages", status=404),
             Case("index status", "/api/search/status"),
             Case("index status ignores query", "/api/search/status?unused=1"),
             Case("search local and remote", "/api/search?q=alpha"),
             Case("search no results", "/api/search?q=not-a-fixture-term"),
             Case("search bad query", "/api/search?q=", status=400),
             Case("Claude context", f"/api/search/claude/{sid}?q=prompt"),
             Case("Codex context", "/api/search/codex/{codex}?q=ochre"),
             Case("Codex missing context", "/api/search/codex/{codex}?q=missingword", status=404),
             Case("missing search detail", f"/api/search/claude/{MISSING}?q=prompt", status=404),
             Case("malformed detail", "/api/search/claude/nope?q=prompt", status=404),
             Case("wrong tool", f"/api/search/nope/{sid}?q=prompt", status=404)]
    # The root picker and its assets intentionally differ from the old server.
    cases += [Case("not found " + path, path, status=404) for path in (
        "/api/settings?x=1", "/api/sessions/", "/api/sessions/nope/explode", "/api/%73ettings",
        "/api/hosts/mac/sessions/" + sid + "/messages")]
    return cases


def post_cases(sid, trash_sid):
    base = f"/api/sessions/{sid}"
    cases = [Case("settings partial", "/api/settings", "POST", {"poll_seconds": 9, "folders": {"depth": 3}}),
             Case("settings nested invalid", "/api/settings", "POST", {"folders": []}, 400),
             Case("settings range invalid", "/api/settings", "POST", {"poll_seconds": True}, 400),
             Case("settings unknown", "/api/settings", "POST", {"unknown": 4}, 400),
             Case("settings command invalid", "/api/settings", "POST", {"claude_command": "echo;true"}, 400),
             Case("settings empty body", "/api/settings", "POST", raw=b""),
             Case("settings retained", "/api/settings"),
             Case("enable", base + "/enable", "POST", {}),
             Case("disable", base + "/disable", "POST", {}),
             Case("restart", base + "/restart", "POST", {}),
             Case("name unicode", base + "/name", "POST", {"name": "Café label"}),
             Case("bad name", base + "/name", "POST", {"name": 12}, 400),
             Case("rename", base + "/rename", "POST", {"title": "Parity renamed"}),
             Case("rename empty", base + "/rename", "POST", {"title": ""}, 400),
             Case("move invalid", base + "/move", "POST", {"cwd": "/no/such/parity-folder"}, 400),
             Case("move", base + "/move", "POST", {"cwd": "{home}/dev/beta"}),
             Case("move no-op", base + "/move", "POST", {"cwd": "{home}/dev/beta"}),
             Case("fork", base + "/fork", "POST", {"title": "Parity fork"}, save="forked"),
             Case("fork duplicate", base + "/fork", "POST", {"title": "Parity fork"}, 409),
             Case("fork empty title", base + "/fork", "POST", {}, 400),
             Case("delete", base + "/delete", "POST", {}),
             Case("delete missing", base + "/delete", "POST", {}, 404),
             Case("restore", base + "/restore", "POST", {}),
             Case("restore not trashed", base + "/restore", "POST", {}, 404),
             Case("restore seeded trash", f"/api/sessions/{trash_sid}/restore", "POST", {}),
             Case("new Claude", "/api/sessions/new", "POST", {"cwd": "{home}/dev/alpha", "name": "Parity new"}, save="created"),
             Case("new duplicate", "/api/sessions/new", "POST", {"cwd": "{home}/dev/alpha", "name": "Parity new"}, 409),
             Case("new bad folder", "/api/sessions/new", "POST", {"cwd": "/no/such", "name": "x"}, 400),
             Case("new untrusted", "/api/sessions/new", "POST", {"cwd": "{home}/scratch", "name": "x"}, 409),
             Case("delete pending", "/api/sessions/{created}/delete", "POST", {}),
             Case("unknown POST", "/api/nope", "POST", {}, 404),
             Case("escaped slash in id", "/api/sessions/part%2Fpart/rename", "POST", {}, 400),
             Case("escaped action", base + "/ren%61me", "POST", {}, 404),
             Case("unknown local action", base + "/explode", "POST", {}, 404),
             Case("GET action as POST", base + "/screen", "POST", {}, 404)]
    cases += codex_cases()
    cases += remote_cases()
    # Each real write route gets its JSON-only and object-envelope checks.
    paths = ["/api/settings", "/api/sessions/new", "/api/trash/empty"]
    paths += [base + "/" + action for action in ("enable", "disable", "restart", "name", "rename", "delete", "restore", "move", "fork")]
    paths += [f"/api/hosts/mac/sessions/{remote}/{action}" for action, remote in REMOTE_IDS.items()
              if action not in ("readonly", "codex")]
    for path in paths:
        cases += [Case("JSON-only " + path, path, "POST", {}, 415, "text/plain"),
                  Case("object-only " + path, path, "POST", [], 400),
                  Case("malformed JSON " + path, path, "POST", status=400, raw=b"{")]
    for action in ("enable", "disable", "restart", "name", "rename", "delete", "restore", "move", "fork"):
        cases.append(Case("bad id " + action, f"/api/sessions/nope/{action}", "POST", {}, 400))
    cases += [Case("oversize", "/api/settings", "POST", status=400, raw=b"x" * 16385),
              Case("trash after actions", "/api/trash"),
              Case("empty trash", "/api/trash/empty", "POST", {}),
              Case("empty trash again", "/api/trash/empty", "POST", {})]
    return cases


def codex_cases():
    base = "/api/sessions/{codex}"
    return [Case("Codex rename", base + "/rename", "POST", {"title": "Codex renamed"}),
            Case("Codex cannot enable", base + "/enable", "POST", {}, 400),
            Case("Codex cannot move", base + "/move", "POST", {"cwd": "{home}/dev/beta"}, 400),
            Case("Codex fork", base + "/fork", "POST", {"title": "Codex fork"}, save="codex_fork"),
            Case("Codex delete", "/api/sessions/{codex_fork}/delete", "POST", {}),
            Case("Codex restore", "/api/sessions/{codex_fork}/restore", "POST", {}),
            Case("Codex delete restored", "/api/sessions/{codex_fork}/delete", "POST", {}),
            Case("Codex needs prompt", "/api/sessions/new", "POST", {"tool": "codex", "name": "New Codex", "cwd": "{home}/dev/alpha"}, 400),
            Case("new Codex", "/api/sessions/new", "POST", {"tool": "codex", "name": "New Codex", "cwd": "{home}/dev/alpha", "prompt": "Hello"}, save="codex_new"),
            Case("Codex busy delete", "/api/sessions/{codex_new}/delete", "POST", {}, 409),
            Case("new Codex messages", "/api/sessions/{codex_new}/messages")]


def remote_cases():
    sid = REMOTE_IDS["rename"]
    base = f"/api/hosts/mac/sessions/{sid}"
    cases = [Case("host missing", f"/api/hosts/mac/sessions/{MISSING}/rename", "POST", {"title": "x"}, 404),
             Case("host bad id", "/api/hosts/mac/sessions/nope/rename", "POST", {"title": "x"}, 400),
             Case("host invalid name", f"/api/hosts/Mac/sessions/{sid}/rename", "POST", {}, 404),
             Case("offline host", f"/api/hosts/offline/sessions/{sid}/rename", "POST", {"title": "x"}, 409),
             Case("unknown host action", base + "/enable", "POST", {}, 400),
             Case("host unexpected fields", base + "/rename", "POST", {"title": "x", "keep": True}, 400),
             Case("host readonly", f"/api/hosts/mac/sessions/{REMOTE_IDS['readonly']}/rename", "POST", {"title": "x"}, 409),
             Case("host Codex no move", f"/api/hosts/mac/sessions/{REMOTE_IDS['codex']}/move", "POST", {"cwd": "/Users/me/dev/beta"}, 400)]
    for action in ("rename", "delete", "restore", "move", "fork", "fork-pi"):
        body = {"title": "Remote copy " + action} if action in ("rename", "fork", "fork-pi") else {}
        if action in ("move", "fork"):
            body["cwd"] = "/Users/me/dev/beta"
        cases.append(Case("host " + action, f"/api/hosts/mac/sessions/{REMOTE_IDS[action]}/{action}", "POST", body))
    cases += [Case("host already pending", base + "/rename", "POST", {"title": "Again"}, 409),
              Case("host action list", "/api/sessions")]
    return cases
