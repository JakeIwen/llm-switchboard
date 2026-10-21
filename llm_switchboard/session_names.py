"""Pure session-title validation and comparison, shared by local and host actions."""

MAX_TITLE_LEN = 120


def clean_title(title) -> str:
    if not isinstance(title, str) or not " ".join(title.split()):
        raise ValueError("a name is required")
    title = " ".join(title.split())
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"the name must be at most {MAX_TITLE_LEN} characters")
    return title


def name_key(title: str) -> str:
    return " ".join(title.split()).casefold()

