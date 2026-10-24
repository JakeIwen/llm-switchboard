"""Install-wide picker settings, validated before locked, atomic updates."""

from copy import deepcopy
import logging
from pathlib import Path
import re
from typing import TypedDict

from .json_file import JsonFile
from .paths import SETTINGS_FILE


class FolderSettings(TypedDict):
    depth: int
    min_sessions: int
    hidden: list[str]


class Settings(TypedDict):
    claude_command: str
    codex_command: str
    folders: FolderSettings
    poll_seconds: int


DEFAULTS: Settings = {
    "claude_command": "claude",
    "codex_command": "codex",
    "folders": {"depth": 2, "min_sessions": 2, "hidden": []},
    "poll_seconds": 4,
}
_COMMAND = re.compile(r"[A-Za-z0-9_./-]{1,64}")
_HIDDEN = re.compile(r"~(/[^\s]+)?")
_RANGES = {"folders.depth": (0, 6), "folders.min_sessions": (1, 50), "poll_seconds": (2, 60)}
log = logging.getLogger(__name__)


def defaults() -> Settings:
    """Return an independent copy, including the nested folder list."""
    return deepcopy(DEFAULTS)


def _validate_value(key: str, value):
    if key in _RANGES:
        low, high = _RANGES[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    elif key == "folders.hidden":
        if (not isinstance(value, list) or len(value) > 50
                or any(not isinstance(item, str) or not _HIDDEN.fullmatch(item) for item in value)):
            raise ValueError(f"{key} must contain at most 50 home-relative paths (~ or ~/folder), without whitespace")
    elif not isinstance(value, str) or not _COMMAND.fullmatch(value):
        raise ValueError(f"{key} must be one shell word (1–64 letters, digits, _, ., / or -)")
    return deepcopy(value)


def validate(partial: dict) -> dict:
    """Validate a partial update without filling missing keys or changing it."""
    return _clean(partial, DEFAULTS, strict=True)


def _clean(raw, template: dict, *, strict: bool, prefix: str = "") -> dict:
    if not isinstance(raw, dict):
        message = f"{prefix or 'settings'} must be an object"
        if strict:
            raise ValueError(message)
        log.warning("%s; using defaults", message)
        return {}
    clean = {}
    for key, value in raw.items():
        name = f"{prefix}.{key}" if prefix else key
        if key not in template:
            if strict:
                raise ValueError(f"unknown settings key: {name}")
            continue
        if isinstance(template[key], dict):
            clean[key] = _clean(value, template[key], strict=strict, prefix=name)
            continue
        try:
            clean[key] = _validate_value(name, value)
        except ValueError as exc:
            if strict:
                raise
            log.warning("%s; using default", exc)
    return clean


def load(path: Path | None = None) -> Settings:
    file = JsonFile(path if path is not None else SETTINGS_FILE)
    try:
        raw = file.load()
    except (OSError, ValueError) as exc:
        log.warning("Cannot read settings from %s: %s; using defaults", file.path, exc)
        raw = {}
    values = defaults()
    _merge(values, _clean(raw, DEFAULTS, strict=False))
    return values


def _merge(values: dict, partial: dict) -> None:
    for key, value in partial.items():
        if isinstance(value, dict):
            values[key].update(value)
        else:
            values[key] = value


def save(partial: dict, path: Path | None = None) -> Settings:
    clean = validate(partial)
    file = JsonFile(path if path is not None else SETTINGS_FILE)
    with file.locked():
        values = load(file.path)
        _merge(values, clean)
        file.write(values)
    return values


def cli_update(key: str, text: str) -> dict:
    """Parse one dotted CLI key using the canonical defaults for its value type."""
    parts = key.split(".")
    template = DEFAULTS
    for part in parts:
        if not isinstance(template, dict) or part not in template:
            raise ValueError(f"unknown settings key: {key}")
        template = template[part]
    if isinstance(template, dict):
        raise ValueError(f"{key} needs a nested key")
    if type(template) is int:
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
    elif isinstance(template, list):
        value = [item.strip() for item in text.split(",") if item.strip()]
    else:
        value = text
    for part in reversed(parts):
        value = {part: value}
    return validate(value)
