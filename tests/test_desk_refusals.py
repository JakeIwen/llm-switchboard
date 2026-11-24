"""The hub's refusals are declared once (llm_switchboard/desk/refusals.py):
every reason the hub's modules (HUB_MODULES) answer with,
and every reason the text rule gives, is a `Refusal`, or one of the few
named pass-throughs of free text from a store. A refusal added anywhere
else in those modules fails here until it is declared."""

import ast
import json
from pathlib import Path
import unittest

from llm_switchboard.desk.refusals import NOT_SENT, Delivery, Refusal
from llm_switchboard.desk.text_rule import check_text

DESK = Path(__file__).resolve().parents[1] / "llm_switchboard" / "desk"
HUB_MODULES = ("hub.py", "hub_state.py", "grants.py", "http_server.py", "link_hub.py", "link_views.py",
               "enrollment_views.py", "hub_enrollment.py", "remote_enrollment.py", "hub_link_ops.py",
               "link_fence.py")

# (module, function, expression): reasons that are not the hub's own words.
PASS_THROUGH = {
    # The lock store's own message (locks.py), or Refusal.LOCK_FILE_FULL.
    ("hub_state.py", "_check_routing", "self._store_error()"),
    ("hub_state.py", "begin_send", "self._store_error()"),
    ("hub_state.py", "hand_off", "reason"),
    ("hub_state.py", "clear_lock", "self._lock_error"),
    ("link_views.py", "clear_stopped", "self._lock_error"),
    # check_text's result, itself a Refusal (test_the_text_rule_gives_only_declared_reasons).
    ("hub_state.py", "begin_send", "reason"),
    # A DeskError's reason, answered as it was raised.
    ("http_server.py", "_dispatch", "exc.reason"),
    ("hub.py", "handle_operator", "exc.reason"),
}


def _is_refusal(node) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "Refusal" and node.attr in Refusal.__members__)


def _refusal_params(func) -> set[str]:
    args = func.args.posonlyargs + func.args.args + func.args.kwonlyargs
    return {a.arg for a in args if isinstance(a.annotation, ast.Name) and a.annotation.id == "Refusal"}


def _declared(module, func, node) -> bool:
    if _is_refusal(node):
        return True
    if isinstance(node, ast.IfExp):
        return _declared(module, func, node.body) and _declared(module, func, node.orelse)
    if isinstance(node, ast.Name) and func is not None and node.id in _refusal_params(func):
        return True
    return (module, func.name if func else None, ast.unparse(node)) in PASS_THROUGH


def _reasons(tree):
    """(enclosing function, reason node, line) for every DeskError(status,
    reason) call and every {"error": reason} dict literal."""
    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Call):
                name = child.func.id if isinstance(child.func, ast.Name) else getattr(child.func, "attr", None)
                if name == "DeskError":
                    reason = child.args[1] if len(child.args) > 1 else next(
                        (k.value for k in child.keywords if k.arg == "reason"), None)
                    yield func, reason, child.lineno
            if isinstance(child, ast.Dict):
                for key, value in zip(child.keys, child.values):
                    if isinstance(key, ast.Constant) and key.value == "error":
                        yield func, value, child.lineno
            yield from walk(child, inner)
    yield from walk(tree, None)


class RefusalVocabularyTests(unittest.TestCase):
    def test_every_refusal_the_hub_answers_with_is_declared(self):
        found = 0
        for module in HUB_MODULES:
            tree = ast.parse((DESK / module).read_text(encoding="utf-8"))
            for func, reason, line in _reasons(tree):
                found += 1
                with self.subTest(module=module, line=line):
                    self.assertIsNotNone(reason)
                    self.assertTrue(_declared(module, func, reason),
                                    f"{module}:{line}: {ast.unparse(reason)} is not a Refusal")
        self.assertGreater(found, 60)

    def test_every_pass_through_still_exists(self):
        # A pass-through whose site is gone would let a new one hide behind it.
        seen = set()
        for module in HUB_MODULES:
            tree = ast.parse((DESK / module).read_text(encoding="utf-8"))
            seen |= {(module, f.name if f else None, ast.unparse(r)) for f, r, _ in _reasons(tree) if r is not None}
        self.assertLessEqual(PASS_THROUGH, seen)

    def test_the_text_rule_gives_only_declared_reasons(self):
        func = next(n for n in ast.parse((DESK / "text_rule.py").read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef) and n.name == "check_text")
        returns = [n.value for n in ast.walk(func) if isinstance(n, ast.Return)]
        self.assertTrue(returns)
        for value in returns:
            with self.subTest(line=value.lineno):
                self.assertTrue(_is_refusal(value) or (isinstance(value, ast.Constant) and value.value is None),
                                ast.unparse(value))
        cases = json.loads((DESK / "text_rule_cases.json").read_text(encoding="utf-8"))["check"]
        reasons = [check_text(case["repeat"] * case["count"] if "repeat" in case else case["text"]) for case in cases]
        self.assertTrue(all(r is None or isinstance(r, Refusal) for r in reasons))
        self.assertGreater(len({r for r in reasons if r is not None}), 3)

    def test_every_refusal_states_its_delivery_class(self):
        self.assertTrue(all(isinstance(r.delivery, Delivery) for r in Refusal))
        self.assertEqual(NOT_SENT, {r for r in Refusal if r.delivery is Delivery.NOT_SENT})
        self.assertIs(Refusal.NOT_FOUND.delivery, Delivery.UNPROVEN)
        self.assertIs(Refusal.UNAUTHORIZED.delivery, Delivery.NOT_SENT)
        # A member whose class is not a Delivery doesn't import.
        source = (DESK / "refusals.py").read_text(encoding="utf-8")
        anchor = '    NOT_FOUND = "not found"\n'
        self.assertIn(anchor, source)
        with self.assertRaises(TypeError):
            exec(compile(source.replace(anchor, anchor + '    NEW_REFUSAL = "new refusal", "not sent"\n'),
                         "refusals.py", "exec"), {})

    def test_a_refusal_is_its_reason(self):
        self.assertEqual(Refusal.BUSY, "busy")
        self.assertEqual(json.dumps({"error": Refusal.BUSY}), '{"error": "busy"}')
        self.assertEqual(f"{Refusal.BUSY}", "busy")
        self.assertEqual(str(Exception(Refusal.BUSY)), "busy")
        self.assertIn(Refusal.BUSY, {"busy"})


if __name__ == "__main__":
    unittest.main()
