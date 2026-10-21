"""Pane resolver: the current tmux pane of a session from its stable identity.

Selection logic runs anywhere on fakes; the real runtime only under the lab UID.
"""
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

from hubi6.config import Config
from hubi6.core.model import HubiError, State, Unavailable
from hubi6.i18n import STATE_PL, pl
from hubi6.services import App
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_menu import FakeGit, snapshot, workload
from tests_v6.test_runtime import wait_for


class NoRunner:
    calls = 0

    def run(self, *args, **kwargs):
        raise AssertionError("selection needs no subprocess")


class PaneSelectionTests(unittest.TestCase):
    def app(self, workloads, **snap):
        app = App(Config.load(), NoRunner())
        app.git = FakeGit()
        s = replace(snapshot(workloads), **snap)
        app.runtime_snapshot = lambda: s
        return app

    def item(self, state, kind="claude", instance="primary", name="praca", pane="%14"):
        w = workload(kind, instance, name, state, created=time.time())
        w.session.pane = w.record.pane = pane
        return w

    def test_running_or_attached_generation_is_returned(self):
        for state in (State.RUNNING, State.ATTACHED):
            with self.subTest(state=state):
                target = self.item(state)
                others = [self.item(State.RUNNING, kind="codex", pane="%5"),
                          self.item(State.RUNNING, instance="rev", pane="%6"),
                          self.item(State.RUNNING, name="alpha", pane="%7")]
                w = self.app([*others, target]).pane("claude", "praca")
                self.assertIs(w, target)
                self.assertEqual(w.session.pane, "%14")

    def test_session_that_is_not_running_is_named_with_its_state(self):
        for state in (State.EXITED, State.ORPHANED, State.STARTING, State.UNKNOWN, State.STALE):
            with self.subTest(state=state):
                w = self.item(state)
                with self.assertRaises(Unavailable) as error:
                    self.app([w]).pane("claude", "praca")
                self.assertEqual(str(error.exception), f"Workload is {state.value}, not running: {w.record.id}")
                self.assertEqual(pl(error.exception), f"Sesja nie działa ({STATE_PL[state]}): {w.record.id}")

    def test_missing_session(self):
        with self.assertRaisesRegex(Unavailable, "No such session"):
            self.app([self.item(State.RUNNING, kind="codex")]).pane("claude", "praca")

    def test_two_running_generations_are_refused(self):
        first, second = self.item(State.RUNNING), self.item(State.RUNNING, pane="%15")
        second.record.generation = "e"*16
        with self.assertRaisesRegex(HubiError, "Several running generations"):
            self.app([first, second]).pane("claude", "praca")

    def test_stopped_runtime_is_named_as_stopped(self):
        with self.assertRaises(HubiError) as error:
            self.app([], runtime_health="DOWN", tmux_known=False).pane("claude", "praca")
        self.assertNotIsInstance(error.exception, Unavailable)
        self.assertEqual(pl(error.exception), "Runtime Hubi nie działa; uruchom `hubi doctor`")

    def test_unknown_runtime_refuses_instead_of_guessing(self):
        for field in ("tmux_known", "systemd_known"):
            with self.subTest(field=field), self.assertRaisesRegex(HubiError, "Runtime state UNKNOWN"):
                self.app([self.item(State.RUNNING)], **{field: False}).pane("claude", "praca")

    def test_list_json_carries_key_and_live_pane(self):
        w = self.item(State.RUNNING)
        data = w.data()
        self.assertEqual((data["key"], data["pane"]), (w.record.key, "%14"))
        for state in (State.EXITED, State.ORPHANED, State.STARTING, State.UNKNOWN, State.STALE):
            with self.subTest(state=state):
                self.assertEqual(self.item(state).data()["pane"], "")   # a dead pane is never an address
        w.session = None
        self.assertEqual(w.data()["pane"], "")


