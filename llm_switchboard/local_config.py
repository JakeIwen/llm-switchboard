"""This install's machine names, read from the checkout's untracked local.env.

The file holds KEY=value lines (no spaces; `#` comments), so setup-mac.sh can
source it too. An LLM_SWITCHBOARD_* environment variable overrides its line.
"""

import os
from pathlib import Path

from .paths import LOCAL_ENV

PI_SSH = "LLM_SWITCHBOARD_PI_SSH"
MAC_NAME = "LLM_SWITCHBOARD_MAC_NAME"
DEFAULTS = {PI_SSH: "pi", MAC_NAME: "mac"}


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
    values.update((k, v) for k, v in environ.items() if k.startswith("LLM_SWITCHBOARD_") and v)
    return values
