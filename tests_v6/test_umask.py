import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

from hubi6.config import Config
from hubi6.core.model import HubiError, State
from hubi6.services import App, launcher_umask, parse_umask
from hubi6.worker import session_umask
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_runtime import wait_for


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class UmaskTests(unittest.TestCase):
    def test_launcher_umask_is_parsed_from_proc_status(self):
        self.assertEqual(parse_umask("Name:\tpython3\nUmask:\t0002\nState:\tR (running)\n"), 0o002)
        self.assertEqual(parse_umask("Umask:\t0077\n"), 0o077)
        self.assertEqual(parse_umask("Umask:\t0000"), 0)
        self.assertEqual(parse_umask("Umask:\t0777"), 0o777)
        for bad in ("", "Name:\tpython3\n", "Umask:\t\n", "Umask:\t0o22\n", "Umask:\t0008\n", "Umask:\t1777\n",
                    "Umask:\t0002 0022\n", "Umask:\t-002\n", "Umask:\t٠٠٠٢\n"):
            with self.subTest(status=bad), self.assertRaises(HubiError):
                parse_umask(bad)

    def test_launcher_umask_is_read_without_changing_it(self):
        old = os.umask(0o027)
        try:
            self.assertEqual(launcher_umask(), 0o027)
            self.assertEqual(os.umask(0o027), 0o027)
        finally:
            os.umask(old)

    def test_unreadable_status_refuses_the_start(self):
        with patch("hubi6.services.open", create=True, side_effect=PermissionError):
            with self.assertRaises(HubiError):
                launcher_umask()

    def test_worker_accepts_only_a_plain_umask(self):
        for value in (0, 0o002, 0o022, 0o077, 0o777):
            self.assertEqual(session_umask({"argv": ["x"], "env": {}, "umask": value}), value)
        for payload in ({"argv": ["x"], "env": {}}, {"umask": None}, {"umask": True}, {"umask": False},
                        {"umask": -1}, {"umask": 0o1000}, {"umask": "0002"}, {"umask": 2.0}, ["umask", 2], None):
            with self.subTest(payload=payload), self.assertRaises(HubiError):
                session_umask(payload)


@unittest.skipUnless(LAB, "requires separate disposable UID; use tools/v6_lab.py")
class UmaskRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        shutil.rmtree(Path.home()/"repos"/("case-"+self._testMethodName), ignore_errors=True)
        self.path = repo("case-"+self._testMethodName)

    def tearDown(self):
        for w in self.app.snapshot().workloads:
            self.app.stop(w.record.id, repair=True)

    def launch(self, mask, kind):
        # The launcher's shell sets its umask, as a login shell over SSH does.
        p = subprocess.run(["/bin/sh", "-c", f'umask {mask:04o} && exec "$@"', "sh",
                            sys.executable, "-m", "hubi6", "start", kind, str(self.path)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return self.app.find(p.stdout.strip()).record

    def agent_file(self, r):
        marker = self.path/(".agent-"+str(self.app.find(r.id).session.pid))
        return wait_for(lambda: marker.exists() and marker)

    def terminal_files(self, r):
        self.app.tmux.command("send-keys", "-t", r.pane, "-l", "touch from-terminal && mkdir from-terminal-dir")
        self.app.tmux.command("send-keys", "-t", r.pane, "Enter")
        return wait_for(lambda: (self.path/"from-terminal-dir").exists() and
                        (self.path/"from-terminal", self.path/"from-terminal-dir"))

    def check(self, mask, file_mode, dir_mode):
        terminal, agent = self.launch(mask, "terminal"), self.launch(mask, "codex")
        created, directory = self.terminal_files(terminal)
        self.assertEqual(mode(created), file_mode)
        self.assertEqual(mode(directory), dir_mode)
        self.assertEqual(mode(self.agent_file(agent)), file_mode)
        # Hubi's own state stays private whatever the launcher's umask.
        for r in (terminal, agent):
            self.assertEqual(mode(self.app.store.path(r)), 0o600)
        self.assertFalse(list(self.app.c.runtime.glob("*.payload")))

    def test_session_gets_the_launcher_umask_0002(self):
        self.check(0o002, 0o664, 0o775)

    def test_session_gets_the_launcher_umask_0077(self):
        self.check(0o077, 0o600, 0o700)

    def test_in_process_start_uses_the_callers_umask(self):
        # The menu starts sessions in its own process through App.start.
        old = os.umask(0o027)
        try:
            agent = self.app.start("claude", str(self.path))
        finally:
            os.umask(old)
        self.assertEqual(mode(self.agent_file(agent)), 0o640)

    def test_invalid_umask_refuses_the_start(self):
        with patch("hubi6.services.launcher_umask", return_value=0o1000):
            with self.assertRaises(HubiError):
                self.app.start("codex", str(self.path))
        w, = self.app.snapshot().workloads
        self.assertEqual(w.state, State.EXITED)
        self.assertEqual(w.session.exit_status, 1)
        self.assertFalse(w.record.invocation)
        self.assertFalse(list(self.path.glob(".agent-*")))
        self.assertFalse(list(self.app.c.runtime.glob("*.payload")))


if __name__ == "__main__":
    unittest.main()
