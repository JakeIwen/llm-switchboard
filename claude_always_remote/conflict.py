"""The error every session action raises when the session's current state
doesn't allow it right now (the web API answers 409)."""


class Conflict(Exception):
    """The session's current state doesn't allow this right now."""
