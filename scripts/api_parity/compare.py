"""Response normalization and bounded diagnostics for the parity oracle."""

import hashlib
import json
import re

TIME_KEYS = {"now", "mtime", "at", "queued_at", "last_seen", "deleted_at", "last_updated", "started_at", "matched_at"}


class Comparison:
    def __init__(self, homes):
        self.homes = homes
        self.ids = [{}, {}]
        self.results = []

    def remember(self, left, right):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return
        for key in ("id", "action_id"):
            if key in left and key in right and left[key] != right[key]:
                token = f"<generated-{len(self.ids[0]) + 1}>"
                for mapping, value in zip(self.ids, (left[key], right[key])):
                    mapping[value] = token

    def normalize(self, value, side, key=""):
        if isinstance(value, bytes):
            return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
        if key in TIME_KEYS and isinstance(value, (float, int)) and value != 0:
            return "<time>"
        if isinstance(value, dict):
            return {k: self.normalize(v, side, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.normalize(v, side) for v in value]
        if isinstance(value, str):
            home = str(self.homes[side])
            value = value.replace(home, "<home>").replace(re.sub(r"[^a-zA-Z0-9]", "-", home), "<encoded-home>")
            for actual, token in self.ids[side].items():
                value = value.replace(actual, token)
                if key == "short_id" and value in (actual[:8], actual[-8:]):
                    value = token + "-short"
            return value
        return value

    def compare(self, case, responses, paths):
        self.remember(responses[0][3], responses[1][3])
        normalized = [self.normalize(response, side) for side, response in enumerate(responses)]
        encoded = [json.dumps(value, ensure_ascii=True) for value in normalized]
        matched = encoded[0] == encoded[1] and all(response[0] == case.status for response in responses)
        row = {"case": case.label, "method": case.method, "path": paths[0], "matched": matched,
               "old_status": responses[0][0], "new_status": responses[1][0], "expected": case.status}
        if not matched:
            row["old"], row["new"] = normalized
        self.results.append(row)
        return matched

    def report(self, servers):
        failures = [row for row in self.results if not row["matched"]]
        lines = ["# HTTP parity", "", f"{len(self.results) - len(failures)}/{len(self.results)} cases matched.", "",
                 "Status, Content-Type, Cache-Control, JSON field order/types and bodies are compared. Static files are byte-hashed.",
                 "Only times, generated UUIDs/short ids, and equivalent fixture HOME prefixes are normalized.",
                 "Both use the real supervisor/indexer/tmux with an offline connectivity probe; no model process can start.",
                 "LLM_SWITCHBOARD_STRICT_RESPONSES=1 makes any new-server schema mismatch fatal during this comparison.", "",
                 "| Server | Ready (s) | RSS (KiB) | SIGTERM code | tmux sentinel survives |",
                 "| --- | ---: | ---: | ---: | --- |"]
        for server in servers:
            lines.append(f"| {server.home.name} | {server.startup_s:.3f} | {server.rss_kb} | {server.stopped_code} | {server.sentinel_survived} |")
        lines += ["", "| Case | Method / route | Old | New | Result |", "| --- | --- | ---: | ---: | --- |"]
        for row in self.results:
            path = self.normalize(row["path"], 0)
            lines.append(f"| {row['case']} | {row['method']} `{path}` | {row['old_status']} | {row['new_status']} | {'MATCH' if row['matched'] else 'DIFF'} |")
        for row in failures:
            lines += ["", "## " + row["case"], f"Expected HTTP {row['expected']}", "", "```json",
                      json.dumps({"old": row["old"], "new": row["new"]}, indent=2), "```"]
        return "\n".join(lines) + "\n"
