"""Where the desk keeps its files and sockets, and which launch it trusts.

One `DeskConfig` is built at each entry point (hub, channel server, operator
CLI) from HOME and the environment, so tests point every part at a temporary
home by passing `home=` and `environ=`. Standard library only: the channel
server imports this on every Claude launch.
"""

from dataclasses import dataclass
import os
from pathlib import Path

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
    # 0700 directory holding the two 0600 Unix sockets (under config_dir).
    runtime_dir: Path
    port: int
    # Claude Code's ~/.claude/sessions/<pid>.json registry.
    claude_sessions_dir: Path
    # Where the native installer keeps claude's executables, one file per
    # version (`~/.local/bin/claude` links to one): the startup
    # acknowledgement requires the foreground process to run one of them.
    claude_versions_dir: Path
    proc_dir: Path = Path("/proc")

    @classmethod
    def for_launch(cls, environ=None, home: Path | None = None) -> "DeskConfig":
        """A kept-session launch needs only config paths. Don't let a bad hub
        setting (e.g. its port) break an otherwise ordinary Claude launch."""
        environ = os.environ if environ is None else environ
        paths = {key: environ[key] for key in ("HOME", "XDG_CONFIG_HOME") if key in environ}
        return cls.from_env(paths, home)

    @classmethod
    def enrolled_file_from_env(cls, environ=None, home: Path | None = None) -> Path:
        return cls.for_launch(environ, home).enrolled_file

    @classmethod
    def from_env(cls, environ=None, home: Path | None = None, repo: Path = REPO) -> "DeskConfig":
        environ = os.environ if environ is None else environ
        home = Path(home or environ.get("HOME") or Path.home())
        config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
        state_home = Path(environ.get("XDG_STATE_HOME") or home / ".local" / "state")
        state_dir = state_home / "llm-switchboard" / "desk"
        config_dir = config_home / "llm-switchboard" / "desk"
        # The sockets sit beside the policy files they guard, so every process
        # that reads one enrollment and settings store finds the same hub (an
        # XDG_RUNTIME_DIR present for the service but not for a shell would
        # otherwise make the CLI see no hub and edit the files behind it).
        runtime_dir = config_dir / "run"
        return cls(
            home=home,
            repo=Path(repo),
            config_dir=config_dir,
            state_dir=state_dir,
            runtime_dir=runtime_dir,
            port=int(environ.get(PORT_ENV) or DEFAULT_DESK_PORT),
            claude_sessions_dir=home / ".claude" / "sessions",
            claude_versions_dir=home / ".local" / "share" / "claude" / "versions",
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

    # -- the Mac link (docs/voice-desk.md, "Mac link") ------------------------------

    @property
    def link_socket(self) -> Path:
        """Pi: the hub's third socket, where the forced command's pump connects."""
        return self.runtime_dir / "link.sock"

    @property
    def link_argv(self) -> tuple[str, ...]:
        """Pi: the `authorized_keys` forced command; moving bin/desk-link breaks the link."""
        return self._checkout_python("desk-link")

    @property
    def link_signers_file(self) -> Path:
        """Pi: the allowed-signers list link proofs are verified against."""
        return self.config_dir / "link-signers"

    @property
    def remote_enrolled_file(self) -> Path:
        """Pi: enrollment of Mac sessions, beside phase 1's enrolled.json."""
        return self.config_dir / "enrolled-hosts.json"

    @property
    def hub_run_lock(self) -> Path:
        """Pi: held by `desk serve` for its life; probed by the offline decisions."""
        return self.runtime_dir / "hub.lock"

    @property
    def relay_argv(self) -> tuple[str, ...]:
        """Mac: the relay's launchd ProgramArguments; moving bin/desk-relay breaks the link."""
        return self._checkout_python("desk-relay")

    @property
    def relay_lock(self) -> Path:
        """Mac: held by the relay for its life, so only one runs."""
        return self.runtime_dir / "relay.lock"

    def _checkout_python(self, script: str) -> tuple[str, ...]:
        return (str(self.repo / ".venv" / "bin" / "python"), "-I", "-B", str(self.repo / "bin" / script))

    @property
    def launch_flags(self) -> tuple[str, ...]:
        """What an enrolled session's claude is started with."""
        return ("--mcp-config", str(self.mcp_config), "--dangerously-load-development-channels", "server:desk")

