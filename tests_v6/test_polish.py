"""Final targeted re-review regressions. Real lifecycle requires the lab UID."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

from hubi6.adapters.git import Git
from hubi6.config import Config
from hubi6.core.model import HubiError, State
from hubi6.doctor import doctor
from hubi6.services import App
from hubi6.tui import Input
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_runtime import Client, wait_for


def collect(client, duration=.2):
    end = time.monotonic()+duration
    while time.monotonic() < end:
        ready, _, _ = select.select([client.master], [], [], max(0, end-time.monotonic()))
        if ready:
            try:
                chunk = os.read(client.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            client.output += chunk


class PolishUnitTests(unittest.TestCase):
    def test_plain_directories_skipped_but_damaged_git_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in range(3):
                (root/f'ordinary-{n}').mkdir()
            (root/'damaged/.git').mkdir(parents=True)
            (root/'git-link').mkdir()
            (root/'git-link/.git').symlink_to('absent')
            refs, issues = Git(replace(Config.load(), repos=root), None).discover()
            self.assertFalse(refs)
            self.assertEqual(len(issues), 2)
            self.assertFalse(any('ordinary' in issue for issue in issues))

    def test_accepted_line_returns_before_quarantine_wait(self):
        rd, wr = os.pipe()
        self.addCleanup(os.close, rd)
        self.addCleanup(os.close, wr)
        os.write(wr, b'1\n')
        reader = Input(rd)
        original = select.select
        def select_now(read, write, error, timeout=None):
            self.assertIsNone(timeout, 'A valid first line must not wait for burst silence')
            return original(read, write, error, 0)
        with patch('hubi6.tui.select.select', side_effect=select_now):
            self.assertEqual(reader.line(), '1')
        self.assertAlmostEqual(reader.quarantine_until-reader.last_byte, .08)


class PolishPtyTests(unittest.TestCase):
    # These processes exercise only Input/termios, never any runtime lifecycle.
    def input_client(self, body):
        script = 'from hubi6.tui import Input,terminal;import sys,time\nwith terminal(paste=True):\n r=Input(sys.stdin.fileno(),echo=True)\n'+body
        client = Client([], command=[sys.executable, '-c', script])
        self.addCleanup(client.close)
        return client

    def test_line_quarantine_blocks_split_burst_without_confirmation(self):
        client = self.input_client(' a=r.prompt("FIRST>")\n print("ACCEPT1:"+str(a),flush=True)\n time.sleep(.03)\n b=r.prompt("SECOND>")\n print("ACCEPT2:"+str(b),flush=True)\n')
        client.read_until(b'FIRST>')
        client.send(b'1\n')
        client.read_until(b'SECOND>')
        # A later chunk of the SAME burst reaches the next logical read. No
        # destructive prompt/flush exists to mask missing line quarantine.
        client.send(b'd\nyes\n')
        collect(client)
        self.assertNotIn(b'ACCEPT2:', client.output)
        client.send(b'ok\n')
        client.read_until(b'ACCEPT2:ok')
        self.assertEqual(client.p.wait(3), 0)
        self.assertEqual(termios.tcgetattr(client.slave), client.before)

    def test_last_byte_extends_quarantine_and_manual_fast_line_works(self):
        client = self.input_client(' a=r.prompt("FIRST>")\n print("ACCEPT1:"+str(a),flush=True)\n b=r.prompt("SECOND>")\n print("ACCEPT2:"+str(b),flush=True)\n')
        client.read_until(b'FIRST>'); client.send(b'1\n'); client.read_until(b'SECOND>')
        for part in (b'd', b'\n', b'yes\n'):
            time.sleep(.04)
            client.send(part)
        collect(client)
        self.assertNotIn(b'ACCEPT2:', client.output)
        client.send('szybko żółć\n'.encode())
        client.read_until('ACCEPT2:szybko żółć'.encode())
        self.assertEqual(client.p.wait(3), 0)

    def test_empty_backspace_and_split_utf8_echo(self):
        client = self.input_client(' a=r.prompt("PROMPT>")\n print("ANSWER:"+str(a),flush=True)\n')
        client.read_until(b'PROMPT>')
        client.send(b'\x7f\b')
        collect(client, .05)
        self.assertNotIn(b'\b \b', client.output)  # prompt has not been erased
        encoded = 'ż'.encode()
        client.send(encoded[:1]); collect(client, .02)
        client.send(encoded[1:]+b'\x7f\x7fok\n')
        client.read_until(b'ANSWER:ok')
        self.assertEqual(client.output.count(b'\b \b'), 1)
        self.assertNotIn(b'\xef\xbf\xbd', client.output)
        self.assertEqual(client.p.wait(3), 0)
        self.assertEqual(termios.tcgetattr(client.slave), client.before)

    def test_bracketed_paste_and_signals_restore_terminal(self):
        body = ' a=r.prompt("FIRST>")\n print("ANSWER:"+str(a),flush=True)\n b=r.prompt("SECOND>")\n'
        for sig, code in ((signal.SIGINT, 130), (signal.SIGHUP, 129)):
            # Use the CLI signal handler's exit mapping, while exercising only Input.
            script = ('from hubi6.tui import Input,terminal,SignalExit;import sys\n'
                      'try:\n with terminal(paste=True):\n  r=Input(0,echo=True)\n  r.prompt("FIRST>")\n'
                      'except SignalExit as e:raise SystemExit(e.code)\n')
            client = Client([], command=[sys.executable, '-c', script])
            try:
                client.read_until(b'FIRST>')
                client.p.send_signal(sig)
                self.assertEqual(client.p.wait(3), code)
                self.assertEqual(termios.tcgetattr(client.slave), client.before)
            finally:
                client.close()
        client = self.input_client(body)
        client.read_until(b'FIRST>');client.send(b'\x1b[200~d\nyes\n\x1b[201~\n')
        client.read_until(b'SECOND>'); collect(client)
        self.assertIn(b'ANSWER:\r\n', client.output)
        client.send(b'\x04')
        self.assertEqual(client.p.wait(3), 0)
        self.assertEqual(termios.tcgetattr(client.slave), client.before)


@unittest.skipUnless(LAB, 'lifecycle only under disposable UID')
class PolishRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        self.path = repo('polish-'+self._testMethodName)

    def tearDown(self):
        for w in self.app.runtime_snapshot().workloads:
            self.app.stop(w.record.id, repair=True)
            if any(x.record.id == w.record.id for x in self.app.runtime_snapshot().workloads):
                self.app.stop(w.record.id, repair=True)

    def test_runtime_issue_precedes_repo_diagnostics_in_menu(self):
        r = self.app.start('terminal', str(self.path))
        root = self.app.c.repos
        ordinary = [root/f'ordinary-polish-{i}' for i in range(3)]
        damaged = [root/f'damaged-polish-{i}' for i in range(3)]
        for path in ordinary:
            path.mkdir()
        for path in damaged:
            (path/'.git').mkdir(parents=True)
        self.app.tmux.command('set-option', '-u', '-t', r.pane, '@hubi6')
        try:
            snap = self.app.snapshot()
            self.assertFalse(any('ordinary-polish' in issue for issue in snap.issues))
            self.assertTrue(snap.issues[0].startswith('Unverified session'), snap.issues)
            client = Client([])
            try:
                client.read_until(b'Choice + Enter:')
                self.assertIn(b'Unverified session '+r.session.encode(), client.output)
            finally:
                client.close()
        finally:
            self.app.tmux.metadata(r)
            import shutil
            for path in ordinary+damaged:
                shutil.rmtree(path)

    def test_pane_repair_missing_journal_and_session_invocation(self):
        r = self.app.start('terminal', str(self.path))
        sibling = self.app.start('terminal', str(self.path), 'sibling')
        self.app.store.path(r).unlink()
        self.app.tmux.metadata(replace(r, invocation='', phase='launching'))
        self.app.tmux.command('split-window', '-d', '-t', r.pane, '--', '/usr/bin/sleep', '999')
        self.assertEqual(self.app.find(r.id).state, State.UNKNOWN)
        self.assertFalse(self.app.find(r.id).record.invocation)
        self.app.stop(r.id, repair=True)
        repaired = self.app.find(r.id)
        self.assertEqual(repaired.state, State.RUNNING)
        self.assertEqual(repaired.session.pane, r.pane)
        self.assertEqual(repaired.session.panes, 1)
        self.assertEqual(repaired.record.invocation, r.invocation)
        self.assertEqual(self.app.find(sibling.id).state, State.RUNNING)
        # Repeat the fallback while explicit repair races an ordinary stop.
        self.app.store.path(r).unlink()
        self.app.tmux.metadata(replace(r, invocation='', phase='launching'))
        self.app.tmux.command('split-window', '-d', '-t', r.pane, '--', '/usr/bin/sleep', '999')
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda repair: self.stop_result(r.id, repair), (False, True)))
        self.assertIn('ok', results)
        remaining = [w for w in self.app.runtime_snapshot().workloads if w.record.id == r.id]
        if remaining:
            self.assertEqual(remaining[0].state, State.RUNNING)
            self.assertEqual(remaining[0].session.panes, 1)
            self.app.stop(r.id)
        self.assertFalse(any(w.record.id == r.id for w in self.app.runtime_snapshot().workloads))
        self.assertEqual(self.app.find(sibling.id).state, State.RUNNING)

    def stop_result(self, identity, repair=False):
        try:
            self.app.stop(identity, repair=repair)
            return 'ok'
        except HubiError:
            return 'refused'

    def test_first_start_immediate_exec_exit_fails_and_stays_inspectable(self):
        config = Path.home()/'immediate-exit.toml'
        config.write_text('[agents]\ncodex=["/bin/sh","-c","exit 23"]\n')
        try:
            for i in range(5):
                cli = [sys.executable, '-m', 'hubi6', '--config', str(config)]
                result = subprocess.run(cli+['start', 'codex', str(self.path), '--instance', f'exit{i}', '--json'], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('startup', result.stderr)
                w = wait_for(lambda: next((w for w in self.app.runtime_snapshot().workloads if w.state == State.EXITED), None))
                self.assertEqual(w.data()['exit_status'], 23)
                listed = subprocess.run(cli+['list', '--json'], capture_output=True, text=True)
                self.assertEqual(json.loads(listed.stdout)['workloads'][0]['exit_status'], 23)
                inspected = subprocess.run(cli+['inspect', w.record.id], capture_output=True, text=True)
                self.assertEqual(json.loads(inspected.stdout)['state'], 'EXITED')
                self.assertEqual(json.loads(inspected.stdout)['exit_status'], 23)
                stopped = subprocess.run(cli+['stop', w.record.id], capture_output=True, text=True)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertFalse(self.app.runtime_snapshot().workloads)
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: subprocess.run(cli+['start', 'codex', str(self.path), '--instance', 'race', '--json'], capture_output=True, text=True), range(2)))
            self.assertTrue(all(p.returncode != 0 for p in results))
            workloads = self.app.runtime_snapshot().workloads
            self.assertEqual(len(workloads), 1)
            self.assertEqual(workloads[0].state, State.EXITED)
            self.assertEqual(workloads[0].data()['exit_status'], 23)
            self.app.stop(workloads[0].record.id)
        finally:
            config.unlink(missing_ok=True)

    def test_xdg_namespace_diagnostic_never_adopts_other_objects(self):
        own = self.app.start('terminal', str(self.path))
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(Path.home()/'polish-other-state')}):
            other = App(Config.load())
        foreign = other.start('terminal', str(self.path))
        try:
            self.assertNotEqual(other.c.runtime_id, self.app.c.runtime_id)
            self.assertEqual([w.record.id for w in self.app.snapshot().workloads], [own.id])
            self.assertEqual([w.record.id for w in other.snapshot().workloads], [foreign.id])
            result = doctor(self.app)
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(result['checks']['other_runtime_objects'], 2)
            self.assertEqual(doctor(other)['checks']['other_runtime_objects'], 2)
            for method in (self.app.find, self.app.stop):
                with self.assertRaises(HubiError):
                    method(foreign.id)
            self.assertEqual(other.find(foreign.id).state, State.RUNNING)
            cli = subprocess.run([sys.executable, '-m', 'hubi6', 'doctor'], capture_output=True, text=True)
            self.assertIn('other_runtime_objects: 2', cli.stdout)
            self.app.tmux.command('set-option', '-u', '-t', foreign.pane, '@hubi6')
            self.assertEqual(doctor(self.app)['checks']['other_runtime_objects'], 1)
            self.assertEqual([w.record.id for w in self.app.snapshot().workloads], [own.id])
        finally:
            other.tmux.metadata(foreign)
            other.stop(foreign.id)

    def test_whole_main_burst_stays_on_workloads_screen(self):
        r = self.app.start('terminal', str(self.path))
        client = Client([])
        try:
            client.read_until(b'Choice + Enter:');client.output = b''
            client.send(b'w\n1\nd\nyes\n')
            client.read_until(b'Choice + Enter:');collect(client)
            self.assertNotIn(b'Clients:', client.output)  # no detail screen
            self.assertNotIn(b'Type yes', client.output)
            self.assertEqual(self.app.find(r.id).state, State.RUNNING)
            time.sleep(.1);client.output = b'';client.send(b'1\nd\nyes\n')
            client.read_until(b'Choice + Enter:');collect(client)
            self.assertIn(b'Clients:', client.output)
            self.assertNotIn(b'Type yes', client.output)
            self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        finally:
            client.close()
