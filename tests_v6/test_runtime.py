from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import pty
import random
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest
from unittest.mock import patch

from hubi6.config import Config
from hubi6.core.model import HubiError, RepoHealth, State
from hubi6.doctor import doctor
from hubi6.services import App
from tests_v6.support import LAB, bootstrap, repo


def wait_for(fn, timeout=5):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError("Condition not reached before timeout")


class Client:
    def __init__(self, args, *, command=None):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        self.before = termios.tcgetattr(self.slave)
        def setup():
            os.setsid()
            fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)
        self.p = subprocess.Popen(command or [sys.executable, "-m", "hubi6", *args], stdin=self.slave, stdout=self.slave,
                                  stderr=self.slave, preexec_fn=setup, close_fds=True,
                                  env=dict(os.environ, TERM="xterm-256color"))
        self.output = b""

    def read_until(self, needle, timeout=5):
        end = time.monotonic()+timeout
        while needle not in self.output and time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], .05)
            if ready:
                try:
                    self.output += os.read(self.master, 65536)
                except OSError:
                    break
        if needle not in self.output:
            raise AssertionError(repr(self.output[-2000:]))

    def send(self, data):
        os.write(self.master, data)

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(3)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        os.close(self.master)
        os.close(self.slave)


@unittest.skipUnless(LAB, "requires separate disposable UID; use tools/v6_lab.py")
class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        for name in ("case-"+self._testMethodName, "case-"+self._testMethodName+"-moved"):
            shutil.rmtree(Path.home()/"repos"/name, ignore_errors=True)
        self.path = repo("case-"+self._testMethodName)

    def tearDown(self):
        for w in self.app.snapshot().workloads:
            self.app.stop(w.record.id, repair=True)

    def start(self, kind="codex", instance="primary"):
        r = self.app.start(kind, str(self.path), instance)
        if kind in ("codex", "claude"):
            pid = self.app.find(r.id).session.pid
            wait_for(lambda: (self.path/(".agent-"+str(pid))).exists())
        return r

    def test_types_unified_view_and_cli_json_F14(self):
        records = [self.start(k) for k in ("codex", "claude", "terminal")]
        snapshot = self.app.snapshot()
        self.assertEqual({w.record.kind for w in snapshot.workloads}, {"codex", "claude", "terminal"})
        self.assertTrue(all(w.state == State.RUNNING for w in snapshot.workloads))
        self.assertEqual(snapshot.subprocesses, 2)
        p = subprocess.run([sys.executable, "-m", "hubi6", "list", "--json"], capture_output=True, text=True, check=True)
        self.assertEqual(len(json.loads(p.stdout)["workloads"]), 3)
        self.assertTrue(doctor(self.app)["ready"])
        for r in records:
            self.app.stop(r.id)
        self.assertFalse(self.app.snapshot().workloads)

    def test_environment_secret_not_argv_F2_F7(self):
        secret = "hubi6-test-secret-7bcc8a33"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret, "ANTHROPIC_API_KEY": secret, "HUBI_ACTIVE": "1",
                                     "TMUX": "/dangerous-owner-socket,172,0", "TMUX_PANE": "%999", "NOT_ALLOWED": "bad"}):
            r = self.start()
        marker = wait_for(lambda: next(self.path.glob(".agent-*"), None))
        data = json.loads(marker.read_text())
        self.assertEqual(data["env"]["OPENAI_API_KEY"], secret)
        for key in ("TMUX", "TMUX_PANE", "HUBI_ACTIVE", "NOT_ALLOWED"):
            self.assertNotIn(key, data["env"])
        self.assertFalse(list(self.app.c.runtime.glob("*.payload")))
        self.assertNotIn(secret, self.app.store.path(r).read_text())
        for proc in Path("/proc").iterdir():
            if proc.name.isdigit():
                try:
                    if proc.stat().st_uid == os.getuid():
                        self.assertNotIn(secret.encode(), (proc/"cmdline").read_bytes())
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    pass

    def test_absent_environment_and_frozen_on_attach(self):
        self.app.tmux.command("set-environment", "-g", "OPENAI_API_KEY", "stale-test-value")
        try:
            with patch.dict(os.environ, {}, clear=True):
                # Keep only the executables lookup; all API variables are absent.
                os.environ["PATH"] = "/usr/bin:/bin"
                r = self.start()
            pid = self.app.find(r.id).session.pid
            data = json.loads((self.path/(".agent-"+str(pid))).read_text())
            self.assertNotIn("OPENAI_API_KEY", data["env"])
            with patch.dict(os.environ, {"OPENAI_API_KEY": "new-test-value"}):
                same = self.start()
                self.assertEqual(same.id, r.id)
            self.assertNotIn(b"new-test-value", Path(f"/proc/{pid}/environ").read_bytes())
        finally:
            self.app.tmux.command("set-environment", "-gu", "OPENAI_API_KEY")

    def test_adversarial_repo_names_F1(self):
        for name in ("C#Sharp", "a#(touch HUBI6_INJECTED)b", "sp ace", "żółć", "$(touch HUBI6_INJECTED)",
                     "`touch HUBI6_INJECTED`", "*?[abc]", "-leading", "#{pid}"):
            with self.subTest(name=name):
                path = repo(name)
                r = self.app.start("terminal", str(path))
                self.assertEqual(self.app.find(r.id).session.cwd, str(path))
                self.app.stop(r.id)
        self.assertFalse((Path.home()/"HUBI6_INJECTED").exists())
        self.assertFalse((Path.home()/"project/HUBI6_INJECTED").exists())

    def test_missing_replaced_invalid_repo_stoppable_F5_F6(self):
        r = self.start()
        self.path.rename(self.path.with_name(self.path.name+"-moved"))
        self.assertEqual(self.app.find(r.id).repo_health, RepoHealth.MISSING)
        repo(self.path.name)
        self.assertEqual(self.app.find(r.id).repo_health, RepoHealth.REPLACED)
        with self.assertRaises(HubiError):
            self.start()
        self.app.stop(r.id)
        r = self.start()
        (self.path/".git/HEAD").write_text("broken")
        self.assertEqual(self.app.find(r.id).repo_health, RepoHealth.INVALID)
        self.app.stop(r.id)

    def test_no_alternate_server_and_readonly_doctor(self):
        self.app.r.run(["systemctl", "--user", "stop", self.app.c.service])
        try:
            files = sorted(str(p) for p in self.app.c.runtime.rglob("*"))
            self.assertFalse(doctor(self.app)["ready"])
            self.assertEqual(files, sorted(str(p) for p in self.app.c.runtime.rglob("*")))
            with self.assertRaises(HubiError):
                self.start()
            self.assertFalse(self.app.c.socket.exists())
        finally:
            self.app.r.run(["systemctl", "--user", "start", self.app.c.service])
            wait_for(lambda: self.app.snapshot().runtime_health == "OK")

    def test_source_file_and_restart_policy_F3(self):
        r = self.start("terminal")
        bad = Path.home()/"bad-tmux.conf"
        bad.write_text("set -g exit-empty on\n")
        self.app.tmux.command("source-file", str(bad))
        self.app.preflight()
        self.assertEqual(self.app.snapshot().exit_empty, "0")
        self.app.stop(r.id)
        self.assertEqual(self.app.snapshot().runtime_health, "OK")

    def test_bare_tmux_cannot_kill_runtime_F2(self):
        r = self.start("terminal")
        before = self.app.snapshot().server_pid
        self.app.tmux.command("send-keys", "-t", r.pane, "-l", "tmux kill-server; touch F2-DONE")
        self.app.tmux.command("send-keys", "-t", r.pane, "Enter")
        wait_for(lambda: (self.path/"F2-DONE").exists())
        self.assertEqual(self.app.snapshot().server_pid, before)
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)

    def test_source_file_last_stop_without_preflight_F3(self):
        r = self.start("terminal")
        bad = Path.home()/"bad-tmux.conf"
        bad.write_text("set -g exit-empty on\n")
        self.app.tmux.command("source-file", str(bad))
        self.app.stop(r.id)
        wait_for(lambda: self.app.snapshot().runtime_health == "OK")

    def test_preflight_unknown_linger_and_missing_kill_capability(self):
        for value in ("no", "unknown"):
            with patch.object(self.app.systemd, "linger", return_value=value), self.assertRaises(HubiError):
                self.start()
        with patch.object(self.app.systemd, "capabilities", return_value=False), self.assertRaises(HubiError):
            self.start()
        self.assertFalse(self.app.snapshot().workloads)

    def test_scope_without_session_and_lost_receipt(self):
        r = self.start()
        self.app.tmux.command("kill-session", "-t", "="+r.session)
        self.app.store.path(r).unlink()
        w = wait_for(lambda: self.app.find(r.id))
        self.assertEqual(w.state, State.ORPHANED)
        self.app.stop(r.id)

    def test_session_without_scope_and_exited_output(self):
        r = self.start()
        self.app.systemd.kill(r, r.invocation, "KILL")
        wait_for(lambda: self.app.find(r.id).state == State.EXITED)
        text = self.app.tmux.command("capture-pane", "-p", "-S", "-", "-t", r.pane).stdout
        self.assertIn("FAKE AGENT READY", text)
        self.app.stop(r.id)

    def test_corrupt_metadata_repair_F11(self):
        r = self.start("terminal")
        self.app.tmux.command("set-option", "-t", r.pane, "@hubi6", "broken")
        self.app.store.path(r).write_text("malformed")
        self.assertEqual(self.app.find(r.id).state, State.UNKNOWN)
        with self.assertRaises(HubiError):
            self.app.stop(r.id)
        self.assertFalse(doctor(self.app)["ready"])
        self.app.stop(r.id, repair=True)
        self.assertFalse(self.app.snapshot().workloads)

    def test_scope_generation_stale_confirmation(self):
        r = self.start()
        with self.assertRaises(HubiError):
            self.app.stop(r.id, expected="a"*16)
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        with self.assertRaises(HubiError):
            self.app.systemd.kill(r, "a"*32, "KILL")
        self.app.stop(r.id)
        successor = self.start()
        with self.assertRaises(HubiError):
            self.app.stop(r.id)
        self.assertEqual(self.app.find(successor.id).state, State.RUNNING)

    def test_systemd_unknown_fails_closed(self):
        from hubi6.core.model import Unit
        r = self.start()
        with patch.object(self.app.systemd, "unit", return_value=Unit(r.scope)):
            with self.assertRaises(HubiError):
                self.app.stop(r.id)
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)

    def test_agent_argv_literal_and_resume(self):
        literal = ["space arg", "#(touch ARGV_BROKEN)", "$(touch ARGV_BROKEN)", "`touch ARGV_BROKEN`", "*?[]", "é"]
        app = App(replace(self.app.c, agents=dict(self.app.c.agents, codex=self.app.c.agents["codex"]+literal)))
        r = app.start("codex", str(self.path), resume=True)
        pid = app.find(r.id).session.pid
        marker = self.path/(".agent-"+str(pid))
        wait_for(marker.exists)
        argv = json.loads(marker.read_text())["argv"]
        self.assertEqual(argv[1:], literal+["resume"])
        self.assertFalse((self.path/"ARGV_BROKEN").exists())

    def test_revalidate_repo_before_external_workload(self):
        create = self.app.tmux.create
        def disappear(record):
            self.path.rename(self.path.with_name(self.path.name+"-moved"))
            return create(record)
        with patch.object(self.app.tmux, "create", side_effect=disappear), self.assertRaises(HubiError):
            self.start()
        snapshot = self.app.snapshot()
        self.assertTrue(snapshot.workloads)
        self.assertTrue(all(not w.unit or not w.unit.live for w in snapshot.workloads))
        self.assertTrue(all(w.repo_health == RepoHealth.MISSING for w in snapshot.workloads))

    def test_concurrent_stops_do_not_touch_successor(self):
        r = self.start()
        def stop(_):
            try:
                App(Config.load()).stop(r.id)
                return "stopped"
            except HubiError:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(stop, range(2)))
        self.assertIn("stopped", results)
        self.assertFalse(self.app.snapshot().workloads)
        successor = self.start()
        self.assertEqual(self.app.find(successor.id).state, State.RUNNING)

    def test_concurrent_workloads_and_independent_locks(self):
        def start(i):
            return App(Config.load()).start("codex", str(self.path), "parallel"+str(i))
        with ThreadPoolExecutor(max_workers=6) as pool:
            records = list(pool.map(start, range(6)))
        self.assertEqual(len({r.scope for r in records}), 6)
        with self.app.store.lock(records[0].key):
            self.app.stop(records[1].id)
        self.assertEqual(len(self.app.snapshot().workloads), 5)
        for r in records[2:]:
            self.assertEqual(self.app.find(r.id).state, State.RUNNING)

    def test_lock_timeout_and_no_descriptor_leak(self):
        r = self.start()
        short = App(replace(self.app.c, lock_timeout=.1))
        with self.app.store.lock(r.key):
            with self.assertRaises(HubiError):
                short.stop(r.id)
        pid = self.app.find(r.id).session.pid
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                self.assertNotIn("/locks/", os.readlink(fd))
            except FileNotFoundError:
                pass

    def test_full_cgroup_stop_setsid_and_sibling(self):
        script = Path.home()/"tree.py"
        script.write_text('''import os,signal,subprocess,sys,time
from pathlib import Path
for s in (signal.SIGHUP,signal.SIGINT,signal.SIGTERM):signal.signal(s,signal.SIG_IGN)
p=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGHUP,signal.SIG_IGN);time.sleep(999)"],start_new_session=True)
Path("child.pid").write_text(str(p.pid))
while True:time.sleep(.1)
''')
        app = App(replace(self.app.c, agents=dict(self.app.c.agents, codex=[sys.executable, str(script)])))
        sibling = self.start("terminal")
        r = app.start("codex", str(self.path))
        wait_for(lambda: (self.path/"child.pid").exists())
        child = int((self.path/"child.pid").read_text())
        self.app.stop(r.id)
        def dead():
            try:
                return Path(f"/proc/{child}/stat").read_text().split()[2] == "Z"
            except FileNotFoundError:
                return True
        wait_for(dead)
        self.assertEqual(self.app.find(sibling.id).state, State.RUNNING)
        self.assertFalse(self.app.systemd.unit(r.scope).live)

    def test_client_loss_multiattach_takeover_and_races(self):
        r = self.start("terminal")
        clients = []
        try:
            c1 = Client(["attach", r.id])
            clients.append(c1)
            wait_for(lambda: self.app.find(r.id).session.attached == 1)
            c2 = Client(["attach", r.id, "--mode", "shared"])
            clients.append(c2)
            wait_for(lambda: self.app.find(r.id).session.attached == 2)
            c3 = Client(["attach", r.id, "--mode", "view"])
            clients.append(c3)
            wait_for(lambda: self.app.find(r.id).session.attached == 3)
            # Read-only client's keys never reach the shell.
            c3.send(b"touch READONLY_BROKEN\n")
            time.sleep(.1)
            self.assertFalse((self.path/"READONLY_BROKEN").exists())
            c4 = Client(["attach", r.id, "--mode", "takeover"])
            clients.append(c4)
            wait_for(lambda: self.app.find(r.id).session.attached == 1)
            c4.p.kill()
            c4.p.wait()
            wait_for(lambda: self.app.find(r.id).state == State.RUNNING)
            for _ in range(3):
                client = Client(["attach", r.id, "--mode", "shared"])
                clients.append(client)
                wait_for(lambda: self.app.find(r.id).session.attached == 1)
                client.send(b"\x02d")
                client.p.wait(3)
                wait_for(lambda: self.app.find(r.id).state == State.RUNNING)
            racers = [Client(["attach", r.id, "--mode", "takeover"]) for _ in range(3)]
            clients.extend(racers)
            self.app.stop(r.id)
            self.assertFalse(self.app.snapshot().workloads)
            for client in clients:
                client.p.wait(4)
        finally:
            for client in clients:
                client.close()

    def test_tmux_server_crash_restarts_without_touching_sibling_runtime_F2(self):
        r = self.start()
        old = self.app.snapshot().server_pid
        self.app.tmux.command("kill-server")
        wait_for(lambda: self.app.snapshot().runtime_health == "OK" and self.app.snapshot().server_pid != old)
        self.assertEqual(self.app.find(r.id).state, State.ORPHANED)
        self.app.stop(r.id)

    def test_runtime_namespace_F4(self):
        r = self.start()
        foreign = App(replace(self.app.c, runtime_id="a"*20))
        self.assertFalse(foreign.snapshot().workloads)
        other = foreign.start("codex", str(self.path))
        self.assertNotEqual(other.scope, r.scope)
        with self.assertRaises(HubiError):
            foreign.systemd.unit(r.scope)
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        foreign.stop(other.id)
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)

    def test_tui_signals_eof_and_terminal_restoration(self):
        for sig, code in ((signal.SIGINT, 130), (signal.SIGHUP, 129), (signal.SIGTERM, 143)):
            client = Client([])
            try:
                client.read_until(b"Choice + Enter:")
                client.p.send_signal(sig)
                self.assertEqual(client.p.wait(3), code)
                self.assertEqual(termios.tcgetattr(client.slave), client.before)
            finally:
                client.close()
        for data, code in ((b"q\n", 98), (b"x\n", 99), (b"\x04", 0)):
            client = Client([])
            try:
                client.read_until(b"Choice + Enter:")
                client.send(data)
                self.assertEqual(client.p.wait(3), code)
                self.assertEqual(termios.tcgetattr(client.slave), client.before)
            finally:
                client.close()

    def test_real_ssh_disconnect_and_launcher_death(self):
        settings = json.loads((Path.home()/".hubi6-ssh.json").read_text())
        r = self.start("terminal")
        before = self.app.find(r.id)
        argv = ["ssh", "-tt", "-i", settings["key"], "-p", str(settings["port"]),
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
                "hubi6test@127.0.0.1", "cd /home/hubi6test/project && exec python3 -m hubi6 attach "+r.id]
        client = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env=dict(os.environ, TERM="xterm-256color"))
        try:
            wait_for(lambda: self.app.find(r.id).session.attached == 1)
            client.kill()  # drops this test TCP connection; no firewall/owner SSH changes
            client.wait(3)
            wait_for(lambda: self.app.find(r.id).state == State.RUNNING)
            after = self.app.find(r.id)
            self.assertEqual(after.record.invocation, before.record.invocation)
            self.assertEqual(after.session.pid, before.session.pid)
        finally:
            if client.poll() is None:
                client.kill()
                client.wait()
            client.stdin.close()

    def test_render_exception_restores_terminal_and_json_on_tty(self):
        script = ('from unittest.mock import patch;from hubi6.cli import main;import sys;'
                  'p=patch("hubi6.services.App.snapshot",side_effect=RuntimeError("injected render fault"));'
                  'p.start();sys.exit(main([]))')
        client = Client([], command=[sys.executable, "-c", script])
        try:
            self.assertEqual(client.p.wait(3), 1)
            self.assertEqual(termios.tcgetattr(client.slave), client.before)
        finally:
            client.close()
        client = Client(["list", "--json"])
        try:
            client.read_until(b"\n}\r\n")
            self.assertEqual(client.p.wait(3), 0)
            self.assertEqual(json.loads(client.output)["schema"], 6)
        finally:
            client.close()

    def test_interrupted_start_recovery_100(self):
        rng = random.Random(6026)
        for i in range(100):
            delay = rng.uniform(.001, .24)
            child = subprocess.Popen([sys.executable, "-m", "hubi6", "start", "codex", str(self.path), "--instance", "crash"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(delay)
            if child.poll() is None:
                child.kill()
            child.wait()
            r = self.app.start("codex", str(self.path), "crash")
            self.assertIn(self.app.find(r.id).state, (State.RUNNING, State.EXITED))
            self.app.stop(r.id)
            self.assertFalse(self.app.snapshot().workloads, f"iteration {i}")
        print("Interrupted starts: 100/100 recovered", flush=True)
