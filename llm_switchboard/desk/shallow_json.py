"""Parsing JSON another process wrote, with a nesting bound checked before
the parser runs.

The rule: a document whose arrays and objects nest deeper than the caller's
`max_depth` is refused without `json.loads` ever seeing it. Catching
RecursionError isn't enough: on CPython 3.13 the C parser's recursion in a
small-stack thread overflows into a crash of the whole process instead of
raising (tests/test_desk_deep_json.py).

The depth count is one linear pass that skips string contents, so brackets
and escaped quotes inside a string don't count. For any text, the nesting
`json.loads` would reach before it stops (finished or failed) is at most
this count: up to the point where the parser fails, the text is a valid
JSON prefix, whose strings this pass delimits exactly as the parser does.
Standard library only.
"""

import json
import re

# The characters that can change the count's state: brackets outside
# strings, and quotes and backslashes, which delimit strings.
_SIGNIFICANT = re.compile(r'[\[\]{}"\\]')


class ShallowJsonError(ValueError):
    """Refused before parsing: not strict UTF-8, unbalanced, or too deep."""


def nesting_depth(text: str) -> int:
    """The deepest array or object nesting in `text` (a flat object is 1,
    a bare scalar 0), counting no bracket inside a string; -1 when a closing
    bracket comes before any opening one, which no JSON document has."""
    depth = deepest = 0
    in_string = False
    pos = 0
    search = _SIGNIFICANT.search
    while (match := search(text, pos)) is not None:
        char = match.group()
        pos = match.end()
        if in_string:
            if char == "\\":
                pos += 1  # the escaped character, whatever it is
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
            if depth < 0:
                return -1
        # A backslash outside a string is invalid JSON; the parser refuses it.
    return deepest


def loads_shallow(data: bytes | bytearray | str, max_depth: int):
    """`json.loads` of `data` (bytes must be strict UTF-8), refused with
    ShallowJsonError, before any parsing, when it nests deeper than
    `max_depth` or closes a bracket it never opened. Other malformed JSON
    raises json.JSONDecodeError (also a ValueError), as `json.loads` does."""
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ShallowJsonError("not UTF-8") from exc
    else:
        text = data
    depth = nesting_depth(text)
    if depth < 0:
        raise ShallowJsonError("unbalanced brackets")
    if depth > max_depth:
        raise ShallowJsonError("JSON nests too deeply")
    return json.loads(text)
