#!/usr/bin/env python3
"""Read-only fingerprint of the owner's v5 runtime. Never invokes Hubi."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def capture():
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    def run(argv):
        p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
        if p.returncode:
            raise RuntimeError(p.stderr)
        return p.stdout.strip()
    units = run(["systemctl", "--user", "show", "hubi-tmux.service", "hubi-*.scope",
                 "--property=Id,InvocationID,MainPID,NRestarts,ActiveState,ControlGroup"])
    socket = Path(f"/run/user/{os.getuid()}/tmux-{os.getuid()}/hubi")
    sessions = run(["tmux", "-N", "-S", str(socket), "list-panes", "-a", "-F",
                    "#{session_id}\t#{session_name}\t#{session_created}\t#{pane_id}\t#{pane_pid}"])
    installed = {}
    for p in (Path.home()/".local/bin/hubi", Path.home()/".config/hubi/tmux-server.conf",
              Path.home()/".config/systemd/user/hubi-tmux.service"):
        installed[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"units": sorted(units.split("\n\n")), "panes": sorted(sessions.splitlines()),
            "socket_inode": socket.stat().st_ino, "installed": installed}


if __name__ == "__main__":
    data = capture()
    if len(sys.argv) == 2:
        before = json.loads(Path(sys.argv[1]).read_text())
        print(json.dumps({"identical": before == data, "before": before, "after": data}, indent=2))
        sys.exit(0 if before == data else 1)
    print(json.dumps(data, indent=2))
