"""Where the desk keeps its files and sockets, and which launch it trusts.

One `DeskConfig` is built at each entry point (hub, channel server, operator
CLI) from HOME and the environment, so tests point every part at a temporary
home by passing `home=` and `environ=`. Standard library only: the channel
server imports this on every Claude launch.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import pwd

DEFAULT_DESK_PORT = 8796
PORT_ENV = "LLM_SWITCHBOARD_DESK_PORT"
REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DeskConfig:
    home: Path
    repo: Path
    # Enrollment, desk settings, the pinned MCP config and the TLS leaf.
    config_dir: Path
    # The target lock file (a bounded SD writer: see docs/voice-desk.md).
    state_dir: Path
    # 0700 directory holding the two 0600 Unix sockets.
    runtime_dir: Path
    port: int
    # Claude Code's ~/.claude/sessions/<pid>.json registry.
    claude_sessions_dir: Path
    proc_dir: Path = Path("/proc")

    @staticmethod
    def enrolled_file_from_env(environ=None, home: Path | None = None) -> Path:
        """The enrollment list's path alone, for readers (the picker) that
        must not fail on an unrelated desk setting such as a bad port."""
        environ = os.environ if environ is None else environ
        home = Path(home or environ.get("HOME") or Path.home())
        config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
        return config_home / "llm-switchboard" / "desk" / "enrolled.json"

    @classmethod
    def from_env(cls, environ=None, home: Path | None = None, repo: Path = REPO) -> "DeskConfig":
        environ = os.environ if environ is None else environ
        home = Path(home or environ.get("HOME") or Path.home())
        config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
        state_home = Path(environ.get("XDG_STATE_HOME") or home / ".local" / "state")
        state_dir = state_home / "llm-switchboard" / "desk"
        runtime = environ.get("XDG_RUNTIME_DIR")
        # A shared XDG_RUNTIME_DIR would let a test instance reach the real
        # hub's sockets, so any HOME other than the account's own (from the
        # password database: Path.home() just follows $HOME) keeps them under
        # its own tree.
        runtime_dir = (Path(runtime) / "llm-switchboard-desk"
                       if runtime and home == _account_home() else state_dir / "run")
        return cls(
            home=home,
            repo=Path(repo),
            config_dir=config_home / "llm-switchboard" / "desk",  # enrolled_file_from_env mirrors this
            state_dir=state_dir,
            runtime_dir=runtime_dir,
            port=int(environ.get(PORT_ENV) or DEFAULT_DESK_PORT),
            claude_sessions_dir=home / ".claude" / "sessions",
        )

    # The only Host header and Origin the hub answers.
    @property
    def host_header(self) -> str:
        return f"localhost:{self.port}"

    @property
    def origin(self) -> str:
        return f"https://localhost:{self.port}"

    @property
    def channel_socket(self) -> Path:
        return self.runtime_dir / "channel.sock"

    @property
    def operator_socket(self) -> Path:
        return self.runtime_dir / "operator.sock"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "locks.json"

    @property
    def enrolled_file(self) -> Path:
        return self.config_dir / "enrolled.json"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "desk-settings.json"

    @property
    def mcp_config(self) -> Path:
        """Pinned: the hub requires claude's command line to carry
        `--mcp-config <this path>` before it trusts a channel child."""
        return self.config_dir / "mcp.json"

    @property
    def tls_cert(self) -> Path:
        return self.config_dir / "tls" / "leaf.pem"

    @property
    def tls_key(self) -> Path:
        return self.config_dir / "tls" / "leaf.key"

    @property
    def channel_argv(self) -> tuple[str, ...]:
        """The exact argv of a trusted channel server (the peer check compares
        /proc/<pid>/cmdline with it)."""
        return (str(self.repo / ".venv" / "bin" / "python"), "-I", "-B", str(self.repo / "bin" / "desk-channel"))

    @property
    def launch_flags(self) -> tuple[str, ...]:
        """What an enrolled session's claude is started with."""
        return ("--mcp-config", str(self.mcp_config), "--dangerously-load-development-channels", "server:desk")


def _account_home() -> Path | None:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        return None
