# Backend organization and verification

The package remains one checkout-run Python 3.11+ application shared by Pi and
Mac. The October 2026 backend pass separates two existing pure contracts:

- `session_names.py` owns title cleanup, comparison keys and the title-length
  limit. `names.py` coordinates uniqueness against sources and retains its
  existing public imports. Host validators use the pure owner directly.
- `supervisor_state.py` owns the existing status records, backoff constants and
  screen observation decision. `supervisor.py` retains public compatibility
  imports and all ordered effects/locking. `catalog.py` reads the record owner.

No service path, protocol, CLI output, UI asset or runtime dependency moved.
The ownership and Contracts tables in `AGENTS.md` are the navigation map.

## Verification entry point

Run `python3 -m unittest discover -s tests -f` with Python 3.11+ and Node.
It includes Python behavior tests, the 24 Node tests, enforced architecture
contracts, and the 54 repo-owned checker fixture/tree tests. The nested checker
suite is bounded at 180 seconds; it takes roughly two minutes on the Pi.

External search-script comparisons run when those scripts are installed.
When isolating HOME on this Pi, preserve their location explicitly with
`SESSION_SEARCH_SCRIPTS=/home/pi/scripts`; they still read only generated
transcript fixtures. Every service fixture must have a private HOME, tmux
socket, loopback port and disabled/fake Codex connection. No test is permission
to operate a real session.

UI changes additionally use the `browser_clean` MCP driver and the existing
browser fixture/behavior files. This pass leaves every static file byte-identical,
so it does not introduce a browser gate or a visual-guide change.

## Preservation captures

The reviewed baseline is `1e7a2bbc36ca16766fb8ef09467e0cbe235ad871`.
Run the same capture file with the target checkout on PYTHONPATH. For example,
from the candidate checkout:

```
PYTHONPATH=/path/to/baseline python3 tests/name_contract_oracle.py
PYTHONPATH=/path/to/candidate python3 tests/name_contract_oracle.py
PYTHONPATH=/path/to/baseline python3 tests/supervisor_oracle.py
PYTHONPATH=/path/to/candidate python3 tests/supervisor_oracle.py
```

Supply separate temporary HOME/socket settings and disabled notifications/Codex
in both environments. Require exit 0 before comparing stdout and stderr.
The naming capture asserts 20 successes/refusals. The supervisor capture asserts
nine scenarios through `reconcile()`/`snapshot()`, records ordered effects,
picker rows and real temporary store bytes, and fixes its store clock at
1700000000 and supervisor clock initially at 10000. No output is normalized.
Their expected behavior also runs in the main suite.

The campaign additionally compared both module and checkout-wrapper help for
root/serve/host-exchange/host-authorize/host-agent/host-install/search/index,
invalid command failures, valid/version-invalid/malformed SSH exchanges, stored
metadata JSON, the Linux host-install refusal, and generated launchd plist bytes.
Each case asserted its expected status/output before equality. These are Pi
checks; generated Mac configuration is not a Mac runtime pass.

## Lasting checks and limits

`python3 scripts/check_layout.py` reads source without importing project code.
All generic findings remain advisory because this repo has no layout baseline.
No baseline was added, and no exception suppresses a finding. The focused
architecture tests independently enforce the pure module dependencies, Mac/Pi
runtime separation, rendered systemd target, checkout wrapper and launchd argv.
The runtime cannot import the checker scripts under the declared layout contract.

The generic checker currently misreads `@REPO@/bin/llm-switchboard` in the unit
source template as a literal missing file. The focused rendered-unit test covers
that gap. Prefix findings for codex/host/mac/search/session siblings remain review
candidates: these families include distinct model, adapter, entry and runtime
roles and established public imports. Grouping them is a separate migration.
The existing 103-line HTTP handler factory and unchanged frontend review triggers
are deferred; no large-module split or CSS cleanup is needed for these boundaries.

Jacob reserved baseline changes. Only after separate review, and after resolving
the checker's template gap, the optional commands are:

```
python3 ~/.claude/skills/consolidate/drift_check.py rebaseline
python3 scripts/check_layout.py rebaseline
```

Neither command was run by this campaign. The checker is copied into the repo;
running it and its tests requires no installed skill folder.

## Activation and rollback

The Pi service executes this checkout through `bin/llm-switchboard`; its user
unit and installer remain unchanged. After verified runtime changes, use
`systemctl --user restart llm-switchboard`, then verify a different MainPID,
enabled/active state, HTTP `/`, `/api/sessions`, `/api/folders`, and retained
managed pane/process identities. There is no dedicated health endpoint.
Do not stop/uninstall for a routine restart. Linger provides boot registration.

The original Git commit above remains recoverable. A rollback must first inspect
current Git ownership, then restore the complete known-good cohort in a separate
reviewed checkout or revert this campaign's commits without discarding newer
work. Run that tree's whole suite before activating it through the documented
installer/restart path. No data format migration or index rebuild is involved.

The Mac uses the same package but its own checkout and launchd job. The Mac owner
must inspect its Git state and installed plist/interpreter, integrate the pushed
commits without absorbing other work, run its full suite and captures, and update
and verify its existing host-agent process. Pi verification never substitutes for
that host-specific check. Preserve the outward-only metadata/action protocol.