class PaneCommandLineTests(unittest.TestCase):
    def run_cli(self, *argv):
        target = workload("claude", "primary", "praca", State.RUNNING, created=time.time())
        target.session.pane = "%14"
        calls = []

        def pane(app, kind, repo, instance="primary"):
            calls.append((kind, repo, instance))
            return target

        out = io.StringIO()
        # Read-only like list/inspect: no state directories, log or locks.
        with patch.object(App, "pane", pane), patch.object(Config, "prepare", side_effect=AssertionError("prepare")), \
                contextlib.redirect_stdout(out):
            from hubi6.cli import main
            code = main(list(argv))
        return code, out.getvalue(), calls, target

    def test_prints_only_the_pane(self):
        code, out, calls, _ = self.run_cli("pane", "claude", "praca")
        self.assertEqual((code, out), (0, "%14\n"))
        self.assertEqual(calls, [("claude", "praca", "primary")])

    def test_polish_default_name_and_json(self):
        code, out, calls, target = self.run_cli("pane", "codex", "~", "główna", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("codex", "~", "primary")])
        data = json.loads(out)
        self.assertEqual((data["pane"], data["id"], data["state"], data["tmux_session"]),
                         ("%14", target.record.id, "RUNNING", target.record.session))
        self.assertEqual(data["socket"], str(Config.load().socket))

    def test_repository_is_required(self):
        from hubi6.cli import parser
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as stop:
            parser().parse_args(["pane", "claude"])
        self.assertEqual(stop.exception.code, 2)


@unittest.skipUnless(LAB, "pane resolver lifecycle only under the disposable UID")
class PaneRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        shutil.rmtree(Path.home()/"repos"/("pane-"+self._testMethodName), ignore_errors=True)
        self.path = repo("pane-"+self._testMethodName)

    def tearDown(self):
        for w in self.app.runtime_snapshot().workloads:
            self.app.stop(w.record.id, repair=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "hubi6", *args], capture_output=True, text=True)

    def live_pane(self, record):
        p = self.app.tmux.command("list-panes", "-t", "="+record.session+":", "-F", "#{pane_id}")
        return p.stdout.strip()

    def test_pane_follows_the_session_across_generations(self):
        name = self.path.name
        first = self.app.start("claude", str(self.path))
        p = self.cli("pane", "claude", name)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, self.live_pane(first)), p.stderr)
        data = json.loads(self.cli("pane", "claude", str(self.path), "--json").stdout)
        self.assertEqual((data["id"], data["state"], data["socket"]), (first.id, "RUNNING", str(self.app.c.socket)))
        self.assertEqual(self.cli("pane", "codex", name).returncode, 4)       # same repo, other type
        self.assertEqual(self.cli("pane", "claude", name, "rev").returncode, 4)  # same repo, other name
        self.app.stop(first.id)
        p = self.cli("pane", "claude", name)
        self.assertEqual(p.returncode, 4)
        self.assertIn("Nie ma takiej sesji", p.stderr)
        second = self.app.start("claude", str(self.path))
        p = self.cli("pane", "claude", name)
        self.assertEqual(p.stdout.strip(), self.live_pane(second))
        self.assertNotEqual(p.stdout.strip(), first.pane)                   # tmux never reuses pane IDs
        listed = json.loads(self.cli("list", "--json").stdout)["workloads"]
        self.assertEqual([(w["key"], w["pane"]) for w in listed], [(second.key, second.pane)])

    def test_finished_session_is_reported_not_resolved(self):
        r = self.app.start("terminal", str(self.path))
        self.app.tmux.command("send-keys", "-t", r.pane, "exit 3", "Enter")
        wait_for(lambda: self.app.find(r.id, health_check=False).state == State.EXITED)
        p = self.cli("pane", "terminal", self.path.name)
        self.assertEqual((p.returncode, p.stdout), (4, ""))
        self.assertIn(f"(zakończony): {r.id}", p.stderr)

    def test_resolver_writes_nothing(self):
        r = self.app.start("codex", str(self.path))
        def state():
            return sorted((str(p), p.stat().st_mtime_ns) for base in (self.app.c.state_base, self.app.c.runtime)
                          for p in base.rglob("*") if p.is_file())
        before = state()
        self.assertEqual(self.cli("pane", "codex", self.path.name).stdout.strip(), r.pane)
        self.assertEqual(state(), before)


if __name__ == "__main__":
    unittest.main()
