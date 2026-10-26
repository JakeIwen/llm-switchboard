"""Picker response schemas. Runtime payloads retain their original JSON encoding."""

from typing import Literal

from pydantic import Field, create_model

from .. import settings
from ..codex_threads import CodexState
from ..host_actions_protocol import ActionKind, ActionState
from ..host_protocol import SessionState
from ..folders import Folder
from .records import ResponseRecord, http_type, record_model
from ..search_records import SearchDetail, SearchResult, SearchStatus
from ..supervisor_state import Exit, Status

SettingsRecord = record_model(settings.Settings)
ExitRecord = create_model("ExitSummary", __base__=ResponseRecord,
                          **{k: (http_type(v), ...) for k, v in Exit.__annotations__.items() if k != "screen"})
StatusRecord = create_model("SessionStatus", __base__=ResponseRecord,
                            **{k: (ExitRecord | None if k == "last_exit" else http_type(v), ...)
                               for k, v in Status.__annotations__.items()})


class ErrorResponse(ResponseRecord):
    error: str


class SettingsResponse(ResponseRecord):
    settings: SettingsRecord
    defaults: SettingsRecord


class OpenProcess(ResponseRecord):
    pid: int | None = None
    status: str


class SessionRow(ResponseRecord):
    id: str
    tool: Literal["claude", "codex"]
    host: str
    short_id: str
    title: str
    first_prompt: str = ""
    cwd: str
    project: str
    mtime: float | None
    size: int | None
    remote_url: str
    resume_cmd: str
    # A headless run (`claude -p`, `codex exec`, another app on Codex's API): nobody typed in it.
    headless: bool = False
    new: bool
    enabled: bool
    rc_name: str
    open_elsewhere: list[OpenProcess]
    status: StatusRecord | None
    # Codex's experimental API may add states; the catalog passes them through.
    codex_state: str | None = Field(default=None, examples=[CodexState.NOT_LOADED, CodexState.IDLE,
                                                          CodexState.ACTIVE, CodexState.SYSTEM_ERROR])
    # These fields exist only on the metadata rows pushed by another machine.
    session_id: str | None = None
    host_online: bool | None = None
    last_seen: float | None = None
    state: SessionState | None = None
    mutable: bool | None = None
    deleted_at: float | None = None


class HostStatus(ResponseRecord):
    host: str
    online: bool
    last_seen: float


class QueuedAction(ResponseRecord):
    id: str
    session_id: str
    tool: Literal["claude", "codex"]
    action: ActionKind
    body: dict
    cwd: str
    title: str
    queued_at: float
    state: ActionState
    message: str
    host: str


class CodexStatus(ResponseRecord):
    enabled: bool
    error: str


class SessionsResponse(ResponseRecord):
    host: str
    now: float
    sessions: list[SessionRow]
    notifications: bool
    codex: CodexStatus
    hosts: list[HostStatus]
    host_actions: list[QueuedAction]
    settings: SettingsRecord


class FoldersResponse(ResponseRecord):
    folders: list[record_model(Folder)]


class TrashItem(ResponseRecord):
    id: str
    tool: Literal["claude", "codex"]
    short_id: str
    title: str
    project: str
    deleted_at: float


class TrashResponse(ResponseRecord):
    trash: list[TrashItem | SessionRow]


class Message(ResponseRecord):
    role: Literal["user", "assistant"]
    text: str
    at: str | float | None


class MessagesResponse(ResponseRecord):
    messages: list[Message]


class ScreenResponse(ResponseRecord):
    source: Literal["live", "last-exit", "none"]
    screen: str
    exit: int | None = None


class OkResponse(ResponseRecord):
    ok: bool


class EmptyTrashResponse(OkResponse):
    removed: int


class ToggleResponse(OkResponse):
    enabled: bool
    rc_name: str


class RenameResponse(OkResponse):
    title: str
    via: Literal["transcript", "running session", "codex"]


class DeleteResponse(OkResponse):
    title: str
    trashed: bool


class RestoreResponse(OkResponse):
    path: str | None = None


class MoveResponse(OkResponse):
    cwd: str
    moved: bool


class CreateResponse(OkResponse):
    id: str
    title: str
    cwd: str | None = None
    project: str | None = None


class ForkResponse(OkResponse):
    id: str
    title: str
    cwd: str


class RemoteActionResponse(OkResponse):
    action_id: str
    state: ActionState


class SearchStatusResponse(ResponseRecord):
    index: record_model(SearchStatus)


class SearchResponse(SearchStatusResponse):
    query: str
    results: list[record_model(SearchResult)]


class SearchDetailResponse(SearchStatusResponse):
    query: str
    detail: record_model(SearchDetail)
