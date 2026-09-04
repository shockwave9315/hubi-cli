#!/usr/bin/env python3
"""Regression coverage for the identity/ownership/lifecycle remediation batch.

Each test below is scoped to one or a small cluster of related audit
findings (P1-01..P1-03, P2-01..P2-08, P3-01) and exercises it against a
fully disposable tmux socket, systemd scope namespace, and private
HUBI_RUNTIME_DIR — never the caller's real ones (P3-02).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HUBI = ROOT / "hubi"
REAL_TMUX = shutil.which("tmux") or "/usr/bin/tmux"


class LifecycleHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="hubi-hardening-"))
        self.repos = self.temp / "repos"
        self.runtime = self.temp / "hubi-runtime"
        self.repos.mkdir()
        self.runtime.mkdir(mode=0o700)
        unique = f"hard{os.getpid()}-{time.time_ns() % 1_000_000_000}"
        self.socket = f"hubi-hardening-{unique}"
        self.repo_name = f"{unique}-repo"
        self.repo = self.repos / self.repo_name
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.tmux_wrapper = self.temp / "tmux-clean"
        self.tmux_wrapper.write_text(f'#!/usr/bin/env bash\nexec {REAL_TMUX} -f /dev/null "$@"\n')
        self.tmux_wrapper.chmod(0o755)
        self.agent = self.temp / "agent"
        self.agent.write_text(
            "#!/usr/bin/env bash\n"
            "echo AGENT_READY\n"
            "trap 'exit 0' INT TERM\n"
            "while :; do sleep 1; done\n"
        )
        self.agent.chmod(0o755)
        self.env = {
            **os.environ,
            "HUBI_REPOS": str(self.repos),
            "HUBI_TMUX_SOCKET": self.socket,
            "HUBI_TMUX_BIN": str(self.tmux_wrapper),
            "HUBI_CODEX_BIN": str(self.agent),
            "HUBI_CLAUDE_BIN": str(self.agent),
            "HUBI_FILE": str(HUBI),
            "HUBI_RUNTIME_DIR": str(self.runtime),
            "TERM": "xterm-256color",
        }
        self.env.pop("TMUX", None)
        self.env.pop("HUBI_ACTIVE", None)
        self.created_scopes: set[str] = set()

    def tearDown(self) -> None:
        result = self.tmux("list-sessions", "-F", "#{@hubi-scope}", check=False)
        scopes = {line for line in result.stdout.splitlines() if line.startswith("hubi-")}
        scopes |= self.created_scopes
        for scope in scopes:
            subprocess.run(
                ["systemctl", "--user", "kill", "--kill-whom=all", "--signal=KILL", scope],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        self.tmux("kill-server", check=False)
        try:
            (Path(f"/tmp/tmux-{os.getuid()}") / self.socket).unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(self.temp, ignore_errors=True)

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [REAL_TMUX, "-f", "/dev/null", "-L", self.socket, *args],
            text=True, capture_output=True, check=check,
        )

    def bash(self, script: str, env: dict[str, str] | None = None, timeout: float = 10,
              check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", script], env=env or self.env, text=True,
            capture_output=True, timeout=timeout, check=check,
        )

    def bash_value(self, script: str, env: dict[str, str] | None = None) -> str:
        return self.bash(script, env=env).stdout.strip()

    def session_name(self, agent: str, canonical: str, instance: str = "primary") -> str:
        return self.bash_value(
            'source "$HUBI_FILE"; agent_session_name "$AGENT" "$CANON" "$INSTANCE"',
            {**self.env, "AGENT": agent, "CANON": canonical, "INSTANCE": instance},
        )

    def scope_name(self, agent: str, canonical: str, instance: str = "primary") -> str:
        return self.bash_value(
            'source "$HUBI_FILE"; agent_scope_name "$AGENT" "$CANON" "$INSTANCE"',
            {**self.env, "AGENT": agent, "CANON": canonical, "INSTANCE": instance},
        )

    def start(self, canonical: str, agent: str = "codex", instance: str = "primary",
               env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.bash(
            'source "$HUBI_FILE"; RESOLVED_REPO_ID="$(stat -Lc "%d:%i" -- "$CANON")"; '
            'ensure_agent_instance_session "$AGENT" "$CANON" "$INSTANCE" "$HUBI_CODEX_BIN"',
            {**(env or self.env), "AGENT": agent, "CANON": canonical, "INSTANCE": instance},
        )

    def scope_active(self, scope: str) -> bool:
        return subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", scope]
        ).returncode == 0

    # -----------------------------------------------------------------
    # P1-01 / P2-07: identity is rooted in the canonical path + structured
    # encoding, not a bare relative key. Covered directly in tests/run.sh
    # (test_repo_root_collision, test_tuple_ambiguity) against the real
    # hash formula; here we add the corresponding lifecycle-level check
    # that two colliding *relative* names started under different roots
    # never share tmux/scope state even while both are live.
    # -----------------------------------------------------------------
    def test_cross_root_identities_run_independently(self) -> None:
        root_a = self.repos / "root-a"
        root_b = self.repos / "root-b"
        root_a.mkdir()
        root_b.mkdir()
        subprocess.run(["git", "init", "-q", str(root_a / "shared")], check=True)
        subprocess.run(["git", "init", "-q", str(root_b / "shared")], check=True)
        env_a = {**self.env, "HUBI_REPOS": str(root_a)}
        env_b = {**self.env, "HUBI_REPOS": str(root_b)}
        started_a = self.start(str(root_a / "shared"), env=env_a)
        started_b = self.start(str(root_b / "shared"), env=env_b)
        self.assertEqual(started_a.returncode, 0, started_a.stdout + started_a.stderr)
        self.assertEqual(started_b.returncode, 0, started_b.stdout + started_b.stderr)
        session_a = self.session_name("codex", str(root_a / "shared"))
        session_b = self.session_name("codex", str(root_b / "shared"))
        self.created_scopes.add(self.scope_name("codex", str(root_a / "shared")))
        self.created_scopes.add(self.scope_name("codex", str(root_b / "shared")))
        self.assertNotEqual(session_a, session_b)
        self.assertEqual(self.tmux("has-session", "-t", session_a, check=False).returncode, 0)
        self.assertEqual(self.tmux("has-session", "-t", session_b, check=False).returncode, 0)
        # Stopping one must never touch the other.
        stop_a = self.bash(
            'source "$HUBI_FILE"; stop_agent_now codex "$CANON" primary',
            {**env_a, "CANON": str(root_a / "shared")},
        )
        self.assertEqual(stop_a.returncode, 0, stop_a.stdout + stop_a.stderr)
        self.assertEqual(self.tmux("has-session", "-t", session_b, check=False).returncode, 0)

    # -----------------------------------------------------------------
    # P1-02: repository replacement at the same canonical path.
    # -----------------------------------------------------------------
    def test_repository_replaced_after_start_is_conflict_not_silent_reuse(self) -> None:
        canonical = str(self.repo)
        started = self.start(canonical)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        session = self.session_name("codex", canonical)
        scope = self.scope_name("codex", canonical)
        self.created_scopes.add(scope)
        deadline = time.monotonic() + 3
        while not self.scope_active(scope) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.scope_active(scope))

        # Replace the repository object at the exact same canonical path.
        shutil.rmtree(self.repo)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

        status = self.bash(
            'source "$HUBI_FILE"; agent_status codex "$CANON" primary',
            {**self.env, "CANON": canonical},
        ).stdout
        self.assertIn("CONFLICT", status)

        # find_agent_session must refuse to hand back the old agent as if it
        # belonged to the replacement repository.
        found = self.bash(
            'source "$HUBI_FILE"; find_agent_session codex "$CANON" primary; echo "rc=$?"',
            {**self.env, "CANON": canonical},
        )
        self.assertIn("rc=3", found.stdout)

        # The old agent must still be genuinely alive underneath — this is a
        # refusal to manage, not an accidental teardown of the running agent.
        self.assertTrue(self.scope_active(scope))
        self.assertEqual(self.tmux("has-session", "-t", session, check=False).returncode, 0)

    # -----------------------------------------------------------------
    # P1-03: systemctl kill failure must not be reported as a successful
    # stop, and must not remove tmux evidence.
    # -----------------------------------------------------------------
    def test_systemctl_kill_failure_does_not_claim_success_or_delete_evidence(self) -> None:
        canonical = str(self.repo)
        # Ignores Ctrl+C/TERM so terminate_scope cannot succeed "for free"
        # via the agent exiting on its own — the (fault-injected) kill path
        # must actually be exercised.
        stubborn_agent = self.temp / "stubborn-agent"
        stubborn_agent.write_text(
            "#!/usr/bin/env bash\ntrap '' INT TERM\nwhile :; do sleep 1; done\n"
        )
        stubborn_agent.chmod(0o755)
        started = self.start(canonical, env={**self.env, "HUBI_CODEX_BIN": str(stubborn_agent)})
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        session = self.session_name("codex", canonical)
        scope = self.scope_name("codex", canonical)
        self.created_scopes.add(scope)

        broken = self.temp / "systemctl-kill-fails"
        broken.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == kill && \"${2-}\" == --help ]]; then\n"
            "  echo '--kill-whom=WHOM --signal=SIGNAL'; exit 0\n"
            "fi\n"
            "if [[ \"$1\" == --user && \"${2-}\" == kill ]]; then\n"
            "  echo 'simulated dbus failure' >&2; exit 1\n"
            "fi\n"
            "exec /usr/bin/systemctl \"$@\"\n"
        )
        broken.chmod(0o755)

        result = self.bash(
            'source "$HUBI_FILE"; stop_agent_now codex "$CANON" primary',
            {**self.env, "CANON": canonical, "HUBI_SYSTEMCTL_BIN": str(broken)},
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        # Evidence must be preserved: tmux session and the (still-active,
        # kill genuinely failed) scope remain exactly as they were.
        self.assertEqual(self.tmux("has-session", "-t", session, check=False).returncode, 0)
        self.assertTrue(self.scope_active(scope))

    # -----------------------------------------------------------------
    # P2-02: transactional bootstrap — representative failure-injection
    # points must each leave ABSENT (no session, no scope, no COMMITTED
    # record), never a partially-configured live object.
    # -----------------------------------------------------------------
    def assert_fully_rolled_back(self, canonical: str, agent: str = "codex") -> None:
        session = self.session_name(agent, canonical)
        scope = self.scope_name(agent, canonical)
        self.assertEqual(self.tmux("has-session", "-t", session, check=False).returncode, 1)
        self.assertFalse(self.scope_active(scope))

    def test_rollback_on_set_option_failure(self) -> None:
        canonical = str(self.repo)
        wrapper = self.temp / "tmux-fail-set-option"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \" $* \" == *\" set-option \"* && \" $* \" == *\"@hubi-managed\"* ]]; then exit 9; fi\n"
            f"exec {REAL_TMUX} -f /dev/null \"$@\"\n"
        )
        wrapper.chmod(0o755)
        result = self.start(canonical, env={**self.env, "HUBI_TMUX_BIN": str(wrapper)})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_fully_rolled_back(canonical)

    def test_rollback_on_respawn_pane_failure(self) -> None:
        canonical = str(self.repo)
        wrapper = self.temp / "tmux-fail-respawn"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \" $* \" == *\" respawn-pane \"* ]]; then echo boom >&2; exit 3; fi\n"
            f"exec {REAL_TMUX} -f /dev/null \"$@\"\n"
        )
        wrapper.chmod(0o755)
        result = self.start(canonical, env={**self.env, "HUBI_TMUX_BIN": str(wrapper)})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_fully_rolled_back(canonical)

    def test_rollback_on_cwd_mismatch_after_creation(self) -> None:
        canonical = str(self.repo)
        wrapper = self.temp / "tmux-wrong-cwd"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \" $* \" == *\" display-message \"* && \" $* \" == *\"pane_current_path\"* ]]; then\n"
            "  printf '/nonexistent-mismatch\\n'; exit 0\n"
            "fi\n"
            f"exec {REAL_TMUX} -f /dev/null \"$@\"\n"
        )
        wrapper.chmod(0o755)
        result = self.start(canonical, env={**self.env, "HUBI_TMUX_BIN": str(wrapper)})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_fully_rolled_back(canonical)

    def test_rollback_on_hubi_active_leak(self) -> None:
        canonical = str(self.repo)
        wrapper = self.temp / "tmux-leak-env"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \" $* \" == *\" show-environment \"* && \" $* \" == *\"HUBI_ACTIVE\"* ]]; then\n"
            "  printf 'HUBI_ACTIVE=1\\n'; exit 0\n"
            "fi\n"
            f"exec {REAL_TMUX} -f /dev/null \"$@\"\n"
        )
        wrapper.chmod(0o755)
        result = self.start(canonical, env={**self.env, "HUBI_TMUX_BIN": str(wrapper)})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HUBI_ACTIVE", result.stdout + result.stderr)
        self.assert_fully_rolled_back(canonical)

    def test_fast_crash_after_respawn_is_committed_exited_not_rolled_back(self) -> None:
        # The one intentional exception: a failed respawn-pane rolls back,
        # but an agent that started and then exited immediately is a
        # legitimate EXITED object, not a failed transaction.
        canonical = str(self.repo)
        crash_agent = self.temp / "crash-agent"
        crash_agent.write_text("#!/usr/bin/env bash\necho CRASH\nexit 9\n")
        crash_agent.chmod(0o755)
        result = self.bash(
            'source "$HUBI_FILE"; RESOLVED_REPO_ID="$(stat -Lc "%d:%i" -- "$CANON")"; '
            'ensure_agent_instance_session codex "$CANON" primary "$CRASH_AGENT"',
            {**self.env, "CANON": canonical, "CRASH_AGENT": str(crash_agent)},
        )
        self.assertNotEqual(result.returncode, 0)
        session = self.session_name("codex", canonical)
        self.assertEqual(self.tmux("has-session", "-t", session, check=False).returncode, 0)
        status = self.bash(
            'source "$HUBI_FILE"; agent_status codex "$CANON" primary',
            {**self.env, "CANON": canonical},
        ).stdout
        self.assertIn("EXITED", status)
        self.assertNotIn("CONFLICT", status)

    # -----------------------------------------------------------------
    # P2-05: a catchable signal delivered to the launcher alone (not its
    # process group) during a genuine, non-artificial startup must cancel
    # bounded and roll back through the trusted STARTING record.
    # -----------------------------------------------------------------
    def test_parent_only_catchable_signal_rolls_back_via_trusted_record(self) -> None:
        canonical = str(self.repo)
        marker = self.temp / "respawn-reached"
        wrapper = self.temp / "tmux-stall-respawn"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \" $* \" == *\" respawn-pane \"* ]]; then\n"
            f"  : >'{marker}'\n"
            "  sleep 2\n"
            "fi\n"
            f"exec {REAL_TMUX} -f /dev/null \"$@\"\n"
        )
        wrapper.chmod(0o755)
        env = {**self.env, "HUBI_TMUX_BIN": str(wrapper)}
        process = subprocess.Popen(
            [str(HUBI), "codex", self.repo_name],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "startup did not reach respawn-pane")

            canon_hash = self.bash_value(
                'source "$HUBI_FILE"; compute_identity_hash "$CANON" codex primary',
                {**self.env, "CANON": canonical},
            )
            # A STARTING record must already exist (written before respawn-pane).
            diagnosis = self.bash_value(
                'source "$HUBI_FILE"; trusted_record_diagnosis "$HASH"',
                {**self.env, "HASH": canon_hash},
            )
            self.assertEqual(diagnosis, "STARTING")

            started = time.monotonic()
            # Parent-only: os.kill targets exactly this pid, never the group
            # the worker/flock supervisor also lives in.
            os.kill(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
            elapsed = time.monotonic() - started
        finally:
            if process.stdout is not None:
                process.stdout.close()
        self.assertLess(elapsed, 4, "cancellation was not bounded")
        self.assertEqual(process.returncode, 143)

        # Give any orphaned grandchild (the trap can only kill the flock
        # supervisor directly, not chase its own descendants — the record is
        # the actual rollback mechanism) a moment to settle, then assert the
        # identity is fully back to ABSENT.
        time.sleep(2.5)
        session = self.session_name("codex", canonical)
        scope = self.scope_name("codex", canonical)
        self.assertEqual(self.tmux("has-session", "-t", session, check=False).returncode, 1)
        self.assertFalse(self.scope_active(scope))
        listed = self.bash_value('source "$HUBI_FILE"; list_trusted_records', self.env)
        self.assertNotIn(canon_hash, listed.splitlines())

    # -----------------------------------------------------------------
    # P2-03: predictable-name tmux lookalikes must never be destroyed by
    # name/marker alone.
    # -----------------------------------------------------------------
    def test_lookalike_matrix_never_destroyed(self) -> None:
        canonical = str(self.repo)
        session = self.session_name("codex", canonical)
        cases = {
            "no-metadata": [],
            "sleep30-only": [],
            "managed-marker-only": [("@hubi-managed", "v4")],
            "partial-metadata": [("@hubi-managed", "v4"), ("@hubi-agent", "codex")],
            "wrong-agent": [("@hubi-managed", "v4"), ("@hubi-agent", "claude"), ("@hubi-token", "x")],
            "wrong-instance": [
                ("@hubi-managed", "v4"), ("@hubi-agent", "codex"),
                ("@hubi-instance", "other"), ("@hubi-token", "x"),
            ],
            "forged-full-metadata": [
                ("@hubi-managed", "v4"), ("@hubi-agent", "codex"), ("@hubi-instance", ""),
                ("@hubi-repo-canon", canonical), ("@hubi-token", "guessed-not-the-real-token"),
            ],
        }
        for label, options in cases.items():
            with self.subTest(case=label):
                self.tmux("new-session", "-d", "-s", session, "--", "sleep", "30")
                for option, value in options:
                    self.tmux("set-option", "-t", session, option, value)
                result = self.start(canonical)
                self.assertNotEqual(result.returncode, 0, (label, result.stdout, result.stderr))
                self.assertIn("CONFLICT", result.stdout + result.stderr, label)
                self.assertEqual(
                    self.tmux("has-session", "-t", session, check=False).returncode, 0, label
                )
                self.tmux("kill-session", "-t", session, check=False)

    # -----------------------------------------------------------------
    # P2-04: a scope with the exact predictable computed name, but never
    # created by Hubi (no trusted record references it), must survive.
    # -----------------------------------------------------------------
    def test_unrelated_scope_with_matching_name_survives(self) -> None:
        canonical = str(self.repo)
        scope = self.scope_name("codex", canonical)
        proc = subprocess.Popen(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={scope}",
             "--", "sleep", "60"],
        )
        self.created_scopes.add(scope)
        deadline = time.monotonic() + 3
        while not self.scope_active(scope) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.scope_active(scope), "precondition: unrelated scope must be active")

        result = self.bash(
            'source "$HUBI_FILE"; kill_agent codex "$CANON" primary',
            {**self.env, "CANON": canonical},
        )
        # No tmux session exists for this identity, so kill_agent's only
        # path to the scope is by name — which alone must not authorize
        # termination without a verified trusted record.
        self.assertTrue(self.scope_active(scope), "unrelated scope must survive Hubi lifecycle")
        subprocess.run(["systemctl", "--user", "kill", "--kill-whom=all", "--signal=KILL", scope],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait(timeout=3)

    # -----------------------------------------------------------------
    # P2-08 / P3-03: a repository that no longer resolves must still be
    # safely inspectable and stoppable through the stale-object path,
    # without weakening ownership checks.
    # -----------------------------------------------------------------
    def test_stale_repository_remains_stoppable_via_trusted_state(self) -> None:
        moved_root = self.temp / "moved-repos"
        moved_root.mkdir()
        canonical = str(self.repo)
        started = self.start(canonical)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        session = self.session_name("codex", canonical)
        scope = self.scope_name("codex", canonical)
        self.created_scopes.add(scope)
        deadline = time.monotonic() + 3
        while not self.scope_active(scope) and time.monotonic() < deadline:
            time.sleep(0.05)

        # The repository disappears from HUBI_REPOS entirely (deleted).
        shutil.rmtree(self.repo)

        hash_value = self.bash_value(
            'source "$HUBI_FILE"; compute_identity_hash "$CANON" codex primary',
            {**self.env, "CANON": canonical},
        )
        listed = self.bash_value('source "$HUBI_FILE"; list_trusted_records', self.env)
        self.assertIn(hash_value, listed.splitlines())

        diagnosis = self.bash_value(
            'source "$HUBI_FILE"; trusted_record_diagnosis "$HASH"',
            {**self.env, "HASH": hash_value},
        )
        self.assertIn("STALE", diagnosis)

        stop = self.bash(
            'source "$HUBI_FILE"; stop_trusted_record "$HASH"',
            {**self.env, "HASH": hash_value},
        )
        self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
        self.assertFalse(self.scope_active(scope))
        self.assertEqual(self.tmux("has-session", "-t", session, check=False).returncode, 1)

    # -----------------------------------------------------------------
    # P3-01: state_root gets the same secure-directory validation as
    # lock_root, uniformly, not only the /tmp fallback path.
    # -----------------------------------------------------------------
    def test_state_root_rejects_symlink_and_unsafe_mode(self) -> None:
        real = self.temp / "real-state-base"
        real.mkdir(mode=0o700)
        symlinked_base = self.temp / "symlinked-runtime-base"
        symlinked_base.symlink_to(real, target_is_directory=True)
        result = self.bash(
            'source "$HUBI_FILE"; state_root',
            {**self.env, "HUBI_RUNTIME_DIR": str(symlinked_base)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

        unsafe_mode_base = self.temp / "unsafe-mode-runtime-base"
        unsafe_mode_base.mkdir(mode=0o755)
        result = self.bash(
            'source "$HUBI_FILE"; state_root',
            {**self.env, "HUBI_RUNTIME_DIR": str(unsafe_mode_base)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

        safe_base = self.temp / "fresh-safe-runtime-base"
        result = self.bash(
            'source "$HUBI_FILE"; state_root',
            {**self.env, "HUBI_RUNTIME_DIR": str(safe_base)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(safe_base / "state"))

    # -----------------------------------------------------------------
    # P2-03/P2-08: a malformed trusted record (bad content, wrong mode, or a
    # symlink standing in for the record file) must never be treated as
    # ownership proof, and must not silently authorize anything.
    # -----------------------------------------------------------------
    def test_malformed_state_record_is_never_trusted(self) -> None:
        state_dir = self.bash_value('source "$HUBI_FILE"; state_root', self.env)
        self.assertTrue(state_dir)
        hash_value = self.bash_value(
            'source "$HUBI_FILE"; compute_identity_hash "$CANON" codex primary',
            {**self.env, "CANON": str(self.repo)},
        )
        record_path = Path(state_dir) / f"{hash_value}.rec"

        # Garbage content, correct permissions.
        record_path.write_text("this is not key=value data at all\n")
        record_path.chmod(0o600)
        result = self.bash(
            'source "$HUBI_FILE"; load_state_record "$HASH"; echo "rc=$?"',
            {**self.env, "HASH": hash_value},
        )
        self.assertIn("rc=2", result.stdout)

        # Well-formed content, but world-readable (unsafe mode).
        record_path.write_text(
            "schema=1\nagent=codex\ninstance=primary\ncanonical_path=/x\n"
            "repo_dev_inode=1:1\nsession=s\npane=%1\nscope=sc.scope\n"
            "token=deadbeef\nstate=COMMITTED\ncreated=1\ncommitted=1\n"
        )
        record_path.chmod(0o644)
        result = self.bash(
            'source "$HUBI_FILE"; load_state_record "$HASH"; echo "rc=$?"',
            {**self.env, "HASH": hash_value},
        )
        self.assertIn("rc=2", result.stdout)
        record_path.unlink()

        # A symlink standing in for the record file.
        target = self.temp / "record-symlink-target"
        target.write_text("schema=1\nstate=COMMITTED\n")
        record_path.symlink_to(target)
        result = self.bash(
            'source "$HUBI_FILE"; load_state_record "$HASH"; echo "rc=$?"',
            {**self.env, "HASH": hash_value},
        )
        self.assertIn("rc=2", result.stdout)
        record_path.unlink()

        # None of this authorized starting a real agent at this identity to
        # somehow inherit the forged/malformed session or scope names.
        status = self.bash(
            'source "$HUBI_FILE"; agent_status codex "$CANON" primary',
            {**self.env, "CANON": str(self.repo)},
        ).stdout
        self.assertIn("STOPPED", status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
