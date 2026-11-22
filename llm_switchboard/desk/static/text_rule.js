// Mirror of llm_switchboard/desk/text_rule.py; both run text_rule_cases.json.
(function (root) {
  "use strict";

  var MAX_TEXT_BYTES = 4096;
  var FORBIDDEN_FIRST = ["/", "+", "!"];

  // Python's str.strip() whitespace, not JS trim(): Python also strips
  // U+001C..U+001F and U+0085 and keeps U+FEFF; JS is the reverse.
  function isPyWhitespace(code) {
    return (
      (code >= 0x09 && code <= 0x0d) ||
      (code >= 0x1c && code <= 0x20) ||
      code === 0x85 || code === 0xa0 || code === 0x1680 ||
      (code >= 0x2000 && code <= 0x200a) ||
      code === 0x2028 || code === 0x2029 || code === 0x202f ||
      code === 0x205f || code === 0x3000
    );
  }

  function pyStrip(text) {
    var start = 0;
    var end = text.length;
    while (start < end && isPyWhitespace(text.charCodeAt(start))) start++;
    while (end > start && isPyWhitespace(text.charCodeAt(end - 1))) end--;
    return text.slice(start, end);
  }

  function badChar(code) {
    if (code === 0x0a || code === 0x09) return false;
    return (
      code < 0x20 ||
      (code >= 0x7f && code <= 0x9f) ||
      (code >= 0x202a && code <= 0x202e) ||
      (code >= 0x2066 && code <= 0x2069) ||
      code === 0x2028 || code === 0x2029
    );
  }

  function hasLoneSurrogate(text) {
    for (var i = 0; i < text.length; i++) {
      var code = text.charCodeAt(i);
      if (code >= 0xd800 && code <= 0xdbff) {
        var next = text.charCodeAt(i + 1);
        if (next >= 0xdc00 && next <= 0xdfff) {
          i++;
          continue;
        }
        return true;
      }
      if (code >= 0xdc00 && code <= 0xdfff) return true;
    }
    return false;
  }

  // A lone surrogate is 3 bytes in Python's surrogatepass and 3 (U+FFFD) in
  // TextEncoder, so the length matches either way.
  function utf8Length(text) {
    return new TextEncoder().encode(text).length;
  }

  function checkText(text) {
    if (typeof text !== "string" || text === "") return "empty";
    if (utf8Length(text) > MAX_TEXT_BYTES) return "too long";
    if (hasLoneSurrogate(text)) return "invalid character";
    if (text !== pyStrip(text)) return "leading or trailing whitespace";
    for (var i = 0; i < text.length; i++) {
      if (badChar(text.charCodeAt(i))) return "control character";
    }
    if (FORBIDDEN_FIRST.indexOf(text.charAt(0)) !== -1) {
      return "starts with a command character";
    }
    return null;
  }

  function normalizeText(text) {
    return pyStrip(text.replace(/\r\n/g, "\n").replace(/\r/g, "\n"));
  }

  var api = { checkText: checkText, normalizeText: normalizeText, MAX_TEXT_BYTES: MAX_TEXT_BYTES };
  root.DeskTextRule = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
