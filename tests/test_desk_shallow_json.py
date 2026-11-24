"""desk/shallow_json.py: the nesting bound is decided before `json.loads`
runs, so no reader depends on RecursionError (which CPython 3.13 turns into
a process crash in a small-stack thread)."""

import json
import random
import threading
import unittest
from unittest import mock

from llm_switchboard.desk import shallow_json
from llm_switchboard.desk.shallow_json import ShallowJsonError, loads_shallow, nesting_depth

# Characters that would confuse a count that didn't skip strings.
_TRICKY = ['[', ']', '{', '}', '"', '\\', '\\"', '"]', '\\\\', '\n', 'é', ' ', '\U0001f600', 'a', ' ']


def _random_string(rng: random.Random) -> str:
    return "".join(rng.choice(_TRICKY) for _ in range(rng.randint(0, 6)))


def _random_value(rng: random.Random, budget: int):
    kind = rng.randint(0, 5) if budget > 0 else rng.randint(0, 2)
    if kind == 0:
        return _random_string(rng)
    if kind == 1:
        return rng.choice([0, -1.5, True, False, None, 12345678901234567890])
    if kind == 2:
        return rng.choice([[], {}])
    if kind in (3, 4):
        return [_random_value(rng, budget - 1) for _ in range(rng.randint(1, 3))]
    return {_random_string(rng): _random_value(rng, budget - 1) for _ in range(rng.randint(1, 3))}


def _true_depth(value) -> int:
    if isinstance(value, list):
        return 1 + max((_true_depth(item) for item in value), default=0)
    if isinstance(value, dict):
        return 1 + max((_true_depth(item) for item in value.values()), default=0)
    return 0


def _small_stack(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            out["error"] = exc
    old = threading.stack_size(256 * 1024)
    try:
        thread = threading.Thread(target=run)
        thread.start()
    finally:
        threading.stack_size(old)
    thread.join()
    return out


class NestingDepthTest(unittest.TestCase):
    def test_depth_agrees_with_true_depth_over_generated_documents(self):
        rng = random.Random(20261124)
        for _ in range(2000):
            value = _random_value(rng, rng.randint(0, 8))
            for text in (json.dumps(value), json.dumps(value, ensure_ascii=False),
                         json.dumps(value, indent=1)):
                with self.subTest(text=text[:80]):
                    self.assertEqual(nesting_depth(text), _true_depth(value))

    def test_bound_admits_exactly_max_depth_over_generated_documents(self):
        rng = random.Random(7)
        for _ in range(500):
            value = _random_value(rng, rng.randint(1, 8))
            depth = _true_depth(value)
            text = json.dumps(value)
            self.assertEqual(loads_shallow(text, depth), value)
            self.assertEqual(loads_shallow(text.encode(), depth), value)
            if depth:
                with self.assertRaises(ShallowJsonError):
                    loads_shallow(text, depth - 1)

    def test_brackets_and_escaped_quotes_inside_strings_do_not_count(self):
        for text, depth in [('"[[[{{{"', 0), ('["\\"[[[", "]"]', 1), ('{"a\\\\": "[\\\\\\"{"}', 1),
                            ('[["\\\\"], "\\"]]]]"]', 2)]:
            with self.subTest(text=text):
                self.assertEqual(nesting_depth(text), depth)
                self.assertEqual(_true_depth(json.loads(text)), depth)

    def test_an_unterminated_string_hides_no_nesting_from_the_parser(self):
        # The parser stops at the unterminated string; brackets before it count.
        self.assertEqual(nesting_depth('[[["abc[[[['), 3)
        with self.assertRaises(ValueError):
            loads_shallow('[[["abc[[[[', 3)

    def test_a_close_before_its_open_is_refused_unparsed(self):
        with mock.patch.object(shallow_json.json, "loads") as loads:
            for text in ("]", "}[", '"x"]['):
                with self.subTest(text=text), self.assertRaises(ShallowJsonError):
                    loads_shallow(text, 64)
        loads.assert_not_called()

    def test_bytes_must_be_strict_utf8(self):
        for data in (b'"\xff"', b'"\xed\xa0\x80"'):  # an invalid byte, an encoded surrogate
            with self.subTest(data=data), self.assertRaises(ShallowJsonError):
                loads_shallow(data, 4)

    def test_deep_document_refused_in_small_stack_without_parsing(self):
        deep = "[" * 30000 + "]" * 30000
        nested_in_object = '{"a": ' * 30000 + "1" + "}" * 30000
        with mock.patch.object(shallow_json.json, "loads") as loads:
            for text in (deep, deep.encode(), nested_in_object):
                out = _small_stack(lambda: loads_shallow(text, 8))
                self.assertIsInstance(out.get("error"), ShallowJsonError)
        loads.assert_not_called()


if __name__ == "__main__":
    unittest.main()
