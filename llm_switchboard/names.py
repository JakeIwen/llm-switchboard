"""Session names: cleaning them up, and keeping them unique per folder.

Neither Claude Code nor Codex stops two sessions in one folder from sharing a
name, so every rename, new session, move and fork here checks first. A name
is compared without regard to case or runs of spaces.
"""

from .conflict import Conflict
from .transcripts import display_path

# Public compatibility imports; pure validation belongs to session_names.
from .session_names import MAX_TITLE_LEN, clean_title, name_key


class Names:
    """Every session's (id, folder, name), gathered from each source the picker shows."""

    def __init__(self, *sources):
        # Each source is a callable returning (id, cwd, title) tuples.
        self.sources = list(sources)

    def add_source(self, source) -> None:
        self.sources.append(source)

    def refuse_duplicate(self, cwd: str, title: str, exclude: str | None = None,
                         hint: str = "pick another name") -> None:
        key = name_key(title)
        for source in self.sources:
            for other, other_cwd, other_title in source():
                if other != exclude and other_cwd == cwd and name_key(other_title) == key:
                    raise Conflict(f"{display_path(cwd)} already has a session named “{other_title}”; {hint}.")


def claude_names(index, store):
    """Claude Code sessions on disk, plus ones created here and not yet written."""
    def source():
        yield from ((t.session_id, t.cwd, t.title) for t in index.all())
        yield from ((e.session_id, e.cwd, e.title) for e in store.load().values()
                    if e.cwd and index.get(e.session_id) is None)
    return source
