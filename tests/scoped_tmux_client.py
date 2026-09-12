#!/usr/bin/env python3
"""Run a tmux client and its waiting shell inside one disposable user scope."""

import os
import pty
import select
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: scoped_tmux_client.py SCOPE SOCKET SESSION", file=sys.stderr)
        return 2
    scope, socket, session = sys.argv[1:]
    pid, fd = pty.fork()
    if pid == 0:
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.setdefault("TERM", "xterm-256color")
        command = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={scope}",
            "--",
            "bash",
            "-c",
            'tail -f /dev/null | tmux -C -N -S "$1" attach-session -t "=$2"',
            "_",
            socket,
            session,
        ]
        os.execvpe(command[0], command, environment)

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                except OSError:
                    break
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                return os.waitstatus_to_exitcode(status)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
