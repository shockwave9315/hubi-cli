"""`hubi send`: deliver text to a session by name. Logic on fakes; delivery only under the lab UID."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from hubi6.adapters.tmux import Tmux
from hubi6.config import Config
from hubi6.core.model import HubiError, State, Unavailable
from hubi6.services import App
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_menu import FakeGit, snapshot, workload
from tests_v6.test_runtime import wait_for


class Recorder:
    """A Runner that records tmux calls and can fail one subcommand."""
    calls = 0

    def __init__(self, fail=None):
        self.log, self.fail = [], fail

    def run(self, argv, *, input=None, check=True, **kw):
        self.log.append((argv[4], list(argv[4:]), input))
        if argv[4] == self.fail and check:
            raise HubiError("tmux failed (exit 1)")
        return subprocess.CompletedProcess(argv, 0, "", "")


class SendTests(unittest.TestCase):
    def app(self, state=State.RUNNING):
        app = App(replace(Config.load(), send_enter_delay=0), Recorder())
        app.git = FakeGit()
        w = workload("claude", "primary", "praca", state, created=time.time())
        w.session.pane = w.record.pane = "%14"
        s = snapshot([w])
        app.runtime_snapshot = lambda: s
        return app

    def commands(self, app):
        return [c[0] for c in app.r.log]

    def test_paste_then_enter_into_the_resolved_pane(self):
        app = self.app()
        app.send("claude", "praca", "primary", "CP17 wznów\nlinia 2")
        self.assertEqual(self.commands(app), ["load-buffer", "paste-buffer", "send-keys"])
        load, paste, enter = app.r.log
        self.assertEqual(load[2], "CP17 wznów\nlinia 2")              # stdin, never argv
        self.assertNotIn("CP17 wznów\nlinia 2", load[1])
        buffer = load[1][2]
        self.assertTrue(buffer.startswith("hubi6-send-"))
        self.assertEqual(paste[1], ["paste-buffer", "-p", "-d", "-b", buffer, "-t", "%14"])
        self.assertEqual(enter[1], ["send-keys", "-t", "%14", "Enter"])

    def test_no_enter_only_pastes(self):
        app = self.app()
        app.send("claude", "praca", "primary", "draft", enter=False)
        self.assertEqual(self.commands(app), ["load-buffer", "paste-buffer"])

    def test_carriage_returns_become_newlines(self):
        app = self.app()
        app.send("claude", "praca", "primary", "a\r\nb\rc", enter=False)
        self.assertEqual(app.r.log[0][2], "a\nb\nc")

    def test_unsafe_or_empty_text_is_refused_before_tmux(self):
        for text, why in (("", "empty"), ("  \n\t", "empty"), ("a\x1b[201~b", "control"), ("a\x00", "control"),
                          ("a\x7f", "control"), ("a\x9b", "control"), ("x"*65537, "64 KiB")):
            with self.subTest(text=text[:10]):
                app = self.app()
                with self.assertRaisesRegex(HubiError, why):
                    app.send("claude", "praca", "primary", text)
                self.assertEqual(app.r.log, [])

    def test_session_that_is_not_running_gets_nothing(self):
        for state in (State.EXITED, State.ORPHANED):
            with self.subTest(state=state):
                app = self.app(state)
                with self.assertRaises(Unavailable):
                    app.send("claude", "praca", "primary", "hello")
                self.assertEqual(app.r.log, [])

    def test_failed_paste_deletes_the_buffer(self):
        runner = Recorder(fail="paste-buffer")
        with self.assertRaises(HubiError):
            Tmux(Config.load(), runner).paste("%14", "text")
        self.assertEqual([c[0] for c in runner.log], ["load-buffer", "paste-buffer", "delete-buffer"])
        self.assertEqual(runner.log[0][1][2], runner.log[2][1][2])

    def test_paste_refuses_a_target_that_is_not_a_pane_id(self):
        runner = Recorder()
        for target in ("praca", "=session:", "%1;kill-server", ""):
            with self.subTest(target=target), self.assertRaises(HubiError):
                Tmux(Config.load(), runner).paste(target, "text")
        self.assertEqual(runner.log, [])


class SendCommandLineTests(unittest.TestCase):
    def run_cli(self, *argv, stdin="", tty=False, error=None):
        calls = []

        def send(app, kind, repo, instance, text, *, enter=True):
            calls.append((kind, repo, instance, text, enter))
            if error:
                raise error

        stream = io.StringIO(stdin)
        stream.isatty = lambda: tty
        out, err = io.StringIO(), io.StringIO()
        with patch.object(App, "send", send), patch.object(Config, "prepare", side_effect=AssertionError("prepare")), \
                patch("sys.stdin", stream), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            from hubi6.cli import main
            code = main(list(argv))
        return code, calls, err.getvalue()

    def test_message_option_and_polish_default_name(self):
        code, calls, _ = self.run_cli("send", "claude", "praca", "główna", "-m", "CP17 wznów")
        self.assertEqual((code, calls), (0, [("claude", "praca", "primary", "CP17 wznów", True)]))

    def test_standard_input_and_no_enter(self):
        code, calls, _ = self.run_cli("send", "codex", "praca", "--no-enter", stdin="raport\nlinia 2\n")
        self.assertEqual((code, calls), (0, [("codex", "praca", "primary", "raport\nlinia 2\n", False)]))

    def test_missing_message_on_a_terminal_is_an_error(self):
        code, calls, err = self.run_cli("send", "claude", "praca", tty=True)
        self.assertEqual((code, calls), (2, []))
        self.assertIn("Brak wiadomości", err)

    def test_unavailable_session_has_its_own_exit_status(self):
        code, _, err = self.run_cli("send", "claude", "praca", "-m", "x", error=Unavailable("No such session; use hubi list"))
        self.assertEqual(code, 4)
        self.assertIn("Nie ma takiej sesji", err)
        code, _, _ = self.run_cli("send", "claude", "praca", "-m", "x", error=HubiError("Message is empty"))
        self.assertEqual(code, 2)


@unittest.skipUnless(LAB, "send delivery only under the disposable UID")
class SendRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        self.repos = []
        for f in Path.home().glob(".agent-*"):
            f.unlink()

    def tearDown(self):
        for w in self.app.runtime_snapshot().workloads:
            self.app.stop(w.record.id, repair=True)

    def repo(self, suffix=""):
        name = "send-"+self._testMethodName+suffix
        shutil.rmtree(Path.home()/"repos"/name, ignore_errors=True)
        return repo(name)

    def cli(self, *args, **kw):
        return subprocess.run([sys.executable, "-m", "hubi6", *args], capture_output=True, text=True, **kw)

    def test_text_reaches_a_terminal_and_runs(self):
        path = self.repo()
        self.app.start("terminal", str(path))
        p = self.cli("send", "terminal", path.name, "-m", "echo delivered > sent.txt")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(wait_for(lambda: (path/"sent.txt").exists() and (path/"sent.txt").read_text()).strip(), "delivered")

    def test_one_session_reaches_another_by_name_after_its_restart(self):
        sender, target = self.repo("-a"), self.repo("-b")
        a = self.app.start("terminal", str(sender))
        old = self.app.start("terminal", str(target))
        self.app.stop(old.id)
        new = self.app.start("terminal", str(target))              # new generation, new pane
        self.assertNotEqual(old.pane, new.pane)
        project = Path(__file__).resolve().parent.parent
        command = (f"cd {project} && {sys.executable} -m hubi6 send terminal {target.name} "
                   f"-m 'echo from-$HUBI6_NAME > from-a.txt'")
        self.app.tmux.paste(a.pane, command)
        self.app.tmux.command("send-keys", "-t", a.pane, "Enter")
        marker = target/"from-a.txt"
        self.assertEqual(wait_for(lambda: marker.exists() and marker.read_text(), 10).strip(), "from-primary")

    def test_session_knows_its_own_name(self):
        path = self.repo()
        self.app.start("claude", str(path), "rev")
        marker = wait_for(lambda: next(path.glob(".agent-*"), None))
        env = json.loads(marker.read_text())["env"]
        self.assertEqual((env["HUBI6_TYPE"], env["HUBI6_REPO"], env["HUBI6_NAME"]), ("claude", str(path), "rev"))

    def test_absent_session_exits_4_and_sends_nothing(self):
        path = self.repo()
        p = self.cli("send", "codex", path.name, "-m", "hello")
        self.assertEqual(p.returncode, 4)
        buffers = self.app.tmux.command("list-buffers", "-F", "#{buffer_name}", check=False).stdout
        self.assertNotIn("hubi6-send-", buffers)


if __name__ == "__main__":
    unittest.main()
