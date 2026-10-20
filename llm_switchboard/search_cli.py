"""Terminal output for transcript search and the manual index command."""

from pathlib import Path
import json
import sys

from .search_api import SearchHandlers
from .search_indexer import run_index
from .search_query import QueryError


def run_search(args, index_file: Path, format_age) -> int:
    raw = " ".join(args.query)
    handlers = SearchHandlers(index_file)
    try:
        payload = handlers.results(raw, limit=max(200 if args.session else 1, args.limit))
    except QueryError as exc:
        print(f"search: {exc}", file=sys.stderr)
        return 2
    status = payload["index"]
    detail = f": {status['error']}" if status["error"] else ""
    if status["last_updated"] is None:
        # Scripts such as llmsearch.sh fall back to grep on this exit code.
        print(f"search: the transcript index isn't built yet ({status['state']}{detail}); "
              "`llm-switchboard index` builds it", file=sys.stderr)
        return 3
    results = payload["results"]
    if args.session:
        result = _search_result(results, args.session)
        if result is None:
            print(f"search: no unique result matches {args.session!r}", file=sys.stderr)
            return 1
        found = handlers.detail(result["tool"], result["id"], raw)
        if found is None:
            print("search: matching excerpts are unavailable", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(found["detail"]))
        else:
            _print_search_detail(found["detail"])
        return 0
    if args.json:
        for result in results:
            print(json.dumps(result))
        return 0
    if not results:
        note = f" ({status['state']}{detail})" if status["state"] != "ready" else ""
        print("No transcript matches." + note)
        return 0
    for result in results:
        stamp = format_age(result["matched_at"])
        print(f"{result['tool']:<6} {result['short_id']}  {stamp:>4}  {result['count']:>3}  "
              f"{result['project']}  {result['title']}")
        print(f"       {result['excerpt']}")
        print(f"       {result['resume_cmd']}")
    return 0


def run_index_command(index_file: Path, projects_dir: Path, codex_dir: Path) -> int:
    stats = run_index(index_file, projects_dir, codex_dir)
    if stats.already_running:
        print("Transcript indexing is already running.")
    else:
        print(f"Indexed {stats.sessions} sessions in {stats.files} files; "
              f"{stats.changed} changed, {stats.removed} removed, "
              f"{stats.bytes_read} bytes read in {stats.elapsed_s:.2f}s.")
    return 0


def _search_result(results: list[dict], selector: str) -> dict | None:
    tool, separator, prefix = selector.partition(":")
    if not separator:
        prefix, tool = selector, ""
    matches = [row for row in results if (not tool or row["tool"] == tool)
               and (row["id"].startswith(prefix) or row["short_id"] == prefix)]
    return matches[0] if len(matches) == 1 else None


def _print_search_detail(detail: dict) -> None:
    result = detail["result"]
    print(f"{result['tool']} {result['id']} — {result['title']}")
    print(result["resume_cmd"])
    for context in detail["contexts"]:
        print()
        for line in context["lines"]:
            marker = ">" if line["match"] else " "
            print(f"{marker} {line['role']}: {line['text']}")
