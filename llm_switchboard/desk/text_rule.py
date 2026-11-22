"""The prompt text rule the hub enforces and the page mirrors
(static/text_rule.js; both run text_rule_cases.json). The hub never rewrites
text: it refuses anything check_text rejects, so the text Jacob reviewed is
exactly the text Claude gets."""

MAX_TEXT_BYTES = 4096
# A leading command character would read as a slash command, a + command or a
# shell escape to a human; the model acts on such text with full authority.
FORBIDDEN_FIRST = ("/", "+", "!")
_ALLOWED_CONTROLS = {"\n", "\t"}
_BIDI = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}
_SEPARATORS = {" ", " "}


def _bad_char(ch: str) -> bool:
    code = ord(ch)
    if ch in _ALLOWED_CONTROLS:
        return False
    return code < 0x20 or 0x7F <= code <= 0x9F or ch in _BIDI or ch in _SEPARATORS


def check_text(text: str) -> str | None:
    """None when the hub accepts text as is, else the reason it refuses."""
    if not isinstance(text, str) or not text:
        return "empty"
    if len(text.encode("utf-8", "surrogatepass")) > MAX_TEXT_BYTES:
        return "too long"
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return "invalid character"
    if text != text.strip():
        return "leading or trailing whitespace"
    if any(_bad_char(ch) for ch in text):
        return "control character"
    if text.startswith(FORBIDDEN_FIRST):
        return "starts with a command character"
    return None


def normalize_text(text: str) -> str:
    """What the page applies before showing the text for review."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()
