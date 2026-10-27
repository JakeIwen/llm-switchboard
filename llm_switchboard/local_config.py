"""This install's machine names, read from the checkout's untracked local.env.

The file holds KEY=value lines (no spaces; `#` comments), so setup-mac.sh can
source it too. An LLM_SWITCHBOARD_* environment variable overrides its line.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

from .paths import LOCAL_ENV

PI_SSH = "LLM_SWITCHBOARD_PI_SSH"
MAC_NAME = "LLM_SWITCHBOARD_MAC_NAME"
PAGE_ORIGINS = "LLM_SWITCHBOARD_PAGE_ORIGINS"
DEFAULTS = {PI_SSH: "pi", MAC_NAME: "mac", PAGE_ORIGINS: ""}


def load(path: Path = LOCAL_ENV, environ=os.environ) -> dict[str, str]:
    values = dict(DEFAULTS)
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            values[key.strip()] = value.strip().strip("\"'")
    values.update((k, v) for k, v in environ.items()
                  if k.startswith("LLM_SWITCHBOARD_") and (v or k == PAGE_ORIGINS))
    return values


def page_origins(values: dict[str, str]) -> frozenset[str]:
    """Only explicit HTTP(S) origins, never a wildcard, URL path or credentials."""
    origins = frozenset(part.strip() for part in values.get(PAGE_ORIGINS, "").split(",") if part.strip())
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or any(char.isspace() for char in origin) or "*" in origin):
            raise ValueError(f"{PAGE_ORIGINS} must contain full HTTP(S) origins without paths")
        # Accessing port rejects malformed/out-of-range ports before starting a listener.
        parsed.port
    return origins
