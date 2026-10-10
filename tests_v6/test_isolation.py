import os
from pathlib import Path
import socket
import unittest

from tests_v6.support import LAB


@unittest.skipUnless(LAB, "kernel probes require disposable UID")
class IsolationTests(unittest.TestCase):
    def test_owner_signals_sockets_and_cgroups_denied(self):
        self.assertNotIn(os.getuid(), (0, 1000))
        protected = 0
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                if p.stat().st_uid != 1000:
                    continue
                with self.assertRaises(PermissionError):
                    os.kill(int(p.name), 0)  # permission probe, sends no signal
                protected += 1
            except (FileNotFoundError, ProcessLookupError):
                pass
        self.assertGreater(protected, 10)
        for path in ("/run/user/1000/bus", "/run/user/1000/tmux-1000/hubi"):
            with socket.socket(socket.AF_UNIX) as s, self.assertRaises(PermissionError):
                s.connect(path)
        for p in Path("/sys/fs/cgroup/user.slice/user-1000.slice").rglob("cgroup.procs"):
            self.assertFalse(os.access(p, os.W_OK))
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                self.assertEqual(int(line.split()[1], 16), 0)
        print(f"Isolation: {protected} owner PIDs deny signals; sockets/cgroup writes denied", flush=True)
