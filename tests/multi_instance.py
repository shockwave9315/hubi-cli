#!/usr/bin/env python3
"""Isolated repo + agent + instance lifecycle regressions."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HUBI = ROOT / "hubi"
REAL_TMUX = shutil.which("tmux") or "/usr/bin/tmux"


class MultiInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="hubi-multi-"))
        self.repos = self.temp / "repos"
        self.runtime = self.temp / "runtime"
        self.repos.mkdir()
        self.runtime.mkdir()
        self.runtime.chmod(0o700)
        real_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        (self.runtime / "systemd").symlink_to(Path(real_runtime) / "systemd", target_is_directory=True)
        unique = f"multi{os.getpid()}-{time.time_ns() % 1_000_000_000}"
        self.socket = f"hubi-multi-{unique}"
        self.repo_x = f"{unique}-repo-x"
        self.repo_y = f"{unique}-repo-y"
        for repo in (self.repo_x, self.repo_y):
            subprocess.run(["git", "init", "-q", str(self.repos / repo)], check=True)
        self.tmux_wrapper = self.temp / "tmux-clean"
        self.tmux_wrapper.write_text(f'#!/usr/bin/env bash\nexec {REAL_TMUX} -f /dev/null "$@"\n')
        self.tmux_wrapper.chmod(0o755)
        self.agent = self.temp / "agent"
        self.agent.write_text(
            "#!/usr/bin/env bash\n"
            "echo INSTANCE_READY\n"
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
            "XDG_RUNTIME_DIR": str(self.runtime),
            "TERM": "xterm-256color",
        }
        self.env.pop("TMUX", None)
        self.env.pop("HUBI_ACTIVE", None)
        self.identities: set[tuple[str, str, str]] = set()

    def tearDown(self) -> None:
        result = self.tmux("list-sessions", "-F", "#{@hubi-scope}", check=False)
        scopes = {line for line in result.stdout.splitlines() if line.startswith("hubi-")}
        scopes.update(self.scope(*identity) for identity in self.identities)
        for scope in scopes:
            subprocess.run(
                ["systemctl", "--user", "kill", "--kill-whom=all", "--signal=KILL", scope],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.tmux("kill-server", check=False)
        shutil.rmtree(self.temp, ignore_errors=True)

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:12]

    def session(self, agent: str, repo: str, instance: str = "primary") -> str:
        if instance == "primary":
            return f"hubi-{agent}-{repo[:28]}-{self.digest(repo)}"
        return f"hubi-{agent}-{instance}-{repo[:28]}-{self.digest(f'{agent}:{repo}:{instance}')}"

    def scope(self, agent: str, repo: str, instance: str = "primary") -> str:
        if instance == "primary":
            return f"hubi-{agent}-{self.digest(repo)}.scope"
        return f"hubi-{agent}-{self.digest(repo)}-{instance}-{self.digest(f'{agent}:{repo}:{instance}')}.scope"

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [REAL_TMUX, "-f", "/dev/null", "-L", self.socket, *args],
            text=True,
            capture_output=True,
            check=check,
        )

    def bash(
        self, script: str, env: dict[str, str] | None = None, timeout: float = 10
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", script], env=env or self.env, text=True,
            capture_output=True, timeout=timeout,
        )

    def active(self, identity: tuple[str, str, str]) -> bool:
        return subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", self.scope(*identity)]
        ).returncode == 0

    def start(
        self, identity: tuple[str, str, str], executable: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        agent, repo, instance = identity
        self.identities.add(identity)
        return self.bash(
            'source "$HUBI_FILE"; resolve_repo "$REPO"; '
            'ensure_agent_instance_session "$AGENT" "$RESOLVED_REPO_KEY" '
            '"$RESOLVED_REPO_DIR" "$INSTANCE" "$EXECUTABLE"',
            {
                **self.env, **(extra_env or {}), "AGENT": agent, "REPO": repo,
                "INSTANCE": instance, "EXECUTABLE": str(executable or self.agent),
            },
        )

    def stop(self, identity: tuple[str, str, str]) -> subprocess.CompletedProcess[str]:
        agent, repo, instance = identity
        return self.bash(
            'source "$HUBI_FILE"; stop_agent_now "$AGENT" "$REPO" "$INSTANCE"',
            {**self.env, "AGENT": agent, "REPO": repo, "INSTANCE": instance},
        )

    def status(self, identity: tuple[str, str, str]) -> str:
        agent, repo, instance = identity
        return self.bash(
            'source "$HUBI_FILE"; agent_status "$AGENT" "$REPO" "$INSTANCE"',
            {**self.env, "AGENT": agent, "REPO": repo, "INSTANCE": instance},
        ).stdout

    def assert_started(self, identity: tuple[str, str, str], result=None) -> None:
        result = result or self.start(identity)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 3
        while not self.active(identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.active(identity), identity)

    def test_identity_matrix_and_independent_reconnect(self) -> None:
        identities = [
            ("claude", self.repo_x, "primary"),
            ("claude", self.repo_x, "review"),
            ("codex", self.repo_x, "primary"),
            ("codex", self.repo_x, "review"),
            ("claude", self.repo_y, "review"),
            ("codex", self.repo_x, "implementation"),
        ]
        for identity in identities:
            self.assert_started(identity)
        sessions = [self.session(*identity) for identity in identities]
        scopes = [self.scope(*identity) for identity in identities]
        self.assertEqual(len(sessions), len(set(sessions)))
        self.assertEqual(len(scopes), len(set(scopes)))
        self.assertEqual(set(self.tmux("list-sessions", "-F", "#S").stdout.splitlines()), set(sessions))
        for identity in identities:
            session = self.session(*identity)
            self.assertEqual(
                self.tmux("show-option", "-qv", "-t", session, "@hubi-instance").stdout.strip(),
                identity[2],
            )
            self.assertEqual(
                self.tmux("show-option", "-qv", "-t", session, "@hubi-scope").stdout.strip(),
                self.scope(*identity),
            )
        for identity in identities[:2]:
            subprocess.run(
                ["python3", str(ROOT / "tests/tmux_client.py"), "readonly", self.socket,
                 self.session(*identity), "IGNORED"],
                env=self.env, check=True,
            )
            self.assertTrue(self.active(identity))

    def test_legacy_v4_primary_is_unchanged_by_secondary(self) -> None:
        identity = ("claude", self.repo_x, "primary")
        session = self.session(*identity)
        pane = self.tmux(
            "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session,
            "-c", str(self.repos / self.repo_x), "--", "sleep", "30",
        ).stdout.strip()
        for option, value in (
            ("@hubi-managed", "v4"), ("@hubi-agent", "claude"),
            ("@hubi-repo", self.repo_x), ("@hubi-scope", self.scope(*identity)),
            ("@hubi-pane", pane),
        ):
            self.tmux("set-option", "-t", session, option, value)
        before = self.tmux("show-options", "-t", session).stdout
        found = self.bash(
            'source "$HUBI_FILE"; find_agent_session claude "$REPO" primary; '
            'printf "%s|%s" "$FOUND_SESSION" "$FOUND_SESSION_KIND"',
            {**self.env, "REPO": self.repo_x},
        )
        self.assertEqual(found.stdout, f"{session}|managed")
        secondary = ("claude", self.repo_x, "review")
        self.assert_started(secondary)
        self.assertEqual(self.tmux("show-options", "-t", session).stdout, before)
        self.assertEqual(self.tmux("show-option", "-qv", "-t", session, "@hubi-instance").stdout, "")

    def test_stop_and_orphan_reconciliation_are_isolated(self) -> None:
        primary = ("claude", self.repo_x, "primary")
        review = ("claude", self.repo_x, "review")
        self.assert_started(primary)
        self.assert_started(review)
        self.assertEqual(self.stop(review).returncode, 0)
        self.assertTrue(self.active(primary))
        self.assertEqual(self.tmux("has-session", "-t", self.session(*primary), check=False).returncode, 0)

        orphan_agent = self.temp / "orphan-agent"
        orphan_agent.write_text(
            "#!/usr/bin/env bash\n"
            "setsid bash -c 'trap \"\" HUP INT TERM; while :; do sleep 1; done' &\n"
            "wait\n"
        )
        orphan_agent.chmod(0o755)
        self.assert_started(review, self.start(review, orphan_agent))
        self.tmux("kill-session", "-t", self.session(*review))
        deadline = time.monotonic() + 2
        while not self.active(review) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("ORPHANED", self.status(review))
        self.assertIn("RUNNING", self.status(primary))
        self.assertEqual(self.stop(review).returncode, 0)
        self.assertTrue(self.active(primary))

    def test_capture_status_and_signal_use_exact_instance_pane(self) -> None:
        primary = ("codex", self.repo_x, "primary")
        review = ("codex", self.repo_x, "review")
        self.assert_started(primary)
        self.assert_started(review)
        review_session = self.session(*review)
        self.tmux(
            "new-window", "-d", "-t", review_session, "-n", "other", "--",
            "bash", "-c", "echo WRONG_PANE; while :; do sleep 1; done",
        )
        subprocess.run(
            ["systemctl", "--user", "kill", "--kill-whom=all", "--signal=TERM", self.scope(*review)],
            check=True,
        )
        deadline = time.monotonic() + 3
        while self.active(review) and time.monotonic() < deadline:
            time.sleep(0.05)
        captured = self.bash(
            'source "$HUBI_FILE"; find_agent_session codex "$REPO" review; '
            'printf "STATUS=%s\\n" "$(session_status "$FOUND_SESSION")"; '
            'capture_session_output "$FOUND_SESSION"',
            {**self.env, "REPO": self.repo_x},
        ).stdout
        self.assertIn("EXITED", captured)
        self.assertIn("INSTANCE_READY", captured)
        self.assertNotIn("WRONG_PANE", captured)
        self.assertEqual(self.stop(review).returncode, 0)
        self.assertTrue(self.active(primary))

    def test_same_instance_serializes_while_different_instances_start(self) -> None:
        script = (
            'source "$HUBI_FILE"; resolve_repo "$REPO"; '
            'ensure_agent_instance_session codex "$RESOLVED_REPO_KEY" "$RESOLVED_REPO_DIR" '
            '"$INSTANCE" "$HUBI_CODEX_BIN"'
        )
        instances = ["review"] * 5 + ["implementation", "upstream"]
        for instance in set(instances):
            self.identities.add(("codex", self.repo_x, instance))
        processes = [
            subprocess.Popen(
                ["bash", "-c", script], env={**self.env, "REPO": self.repo_x, "INSTANCE": instance},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for instance in instances
        ]
        results = [process.communicate(timeout=12) + (process.returncode,) for process in processes]
        self.assertTrue(all(result[2] == 0 for result in results), results)
        sessions = self.tmux("list-sessions", "-F", "#S").stdout.splitlines()
        for instance in set(instances):
            self.assertEqual(sessions.count(self.session("codex", self.repo_x, instance)), 1)

    def test_instance_discovery_batches_tmux_metadata_without_show_option(self) -> None:
        call_log = self.temp / "discovery-tmux-calls"
        tmux_batch = self.temp / "tmux-batch"
        no_scopes = self.temp / "systemctl-no-scopes"

        def record(*fields: str) -> str:
            return "".join(f"{len(field.encode())}:{field}" for field in fields)

        records = [
            record("managed-review", "v4", "codex", self.repo_x, "review"),
            record("legacy-primary", "v4", "codex", self.repo_x, ""),
            record("unmanaged", "", "", "", ""),
            record("wrong-agent", "v4", "claude", self.repo_x, "review"),
            record("wrong-repo", "v4", "codex", self.repo_y, "review"),
        ]
        tmux_batch.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
            "if [[ \" $* \" == *\" show-option \"* ]]; then exit 97; fi\n"
            "if [[ \" $* \" == *\" list-sessions \"* ]]; then\n"
            + "".join(f"  printf '%s\\n' '{value}'\n" for value in records)
            + "fi\n"
        )
        tmux_batch.chmod(0o755)
        no_scopes.write_text("#!/usr/bin/env bash\nexit 0\n")
        no_scopes.chmod(0o755)

        result = self.bash(
            'source "$HUBI_FILE"; agent_instance_list codex "$REPO"',
            {
                **self.env,
                "REPO": self.repo_x,
                "CALL_LOG": str(call_log),
                "HUBI_TMUX_BIN": str(tmux_batch),
                "HUBI_SYSTEMCTL_BIN": str(no_scopes),
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["primary", "review"])
        calls = call_log.read_text().splitlines()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("list-sessions", calls[0])
        self.assertNotIn("show-option", calls[0])
        for field in ("session_name", "@hubi-managed", "@hubi-agent", "@hubi-repo", "@hubi-instance"):
            self.assertIn(field, calls[0])

    def test_unsafe_names_and_unmanaged_lookalike_are_never_claimed(self) -> None:
        for name in ("", ".", "..", "has space", "a/b", "line\nbreak", "$(touch nope)", "-bad", "żółw"):
            result = self.bash(
                'source "$HUBI_FILE"; validate_instance_name "$INSTANCE"',
                {**self.env, "INSTANCE": name},
            )
            self.assertEqual(result.returncode, 2, name)
        lookalike = self.session("claude", self.repo_x, "review")
        self.tmux("new-session", "-d", "-s", lookalike, "--", "sleep", "30")
        result = self.bash(
            'source "$HUBI_FILE"; find_agent_session claude "$REPO" review',
            {**self.env, "REPO": self.repo_x},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tmux("has-session", "-t", lookalike, check=False).returncode, 0)

        special_repo = f"{self.repo_x}|żółw"
        subprocess.run(["git", "init", "-q", str(self.repos / special_repo)], check=True)
        identity = ("codex", special_repo, "review")
        self.assert_started(identity)
        listed = self.bash(
            'source "$HUBI_FILE"; agent_instance_list codex "$REPO"',
            {**self.env, "REPO": special_repo},
        )
        self.assertEqual(listed.stdout.splitlines(), ["primary", "review"])

    def test_claude_and_codex_resume_are_managed_and_persistent(self) -> None:
        cases = [
            ("claude", "review", ["--permission-mode", "bypassPermissions", "--resume"]),
            ("codex", "implementation", ["resume"]),
        ]
        for agent, instance, expected in cases:
            argv_file = self.temp / f"{agent}-argv"
            argv_agent = self.temp / f"{agent}-argv-agent"
            argv_agent.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"$@\" >'{argv_file}'\n"
                "trap 'exit 0' INT TERM\nwhile :; do sleep 1; done\n"
            )
            argv_agent.chmod(0o755)
            identity = (agent, self.repo_x, instance)
            self.identities.add(identity)
            result = self.bash(
                'source "$HUBI_FILE"; attach_session() { :; }; '
                'start_agent "$AGENT" "$REPO" "$INSTANCE" resume',
                {
                    **self.env, "AGENT": agent, "REPO": self.repo_x, "INSTANCE": instance,
                    "HUBI_CLAUDE_BIN": str(argv_agent), "HUBI_CODEX_BIN": str(argv_agent),
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            deadline = time.monotonic() + 2
            while not argv_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(argv_file.read_text().splitlines(), expected)
            self.assertTrue(self.active(identity))

    def test_exited_secondary_restarts_and_shell_warning_is_visible(self) -> None:
        marker = self.temp / "first-run"
        agent = self.temp / "crash-once"
        agent.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ ! -e \"$FIRST_RUN\" ]]; then : >\"$FIRST_RUN\"; echo CRASH; exit 17; fi\n"
            "trap 'exit 0' INT TERM\nwhile :; do sleep 1; done\n"
        )
        agent.chmod(0o755)
        identity = ("claude", self.repo_x, "old-sonnet")
        first = self.start(identity, agent, {"FIRST_RUN": str(marker)})
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("EXITED", self.status(identity))
        self.assertEqual(self.stop(identity).returncode, 0)
        self.assert_started(identity, self.start(identity, agent, {"FIRST_RUN": str(marker)}))
        warning = subprocess.run(
            [str(HUBI), "shell", self.repo_x], input="exit\n", env=self.env,
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(warning.returncode, 0)
        self.assertIn("Tymczasowy shell", warning.stdout)
        self.assertIn("mogą zakończyć się po rozłączeniu SSH", warning.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
