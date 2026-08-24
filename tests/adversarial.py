#!/usr/bin/env python3
"""High-fidelity release-gate regressions for Hubi v4.

Every test uses a unique tmux socket, temporary repositories, fake agents, and
real disposable systemd user scopes. It never addresses the default tmux
server.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import struct
import termios
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HUBI = ROOT / "hubi"
REAL_TMUX = shutil.which("tmux") or "/usr/bin/tmux"


class PtyProcess:
    def __init__(
        self, argv: list[str], env: dict[str, str], size: tuple[int, int] | None = None
    ) -> None:
        self.pid, self.fd = pty.fork()
        self.buffer = bytearray()
        if self.pid == 0:
            if size is not None:
                rows, columns = size
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            child_env = env.copy()
            child_env.pop("TMUX", None)
            child_env.setdefault("TERM", "xterm-256color")
            os.execvpe(argv[0], argv, child_env)
        os.set_blocking(self.fd, False)
        self.reaped = False

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def set_size(self, rows: int, columns: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def wait(self, timeout: float = 4.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                self.reaped = True
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
        raise TimeoutError(f"PTY process {self.pid} did not exit")

    def read_available(self, timeout: float = 0.1) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            self.buffer.extend(chunk)
        return bytes(self.buffer)

    def wait_for(self, needle: bytes, timeout: float = 4.0) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.buffer and time.monotonic() < deadline:
            self.read_available(0.1)
        if needle not in self.buffer:
            raise AssertionError(
                f"PTY did not show {needle!r}; output={bytes(self.buffer)[-2000:]!r}"
            )
        return bytes(self.buffer)

    def close(self) -> None:
        if self.reaped:
            try:
                os.close(self.fd)
            except OSError:
                pass
            return
        try:
            os.kill(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            waited, _ = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
        try:
            os.close(self.fd)
        except OSError:
            pass


class HubiAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="hubi-adversarial-"))
        self.repos = self.temp / "repos"
        self.runtime = self.temp / "runtime"
        self.repos.mkdir()
        self.runtime.mkdir(mode=0o700)
        self.unique = f"red{os.getpid()}-{time.time_ns() % 1_000_000_000}"
        self.socket = f"hubi-adversarial-{self.unique}"
        self.repo_name = f"{self.unique}-repo"
        self.repo = self.repos / self.repo_name
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.tmux_wrapper = self.temp / "tmux-clean"
        self.tmux_wrapper.write_text(
            f"#!/usr/bin/env bash\nexec {REAL_TMUX} -f /dev/null \"$@\"\n"
        )
        self.tmux_wrapper.chmod(0o755)
        self.input_log = self.temp / "agent-input"
        self.fake_agent = self.temp / "fake-agent"
        self.fake_agent.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'AGENT_READY\\n'\n"
            "trap 'exit 0' INT TERM\n"
            "while IFS= read -r line; do printf '%s\\n' \"$line\" >>\"$AGENT_INPUT_LOG\"; done\n"
        )
        self.fake_agent.chmod(0o755)
        self.env = os.environ.copy()
        self.env.pop("TMUX", None)
        self.env.pop("HUBI_ACTIVE", None)
        self.env.update(
            {
                "HUBI_REPOS": str(self.repos),
                "HUBI_TMUX_SOCKET": self.socket,
                "HUBI_TMUX_BIN": str(self.tmux_wrapper),
                "HUBI_CODEX_BIN": str(self.fake_agent),
                "HUBI_CLAUDE_BIN": str(self.fake_agent),
                "HUBI_FILE": str(HUBI),
                "AGENT_INPUT_LOG": str(self.input_log),
                "TERM": "xterm-256color",
            }
        )
        self.ptys: list[PtyProcess] = []

    def tearDown(self) -> None:
        for process in reversed(self.ptys):
            process.close()
        # Stop only units recorded by sessions on this disposable server.
        result = self.tmux("list-sessions", "-F", "#{@hubi-scope}", check=False)
        for scope in result.stdout.splitlines():
            if scope.startswith("hubi-") and scope.endswith(".scope"):
                subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "kill",
                        "--kill-whom=all",
                        "--signal=KILL",
                        scope,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        # Also clean the two predictable agent scopes if tmux metadata vanished.
        for agent in ("codex", "claude"):
            scope = self.scope_name(agent)
            subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "kill",
                    "--kill-whom=all",
                    "--signal=KILL",
                    scope,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.tmux("kill-server", check=False)
        for agent in ("codex", "claude"):
            try:
                self.lock_path(agent).unlink()
            except FileNotFoundError:
                pass
        socket_path = Path(f"/tmp/tmux-{os.getuid()}") / self.socket
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(self.temp, ignore_errors=True)

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [REAL_TMUX, "-f", "/dev/null", "-L", self.socket, *args],
            text=True,
            capture_output=True,
            check=check,
        )

    def bash(
        self, script: str, timeout: float = 8, check: bool = True, env=None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", script],
            env=env or self.env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=check,
        )

    def scope_name(self, agent: str = "codex") -> str:
        digest = hashlib.sha256(self.repo_name.encode()).hexdigest()[:12]
        return f"hubi-{agent}-{digest}.scope"

    def lock_path(self, agent: str = "codex") -> Path:
        runtime = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        lock_dir = runtime / f"hubi-locks-{os.getuid()}"
        digest = hashlib.sha256(f"{agent}:{self.repo_name}".encode()).hexdigest()[:12]
        return lock_dir / f"{digest}.lock"

    def session_name(self, agent: str = "codex") -> str:
        digest = hashlib.sha256(self.repo_name.encode()).hexdigest()[:12]
        slug = self.repo_name[:28]
        return f"hubi-{agent}-{slug}-{digest}"

    def scope_active(self, agent: str = "codex") -> bool:
        return (
            subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", self.scope_name(agent)]
            ).returncode
            == 0
        )

    def start_agent(self, agent: str = "codex") -> None:
        result = self.bash(
            'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; '
            'ensure_agent_session "$AGENT" "$RESOLVED_REPO_KEY" '
            '"$RESOLVED_REPO_DIR" "$FAKE_AGENT"',
            check=False,
            env={
                **self.env,
                "REPO_NAME": self.repo_name,
                "AGENT": agent,
                "FAKE_AGENT": str(self.fake_agent),
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 3
        while not self.scope_active(agent) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.scope_active(agent), "agent scope did not become active")

    def spawn_hubi(self) -> PtyProcess:
        process = PtyProcess([str(HUBI)], self.env)
        self.ptys.append(process)
        process.wait_for(b"Projekty")
        return process

    def spawn_tmux_client(
        self,
        session: str,
        readonly: bool = False,
        size: tuple[int, int] | None = None,
    ) -> PtyProcess:
        argv = [REAL_TMUX, "-f", "/dev/null", "-L", self.socket, "attach-session"]
        if readonly:
            argv.append("-r")
        argv.extend(["-t", session])
        process = PtyProcess(argv, self.env, size=size)
        self.ptys.append(process)
        time.sleep(0.4)
        process.read_available()
        return process

    def enter_project(self, process: PtyProcess) -> None:
        process.send(b"1\n")
        process.wait_for(b"Projekt:")

    def assert_agent_survives(self) -> None:
        time.sleep(0.6)
        self.assertTrue(self.scope_active(), "destructive paste stopped the agent scope")
        self.assertEqual(
            self.tmux("has-session", "-t", self.session_name(), check=False).returncode,
            0,
            "destructive paste removed the agent session",
        )

    def test_multiline_bracketed_paste_cannot_confirm_stop(self) -> None:
        self.start_agent()
        hubi = self.spawn_hubi()
        self.enter_project(hubi)
        hubi.send(b"\x1b[200~git log --oneline\nc\ny\n\x1b[201~")
        self.assert_agent_survives()

    def test_multiline_bracketed_paste_after_shell_roundtrip(self) -> None:
        self.start_agent()
        hubi = self.spawn_hubi()
        hubi.send(b"h\n")
        hubi.wait_for(b"Shell ai-devbox")
        hubi.send(b"exit\n")
        # Wait for a fresh main-menu render after child Bash disables paste mode.
        deadline = time.monotonic() + 4
        while bytes(hubi.buffer).count(b"Projekty") < 2 and time.monotonic() < deadline:
            hubi.read_available()
        self.assertGreaterEqual(bytes(hubi.buffer).count(b"Projekty"), 2)
        self.enter_project(hubi)
        hubi.send(b"\x1b[200~git log --oneline\nc\ny\n\x1b[201~")
        self.assert_agent_survives()

    def test_cross_menu_burst_cannot_reach_writable_agent(self) -> None:
        self.start_agent()
        watcher = self.spawn_tmux_client(self.session_name(), readonly=True)
        hubi = self.spawn_hubi()
        hubi.send(b"1\n1\ns\nBURST_LEAK\n")
        time.sleep(1)
        watcher.read_available()
        leaked = self.input_log.read_text() if self.input_log.exists() else ""
        self.assertNotIn("BURST_LEAK", leaked)

    def test_multiline_paste_with_all_action_keys_is_inert(self) -> None:
        self.start_agent()
        hubi = self.spawn_hubi()
        self.enter_project(hubi)
        hubi.send(b"\x1b[200~\nc\ny\na\n1\n2\np\ns\nx\nq\n\x1b[201~")
        self.assert_agent_survives()

    def make_stalling_tmux_wrapper(self) -> Path:
        marker = self.temp / "cold-server-created"
        wrapper = self.temp / "tmux-stalling"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \" $* \" == *\" new-session \"* ]]; then\n"
            f"  {REAL_TMUX} -f /dev/null \"$@\"\n"
            "  rc=$?\n"
            "  : >\"$LOCK_MARKER\"\n"
            "  sleep 60\n"
            "  exit \"$rc\"\n"
            "fi\n"
            f"exec {REAL_TMUX} -f /dev/null \"$@\"\n"
        )
        wrapper.chmod(0o755)
        self.env["HUBI_TMUX_BIN"] = str(wrapper)
        self.env["LOCK_MARKER"] = str(marker)
        return marker

    def tmux_server_holds_lock(self) -> bool:
        pid_result = self.tmux("display-message", "-p", "#{pid}")
        pid = int(pid_result.stdout.strip())
        lock_path = self.lock_path()
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                if fd.resolve() == lock_path:
                    return True
            except (FileNotFoundError, PermissionError):
                continue
        return False

    def run_interrupted_cold_start(self, sig: signal.Signals) -> None:
        marker = self.make_stalling_tmux_wrapper()
        process = subprocess.Popen(
            [str(HUBI), "codex", self.repo_name],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "cold tmux server was not created")
        os.killpg(process.pid, sig)
        process.wait(timeout=3)
        if process.stdout is not None:
            process.stdout.close()
        self.assertFalse(self.tmux_server_holds_lock(), "tmux server inherited startup lock")
        # A retry must never wait forever on a lock left by the dead launcher.
        retry = self.bash(
            'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; '
            'ensure_agent_session codex "$RESOLVED_REPO_KEY" '
            '"$RESOLVED_REPO_DIR" "$FAKE_AGENT"',
            timeout=4,
            check=False,
            env={
                **self.env,
                "HUBI_TMUX_BIN": str(self.tmux_wrapper),
                "REPO_NAME": self.repo_name,
                "FAKE_AGENT": str(self.fake_agent),
            },
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_cold_start_term_does_not_wedge_lock(self) -> None:
        self.run_interrupted_cold_start(signal.SIGTERM)

    def test_cold_start_sigkill_does_not_wedge_lock(self) -> None:
        self.run_interrupted_cold_start(signal.SIGKILL)

    def test_lock_timeout_is_bounded_and_visible(self) -> None:
        sentinel = f"{self.unique}-sentinel"
        self.tmux("new-session", "-d", "-s", sentinel, "--", "sleep", "30")
        lock_dir = self.lock_path().parent
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        lock_path = self.lock_path()
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = time.monotonic()
            try:
                result = self.bash(
                    'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; '
                    'ensure_agent_session codex "$RESOLVED_REPO_KEY" '
                    '"$RESOLVED_REPO_DIR" "$FAKE_AGENT"',
                    timeout=4,
                    check=False,
                    env={
                        **self.env,
                        "REPO_NAME": self.repo_name,
                        "FAKE_AGENT": str(self.fake_agent),
                    },
                )
            except subprocess.TimeoutExpired as error:
                self.fail(f"startup lock wait was unbounded: {error}")
        self.assertLess(time.monotonic() - started, 4)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock", (result.stdout + result.stderr).lower())
        self.assertEqual(self.tmux("has-session", "-t", sentinel, check=False).returncode, 0)

    def test_missing_full_cgroup_kill_capability_fails_closed(self) -> None:
        unsupported = self.temp / "systemctl-unsupported"
        unsupported.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == kill && \"$2\" == --help ]]; then echo 'no cgroup kill'; exit 0; fi\n"
            "exit 1\n"
        )
        unsupported.chmod(0o755)
        result = self.bash(
            'source "$HUBI_FILE"; SYSTEMCTL_BIN="$UNSUPPORTED"; require_scope_kill_capability',
            check=False,
            env={**self.env, "UNSUPPORTED": str(unsupported)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pełnego cgroup", result.stderr)

    def test_missing_tmux_session_is_reported_and_cleanup_of_scope_works(self) -> None:
        orphan_agent = self.temp / "orphan-agent"
        orphan_agent.write_text(
            "#!/usr/bin/env bash\n"
            "setsid bash -c 'trap \"\" HUP INT TERM; while :; do sleep 1; done' &\n"
            "wait\n"
        )
        orphan_agent.chmod(0o755)
        self.fake_agent = orphan_agent
        self.start_agent()
        self.tmux("kill-session", "-t", self.session_name())
        self.assertTrue(self.scope_active(), "test precondition: scope should survive tmux loss")
        result = self.bash(
            'source "$HUBI_FILE"; printf "STATUS=%s\\n" '
            '"$(agent_status codex "$REPO_NAME")"; stop_agent_now codex "$REPO_NAME"',
            check=False,
            env={**self.env, "REPO_NAME": self.repo_name},
        )
        self.assertIn("ORPHANED", result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.scope_active())
        self.start_agent()
        self.assertTrue(self.scope_active())
        self.assertEqual(
            self.tmux("has-session", "-t", self.session_name(), check=False).returncode,
            0,
        )

    def test_status_and_capture_use_pinned_agent_pane(self) -> None:
        marker_agent = self.temp / "crash-agent"
        marker_agent.write_text(
            "#!/usr/bin/env bash\n"
            "echo PINNED_AGENT_OUTPUT\n"
            "trap 'echo PINNED_AGENT_CRASH; exit 0' TERM\n"
            "while :; do sleep 1; done\n"
        )
        marker_agent.chmod(0o755)
        self.fake_agent = marker_agent
        self.start_agent()
        session = self.session_name()
        self.tmux(
            "new-window",
            "-t",
            session,
            "-n",
            "second",
            "--",
            "bash",
            "-c",
            "echo SECOND_WINDOW_OUTPUT; while :; do sleep 1; done",
        )
        subprocess.run(
            [
                "systemctl",
                "--user",
                "kill",
                "--kill-whom=all",
                "--signal=TERM",
                self.scope_name(),
            ],
            check=True,
        )
        deadline = time.monotonic() + 3
        while self.scope_active() and time.monotonic() < deadline:
            time.sleep(0.05)
        result = self.bash(
            'source "$HUBI_FILE"; printf "STATUS=%s\\n" '
            '"$(session_status "$SESSION")"; capture_session_output "$SESSION"',
            env={**self.env, "SESSION": session},
        )
        self.assertIn("EXITED", result.stdout)
        self.assertIn("PINNED_AGENT_OUTPUT", result.stdout)
        self.assertNotIn("SECOND_WINDOW_OUTPUT", result.stdout)

    def test_new_windows_in_managed_session_keep_safety_options(self) -> None:
        self.start_agent()
        session = self.session_name()
        self.tmux("new-window", "-d", "-t", session, "-n", "second", "--", "sleep", "30")
        self.tmux("select-window", "-t", f"{session}:second")
        size = self.tmux(
            "show-window-options", "-v", "-t", f"{session}:second", "window-size"
        ).stdout.strip()
        remain = self.tmux(
            "show-window-options", "-v", "-t", f"{session}:second", "remain-on-exit"
        ).stdout.strip()
        self.assertEqual(size, "largest")
        self.assertEqual(remain, "on")

    def test_hubi_active_never_reaches_tmux_server_agent_or_later_pane(self) -> None:
        agent_env = self.temp / "agent-hubi-active"
        later_env = self.temp / "later-hubi-active"
        env_agent = self.temp / "env-agent"
        env_agent.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s' \"${HUBI_ACTIVE-unset}\" >\"$AGENT_ENV_FILE\"\n"
            "trap 'exit 0' INT TERM\n"
            "while :; do sleep 1; done\n"
        )
        env_agent.chmod(0o755)
        env = {
            **self.env,
            "HUBI_CODEX_BIN": str(env_agent),
            "AGENT_ENV_FILE": str(agent_env),
        }
        hubi = PtyProcess([str(HUBI), "codex", self.repo_name], env)
        self.ptys.append(hubi)
        deadline = time.monotonic() + 5
        while not agent_env.exists() and time.monotonic() < deadline:
            hubi.read_available()
            time.sleep(0.05)
        self.assertTrue(agent_env.exists(), "agent did not start")
        hubi.send(b"\x02d")
        time.sleep(0.3)
        pid = int(self.tmux("display-message", "-p", "#{pid}").stdout.strip())
        server_environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        self.tmux(
            "new-window",
            "-d",
            "-t",
            self.session_name(),
            "-n",
            "later",
            "--",
            "bash",
            "-c",
            f"printf '%s' \"${{HUBI_ACTIVE-unset}}\" >{later_env}",
        )
        deadline = time.monotonic() + 2
        while not later_env.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotIn(b"HUBI_ACTIVE=1", server_environment)
        self.assertEqual(agent_env.read_text(), "unset")
        self.assertEqual(later_env.read_text(), "unset")

    def attach_mode_setup(self) -> tuple[PtyProcess, PtyProcess]:
        self.start_agent()
        watcher = self.spawn_tmux_client(self.session_name(), readonly=True)
        hubi = self.spawn_hubi()
        self.enter_project(hubi)
        hubi.send(b"1\n")
        hubi.wait_for("Sesja ma już podłączonych klientów".encode("utf-8"))
        return hubi, watcher

    def test_hubi_view_mode_is_read_only(self) -> None:
        hubi, _ = self.attach_mode_setup()
        hubi.send(b"v\n")
        hubi.wait_for(b"AGENT_READY")
        hubi.send(b"VIEW_MUST_NOT_ARRIVE\n")
        time.sleep(0.5)
        content = self.input_log.read_text() if self.input_log.exists() else ""
        self.assertNotIn("VIEW_MUST_NOT_ARRIVE", content)

    def test_hubi_share_mode_is_writable(self) -> None:
        hubi, _ = self.attach_mode_setup()
        hubi.send(b"s\n")
        hubi.wait_for(b"AGENT_READY")
        hubi.send(b"SHARE_ARRIVED\n")
        deadline = time.monotonic() + 2
        while not self.input_log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("SHARE_ARRIVED", self.input_log.read_text())

    def test_hubi_takeover_detaches_other_client_and_is_writable(self) -> None:
        hubi, watcher = self.attach_mode_setup()
        hubi.send(b"p\n")
        hubi.wait_for(b"AGENT_READY")
        deadline = time.monotonic() + 2
        clients = ""
        while time.monotonic() < deadline:
            clients = self.tmux(
                "list-clients", "-t", self.session_name(), "-F", "#{client_name}"
            ).stdout
            if len(clients.splitlines()) == 1:
                break
            time.sleep(0.05)
        self.assertEqual(len(clients.splitlines()), 1)
        watcher.read_available()
        hubi.send(b"TAKEOVER_ARRIVED\n")
        deadline = time.monotonic() + 2
        while not self.input_log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("TAKEOVER_ARRIVED", self.input_log.read_text())

    def test_hubi_attach_back_does_not_attach(self) -> None:
        hubi, _ = self.attach_mode_setup()
        hubi.send(b"b\n")
        deadline = time.monotonic() + 2
        while bytes(hubi.buffer).count(b"Projekt:") < 2 and time.monotonic() < deadline:
            hubi.read_available()
        clients = self.tmux(
            "list-clients", "-t", self.session_name(), "-F", "#{client_name}"
        ).stdout.splitlines()
        self.assertGreaterEqual(bytes(hubi.buffer).count(b"Projekt:"), 2)
        self.assertEqual(len(clients), 1)

    def test_q_and_x_return_documented_codes_on_real_pty(self) -> None:
        for key, expected in ((b"q\n", 98), (b"x\n", 99)):
            process = self.spawn_hubi()
            process.send(key)
            self.assertEqual(process.wait(), expected)

    def test_menu_signals_on_real_pty_have_explicit_codes(self) -> None:
        for signum, expected, name in (
            (signal.SIGINT, 130, b"INT"),
            (signal.SIGHUP, 129, b"HUP"),
            (signal.SIGTERM, 143, b"TERM"),
        ):
            process = self.spawn_hubi()
            foreground = os.tcgetpgrp(process.fd)
            os.killpg(foreground, signum)
            self.assertEqual(process.wait(), expected)
            process.read_available()
            self.assertIn(b"odebrano " + name, process.buffer)

    def test_ctrl_c_while_attached_reaches_agent_not_launcher(self) -> None:
        marker = self.temp / "foreground-int"
        foreground_agent = self.temp / "foreground-agent"
        foreground_agent.write_text(
            "#!/usr/bin/env bash\n"
            "echo AGENT_FOREGROUND_READY\n"
            "trap 'echo INT >\"$FOREGROUND_MARKER\"; exit 0' INT\n"
            "while :; do sleep 1; done\n"
        )
        foreground_agent.chmod(0o755)
        process = PtyProcess(
            [str(HUBI), "codex", self.repo_name],
            {
                **self.env,
                "HUBI_CODEX_BIN": str(foreground_agent),
                "FOREGROUND_MARKER": str(marker),
            },
        )
        self.ptys.append(process)
        process.wait_for(b"AGENT_FOREGROUND_READY")
        process.send(b"\x03")
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            process.read_available()
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "Ctrl+C did not reach foreground agent")
        process.send(b"\x02d")
        self.assertEqual(process.wait(), 0)

    def test_legacy_session_is_attachable_but_never_managed(self) -> None:
        legacy = f"codex-{self.repo_name}"
        self.tmux(
            "new-session",
            "-d",
            "-s",
            legacy,
            "-c",
            str(self.repo),
            "--",
            "bash",
            "-c",
            "echo LEGACY_READY; while :; do sleep 1; done",
        )
        process = PtyProcess([str(HUBI), "codex", self.repo_name], self.env)
        self.ptys.append(process)
        process.wait_for(b"legacy/unmanaged")
        process.send(b"a\n")
        process.wait_for(b"LEGACY_READY")
        process.send(b"\x02d")
        self.assertEqual(process.wait(), 0)
        result = self.bash(
            'source "$HUBI_FILE"; printf "STATUS=%s\\n" '
            '"$(agent_status codex "$REPO_NAME")"; stop_agent_now codex "$REPO_NAME"',
            check=False,
            env={**self.env, "REPO_NAME": self.repo_name},
        )
        self.assertIn("LEGACY/UNMANAGED", result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tmux("has-session", "-t", legacy, check=False).returncode, 0)

    def test_laptop_and_phone_keep_second_window_at_largest_size(self) -> None:
        self.start_agent()
        session = self.session_name()
        self.tmux("new-window", "-d", "-t", session, "-n", "second", "--", "sleep", "30")
        self.tmux("select-window", "-t", f"{session}:second")
        laptop = self.spawn_tmux_client(session, size=(40, 120))
        time.sleep(0.4)
        phone = self.spawn_tmux_client(session, readonly=True, size=(12, 40))
        time.sleep(0.5)
        geometry = self.tmux(
            "display-message", "-p", "-t", f"{session}:second", "#{window_width}x#{window_height}"
        ).stdout.strip()
        client_geometry = self.tmux(
            "list-clients", "-t", session, "-F", "#{client_width}x#{client_height}:#{client_readonly}"
        ).stdout.strip()
        width, height = (int(value) for value in geometry.split("x"))
        self.assertGreaterEqual(width, 100, f"window={geometry}, clients={client_geometry}")
        self.assertGreaterEqual(height, 30, f"window={geometry}, clients={client_geometry}")

    def test_repository_escape_forms_are_rejected(self) -> None:
        outside = self.temp / "outside"
        subprocess.run(["git", "init", "-q", str(outside)], check=True)
        (self.repos / "escape-link").symlink_to(outside, target_is_directory=True)
        for argument in ("../outside", str(outside), "escape-link"):
            result = self.bash(
                'source "$HUBI_FILE"; resolve_repo "$ARGUMENT"',
                check=False,
                env={**self.env, "ARGUMENT": argument},
            )
            self.assertEqual(result.returncode, 2, argument)

    def test_claude_permission_arguments_are_separate_argv(self) -> None:
        argv_file = self.temp / "claude-argv"
        argv_agent = self.temp / "argv-agent"
        argv_agent.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$#\" \"$@\" >\"$ARGV_FILE\"\n"
            "echo ARGV_READY\n"
            "trap 'exit 0' INT TERM\n"
            "while :; do sleep 1; done\n"
        )
        argv_agent.chmod(0o755)
        process = PtyProcess(
            [str(HUBI), "claude", self.repo_name],
            {
                **self.env,
                "HUBI_CLAUDE_BIN": str(argv_agent),
                "ARGV_FILE": str(argv_file),
            },
        )
        self.ptys.append(process)
        deadline = time.monotonic() + 5
        while not argv_file.exists() and time.monotonic() < deadline:
            process.read_available()
            time.sleep(0.05)
        process.wait_for(b"ARGV_READY")
        self.assertEqual(
            argv_file.read_text().splitlines(),
            ["2", "--permission-mode", "bypassPermissions"],
        )
        process.send(b"\x02d")
        self.assertEqual(process.wait(), 0)

    def test_exited_session_can_be_stopped_and_restarted(self) -> None:
        first_run = self.temp / "first-run"
        restart_agent = self.temp / "restart-agent"
        restart_agent.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ ! -e \"$FIRST_RUN\" ]]; then : >\"$FIRST_RUN\"; echo FIRST_CRASH; exit 17; fi\n"
            "echo RESTART_RUNNING\n"
            "trap 'exit 0' INT TERM\n"
            "while :; do sleep 1; done\n"
        )
        restart_agent.chmod(0o755)
        env = {
            **self.env,
            "REPO_NAME": self.repo_name,
            "FAKE_AGENT": str(restart_agent),
            "FIRST_RUN": str(first_run),
        }
        first = self.bash(
            'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; '
            'ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$FAKE_AGENT"',
            check=False,
            env=env,
        )
        self.assertNotEqual(first.returncode, 0)
        state = self.bash(
            'source "$HUBI_FILE"; printf "%s" "$(agent_status codex "$REPO_NAME")"',
            env=env,
        ).stdout
        self.assertIn("EXITED", state)
        self.assertEqual(
            self.bash(
                'source "$HUBI_FILE"; stop_agent_now codex "$REPO_NAME"', env=env, check=False
            ).returncode,
            0,
        )
        second = self.bash(
            'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; '
            'ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$FAKE_AGENT"',
            env=env,
            check=False,
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn(
            "RUNNING",
            self.bash(
                'source "$HUBI_FILE"; agent_status codex "$REPO_NAME"', env=env
            ).stdout,
        )

    def test_five_concurrent_starters_converge(self) -> None:
        script = (
            'source "$HUBI_FILE"; resolve_repo "$REPO_NAME"; '
            'ensure_agent_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" "$FAKE_AGENT"'
        )
        env = {
            **self.env,
            "REPO_NAME": self.repo_name,
            "FAKE_AGENT": str(self.fake_agent),
        }
        processes = [
            subprocess.Popen(
                ["bash", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            for _ in range(5)
        ]
        results = [process.communicate(timeout=8) + (process.returncode,) for process in processes]
        self.assertTrue(all(result[2] == 0 for result in results), results)
        sessions = self.tmux("list-sessions", "-F", "#S").stdout.splitlines()
        self.assertEqual(sessions.count(self.session_name()), 1)

    def test_autologin_fragment_recovery_and_bypass(self) -> None:
        home = self.temp / "home"
        binary = home / ".local/bin/hubi"
        binary.parent.mkdir(parents=True)
        fragment = ROOT / "bashrc-autologin.sh"

        missing = subprocess.run(
            ["bash", "--noprofile", "--norc", "-ic", f'source "{fragment}"; echo MISSING_OK'],
            env={**self.env, "HOME": str(home), "SSH_TTY": "/dev/pts/test"},
            text=True,
            capture_output=True,
        )
        self.assertIn("MISSING_OK", missing.stdout)

        called = self.temp / "autologin-called"
        binary.write_text(f"#!/usr/bin/env bash\n: >{called}\nexit 98\n")
        binary.chmod(0o755)
        bypass = subprocess.run(
            ["bash", "--noprofile", "--norc", "-ic", f'source "{fragment}"; echo BYPASS_OK'],
            env={
                **self.env,
                "HOME": str(home),
                "SSH_TTY": "/dev/pts/test",
                "HUBI_NOAUTO": "1",
            },
            text=True,
            capture_output=True,
        )
        self.assertIn("BYPASS_OK", bypass.stdout)
        self.assertFalse(called.exists())

        q_result = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-ic",
                f'unset HUBI_NOAUTO; source "{fragment}"; printf "NOAUTO=%s" "$HUBI_NOAUTO"',
            ],
            env={**self.env, "HOME": str(home), "SSH_TTY": "/dev/pts/test"},
            text=True,
            capture_output=True,
        )
        self.assertIn("NOAUTO=1", q_result.stdout)

        binary.write_text("#!/usr/bin/env bash\nexit 99\n")
        binary.chmod(0o755)
        x_result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-ic", f'source "{fragment}"; echo MUST_NOT_PRINT'],
            env={**self.env, "HOME": str(home), "SSH_TTY": "/dev/pts/test"},
            text=True,
            capture_output=True,
        )
        self.assertNotIn("MUST_NOT_PRINT", x_result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
