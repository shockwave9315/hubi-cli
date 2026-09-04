#!/usr/bin/env python3
"""Attach a real pseudo-terminal tmux client and type one line."""

import os
import pty
import signal
import sys
import time


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] not in {"readonly", "write"}:
        print("usage: tmux_client.py MODE SOCKET SESSION TEXT", file=sys.stderr)
        return 2

    mode, socket, session, payload = sys.argv[1:]
    pid, fd = pty.fork()
    if pid == 0:
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.setdefault("TERM", "xterm-256color")
        command = ["tmux", "-L", socket, "attach-session"]
        if mode == "readonly":
            command.append("-r")
        command.extend(["-t", session])
        os.execvpe(command[0], command, environment)

    try:
        time.sleep(0.35)
        os.write(fd, payload.encode() + b"\n")
        time.sleep(0.35)
    finally:
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
