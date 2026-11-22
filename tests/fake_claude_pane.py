"""A pane program that shows Claude Code's development-channel warning.

Usage: fake_claude_pane.py <state dir> [claude-style arguments...]

Writes its pid to <state dir>/pid, then draws the warning from
tests/screens/desk-channel-warning.txt with option 1 selected. Its terminal
is raw, so every byte typed into the pane is appended to <state dir>/typed
exactly as received. A carriage return (Enter) on option 1 replaces the
screen with "accepted"; SIGUSR1 moves the selection to the other option, as
an arrow key would in Claude, and SIGUSR2 redraws the warning with a longer
rule (still the warning, but another screen), both without typing anything.
"""

import os
from pathlib import Path
import signal
import sys
import termios
import tty

WARNING = (Path(__file__).parent / "screens" / "desk-channel-warning.txt").read_text()
state_dir = Path(sys.argv[1])
selected = [1]
accepted = [False]
wide = [False]


def draw() -> None:
    if accepted[0]:
        screen = "accepted\n"
    elif selected[0] == 1:
        screen = WARNING
    else:
        screen = WARNING.replace("❯ 1.", "  1.").replace("    2. Exit", "  ❯ 2. Exit")
    if wide[0] and not accepted[0]:
        screen = "──────────" + screen
    sys.stdout.write("\x1b[2J\x1b[H" + screen.replace("\n", "\r\n"))
    sys.stdout.flush()


def toggle(*_) -> None:
    selected[0] = 2 if selected[0] == 1 else 1
    draw()


def widen(*_) -> None:
    wide[0] = True
    draw()


def main() -> None:
    (state_dir / "pid").write_text(str(os.getpid()))
    fd = sys.stdin.fileno()
    tty.setraw(fd, termios.TCSANOW)
    signal.signal(signal.SIGUSR1, toggle)
    signal.signal(signal.SIGUSR2, widen)
    draw()
    with open(state_dir / "typed", "ab", buffering=0) as typed:
        while True:
            try:
                data = os.read(fd, 1024)
            except InterruptedError:
                continue
            if not data:
                return
            typed.write(data)
            if b"\r" in data and selected[0] == 1 and not accepted[0]:
                accepted[0] = True
                draw()


if __name__ == "__main__":
    main()
