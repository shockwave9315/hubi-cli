"""Home workspace: sessions outside any repository. Pure logic anywhere; lifecycle in the lab."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from hubi6 import menu
from hubi6.adapters.git import Git, directory_reference, health, pin_workspace
from hubi6.adapters.tmux import label
from hubi6.config import Config
from hubi6.core.model import HubiError, Record, RepoHealth, RepoRef, Session, State, Workload, key_id
from hubi6.services import App
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_menu import FakeApp, FakeGit, plain, snapshot, workload
from tests_v6.test_runtime import Client, wait_for

ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
HOME = RepoRef(str(ACCOUNT_HOME), 9, 99, "", 0, 0)


class NoSubprocess:
    calls = 0

    def run(self, *args, **kwargs):
        raise AssertionError("the home workspace needs no subprocess")


def tree(path):
    """Every name, type and mtime below path: proof that nothing was written."""
    out = []
    for root, dirs, files in os.walk(path):
        for name in sorted(dirs+files):
            p = os.path.join(root, name)
            s = os.lstat(p)
            out.append((os.path.relpath(p, path), s.st_mode, s.st_mtime_ns))
    return sorted(out)


def home_workload(state=State.RUNNING, created=None, instance="primary", kind="terminal"):
    r = Record("a"*20, os.getuid(), kind, instance, key_id(kind, HOME.path, instance), "d"*16, HOME,
               created=created or time.time(), pane="%7", invocation="c"*32, phase="running")
    s = Session(r.session, "$7", "%7", state == State.EXITED, 1, 0, r.encoded())
    return Workload(r, state, RepoHealth.OK, s, None, [])


class HomeConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.toml"
            path.write_text(text)
            return Config.load(str(path))

    def test_default_explicit_and_disabled(self):
        self.assertEqual(self.load("").home_workspace, ACCOUNT_HOME)
        self.assertEqual(self.load('home_workspace = "~"').home_workspace, ACCOUNT_HOME)
        self.assertEqual(self.load('home_workspace = "~/scratch"').home_workspace, ACCOUNT_HOME/"scratch")
        self.assertEqual(self.load('home_workspace = "/srv"').home_workspace, Path("/srv").resolve())
        self.assertIsNone(self.load('home_workspace = ""').home_workspace)

    def test_home_workspace_must_lie_outside_repos_root(self):
        for text in ('repos_root = "/srv/r"\nhome_workspace = "/srv/r/x"', 'repos_root = "/srv/r"\nhome_workspace = "/srv/r"',
                     'repos_root = "~"\nhome_workspace = "~"', 'home_workspace = "~/repos/alpha"'):
            with self.subTest(text=text), self.assertRaisesRegex(HubiError, "outside repos_root"):
                self.load(text)
        self.assertIsNone(self.load('repos_root = "~"').home_workspace)    # the default switches off instead
        self.assertEqual(self.load('repos_root = "~/code"\nhome_workspace = "~/scratch"').home_workspace, ACCOUNT_HOME/"scratch")

    def test_invalid_values_are_actionable_errors(self):
        for text in ('home_workspace = "relative/dir"', 'home_workspace = 5', 'home_workspace = "/tmp/a\\nb"',
                     'home_workspace = ["~"]'):
            with self.subTest(text=text), self.assertRaises(HubiError):
                self.load(text)


class HomeCommandLineTests(unittest.TestCase):
    def test_only_the_terminal_defaults_to_the_home_workspace(self):
        from hubi6.cli import parser
        self.assertIsNone(parser().parse_args(["terminal"]).repo)
        self.assertEqual(parser().parse_args(["terminal", "alpha", "x"]).instance, "x")
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as stop:
                parser().parse_args([kind])               # `hubi claude $REPO` with an unset REPO
            self.assertEqual(stop.exception.code, 2)
            self.assertEqual(parser().parse_args([kind, "~"]).repo, "~")

    def test_agents_never_start_without_an_explicit_place(self):
        app = App(Config.load())
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind), self.assertRaisesRegex(HubiError, "explicit repository"):
                app.start(kind, None)                     # refused before any filesystem or runtime access
        git = Git(Config.load(), NoSubprocess())
        with self.assertRaisesRegex(HubiError, "empty"):
            git.resolve("")                               # `hubi claude "$REPO"` with an empty REPO


class HomeRecordTests(unittest.TestCase):
    def record(self):
        return Record("a"*20, os.getuid(), "terminal", "primary", key_id("terminal", HOME.path, "primary"), "b"*16, HOME)

    def test_plain_marker_round_trips_with_the_unchanged_schema(self):
        r = self.record()
        self.assertTrue(r.repo.plain)
        self.assertEqual(set(r.data()["repo"]), set(RepoRef.__dataclass_fields__))
        self.assertEqual(Record.decode(r.encoded(), r.runtime, os.getuid()), r)
        self.assertEqual(Workload(r, State.RUNNING, RepoHealth.OK, None, None).data()["workspace"], "home")
        repo_record = workload("codex", "primary", "alpha", State.RUNNING, created=1)
        self.assertEqual(repo_record.data()["workspace"], "repo")

    def test_inconsistent_plain_or_git_fields_are_refused(self):
        r = self.record()
        for change in ({"git_inode": 3}, {"git_device": 1}, {"origin": "github.com/a/b"}, {"workspace_id": "c"*32},
                       {"git_dir": 0}, {"git_dir": "relative/.git", "git_inode": 3}):
            data = r.data()
            data["repo"] = dict(data["repo"], **change)
            with self.subTest(change=change), self.assertRaises(HubiError):
                Record.parse(data, r.runtime, os.getuid())


class HomeGitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.repos = self.root/"home", self.root/"repos"
        self.home.mkdir()
        (self.repos/"alpha").mkdir(parents=True)
        subprocess.run(["git", "-C", str(self.repos/"alpha"), "init", "-q"], check=True)
        self.c = replace(Config.load(), repos=self.repos, home_workspace=self.home)
        self.git = Git(self.c, NoSubprocess())

    def test_every_spelling_resolves_to_the_plain_home_without_git(self):
        link = self.root/"link"
        link.symlink_to(self.home)
        for value in (None, str(self.home), str(self.home)+"/", str(self.home/"."), str(link)):
            with self.subTest(value=value):
                ref = self.git.resolve(value)
                self.assertTrue(ref.plain)
                self.assertEqual(ref.path, str(self.home))
                self.assertEqual(health(ref), RepoHealth.OK)
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(self.git.resolve("~").path, str(self.home))

    def test_health_follows_the_directory_identity(self):
        ref = self.git.home()
        self.assertEqual(health(ref), RepoHealth.OK)
        self.home.rename(self.root/"moved")
        self.assertEqual(health(ref), RepoHealth.MISSING)
        self.home.mkdir()
        self.assertEqual(health(ref), RepoHealth.REPLACED)
        with self.assertRaises(HubiError):
            self.git.validate(ref)                  # a replaced home never starts a session
        self.assertEqual(health(self.git.home()), RepoHealth.OK)

    def test_only_the_configured_directory_is_a_valid_plain_workspace(self):
        other = self.root/"other"
        other.mkdir()
        with self.assertRaises(HubiError):
            self.git.validate(directory_reference(other))
        with self.assertRaises(HubiError):
            self.git.summary(self.git.home())
        (self.root/"file").write_text("x")
        with self.assertRaises(HubiError):
            directory_reference(self.root/"file")

    def test_disabled_home_workspace(self):
        git = Git(replace(self.c, home_workspace=None), NoSubprocess())
        for call in (git.home, lambda: git.resolve(None)):
            with self.assertRaises(HubiError):
                call()
        self.assertFalse(git.is_home(self.home))
        with self.assertRaises(HubiError):             # now it is just a directory that is not a repository
            git.resolve(str(self.home))

    def test_dotfiles_repository_in_home_is_still_plain_and_untouched(self):
        subprocess.run(["git", "-C", str(self.home), "init", "-q"], check=True)
        before = tree(self.home)
        ref = self.git.resolve(None)
        self.assertTrue(ref.plain)
        self.assertEqual(pin_workspace(ref), ref)
        self.assertEqual(tree(self.home), before)
        self.assertFalse(list(self.home.rglob("hubi6-workspace-id")))
        self.assertEqual([Path(r.path).name for r in self.git.discover()[0]], ["alpha"])

    def test_repositories_are_unchanged(self):
        top = str(self.repos/"alpha")+"\n"
        git = Git(self.c, type("R", (), {"run": lambda runner, argv, **kw: subprocess.CompletedProcess(argv, 0, top, "")})())
        ref = git.resolve("alpha")
        self.assertFalse(ref.plain)
        self.assertEqual(ref.path, str(self.repos/"alpha"))


class HomeGitFake(FakeGit):
    def home(self):
        return HOME


class HomeMenuTests(unittest.TestCase):
    def make(self, workloads=(), home=True):
        rd, wr = os.pipe()
        self.addCleanup(os.close, rd)
        self.addCleanup(os.close, wr)
        self.wr = wr

        class Stream(io.StringIO):
            def fileno(self):
                raise OSError()
        app = FakeApp(workloads)
        if home:
            app.git = HomeGitFake()
        m = menu.Menu(app, menu.Keys(rd), menu.Screen(Stream()))
        m.refresh()
        return m, app

    def press(self, m, *keys):
        for key in keys:
            with patch("hubi6.tui.sys.stdout", io.StringIO()):
                m.handle(key)
            m.refresh()
            m.draw()

    def text(self, m):
        return plain(m.screen.stream.getvalue())

    def test_terminal_then_h_starts_in_home_and_attaches(self):
        m, app = self.make()
        self.press(m, "t", "h")
        started = app.workloads[-1].record
        self.assertEqual(app.calls, [("start", "terminal", HOME.path, "primary", False), ("attach", started.id, "normal")])
        self.assertEqual(m.ui.screen, "main")

    def test_agents_and_resume_in_home(self):
        m, app = self.make()
        self.press(m, "l", "r", "h")
        self.assertEqual(app.calls[0], ("start", "claude", HOME.path, "primary", True))
        self.press(m, "c", "h")
        self.assertEqual(app.calls[2], ("start", "codex", HOME.path, "primary", False))

    def test_second_home_terminal_gets_the_next_free_name(self):
        m, app = self.make([home_workload()])
        self.press(m, "t", "h")
        self.assertEqual(app.calls[0], ("start", "terminal", HOME.path, "2", False))

    def test_home_view_has_no_git_and_starts_sessions(self):
        m, app = self.make([home_workload()])
        self.press(m, "h")
        self.assertEqual((m.ui.screen, m.ui.repo.path), ("repo", HOME.path))
        text = self.text(m)
        self.assertIn("KATALOG DOMOWY ~", text)
        self.assertIn("Sesje w katalogu domowym", text)
        self.assertNotIn("status Gita", text)
        self.press(m, "s")
        self.assertIn("nie jest repozytorium Git", m.ui.message)
        self.assertEqual(app.git.summaries, 0)
        self.press(m, "1")
        self.assertEqual(app.calls[-1][0], "attach")
        self.press(m, "c")
        self.assertEqual(app.calls[-2], ("start", "codex", HOME.path, "primary", False))

    def test_pick_screen_and_repo_view_from_r(self):
        m, app = self.make()
        self.press(m, "r", "h")
        self.assertEqual((m.ui.screen, m.ui.repo.path), ("repo", HOME.path))
        self.assertEqual(app.git.summaries, 0)

    def test_disabled_home_is_explained_in_polish(self):
        m, app = self.make(home=False)
        self.press(m, "h")
        self.assertEqual(m.ui.screen, "main")
        self.assertIn("wyłączony", m.ui.message)
        self.press(m, "t", "h")
        self.assertEqual(app.calls, [])
        main = "\n".join(menu.render(menu.frame_main(m.view, menu.Ui(), 64, 25, menu.UNICODE), menu.Theme(False), 64))
        self.assertNotIn("bez repo", main)

    def test_rows_render_everywhere_without_overflow(self):
        items = [home_workload(created=time.time()-100), home_workload(created=time.time()-50, instance="2"),
                 workload("codex", "primary", "alpha", State.RUNNING, created=time.time()-10)]
        view = menu.build_view(snapshot(items), {}, home=HOME)
        self.assertEqual([i.repo for i in view.items[:2]], ["~", "~"])
        self.assertEqual(len(view.home.items), 2)
        g = menu.UNICODE
        frames = {
            "main": lambda w, h: menu.frame_main(view, menu.Ui(), w, h, g),
            "pick": lambda w, h: menu.frame_pick(view, menu.Ui(screen="pick", kind="terminal"), w, h, g),
            "home": lambda w, h: menu.frame_repo(view, menu.Ui(screen="repo", repo=HOME), w, h, g),
        }
        for width in (24, 39, 48, 64, 79, 120):
            for height in (8, 12, 18, 25, 50):
                for name, frame in frames.items():
                    rendered = menu.render(frame(width, height), menu.Theme(False), width)
                    self.assertLessEqual(len(rendered), height, (name, width, height))
                    for line in rendered:
                        self.assertLessEqual(menu.width_of(line), width, (name, width, height, line))
        main = "\n".join(menu.render(frames["main"](64, 25), menu.Theme(False), 64))
        self.assertIn(" h  ~", main)
        self.assertIn("bez repo", main)
        self.assertIn("h ~ bez repo", main)
        empty = menu.build_view(snapshot([]), {}, home=HOME)
        idle = "\n".join(menu.render(menu.frame_main(empty, menu.Ui(), 64, 25, g), menu.Theme(False), 64))
        self.assertIn("h ~ bez repo", [line for line in idle.splitlines() if "Nic nie działa" in line][0])
        pick = "\n".join(menu.render(frames["pick"](64, 25), menu.Theme(False), 64))
        self.assertIn(" h  ~", pick)
        self.assertIn("$ główna", pick)

    def test_status_bar_label_says_tilde(self):
        self.assertIn("terminal/główna · ~", label(home_workload().record))


@unittest.skipUnless(LAB, "home workspace lifecycle only under the disposable UID")
class HomeRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.base = Path.home()/".config/hubi6/config.toml"
        self.app = App(Config.load())
        for f in Path.home().glob(".agent-*"):
            f.unlink()

    def tearDown(self):
        for config in (None, *getattr(self, "configs", [])):
            app = App(Config.load(config))
            for w in app.runtime_snapshot().workloads:
                app.stop(w.record.id, repair=True)
                if any(x.record.id == w.record.id for x in app.runtime_snapshot().workloads):
                    app.stop(w.record.id, repair=True)
        for f in Path.home().glob(".agent-*"):
            f.unlink()

    def clients(self, identity, count):
        """The journal is written before the tmux session exists: wait for both."""
        def check():
            session = self.app.find(identity, health_check=False).session
            return session is not None and session.attached == count
        return wait_for(check, 8)

    def config(self, line):
        path = Path.home()/(f"home-{self._testMethodName}.toml")
        path.write_text(line+"\n"+self.base.read_text())
        self.configs = getattr(self, "configs", [])+[str(path)]
        return str(path)

    def cli(self, *args, config=None):
        argv = [sys.executable, "-m", "hubi6"]+(["--config", config] if config else [])+list(args)
        return subprocess.run(argv, capture_output=True, text=True)

    def test_cli_sessions_in_home_beside_a_repository(self):
        project = repo("home-neighbour")
        p = self.cli("start", "terminal", "~")
        self.assertEqual(p.returncode, 0, p.stderr)
        home_id = p.stdout.strip()
        p = self.cli("start", "terminal", str(project))
        self.assertEqual(p.returncode, 0, p.stderr)
        repo_id = p.stdout.strip()
        data = json.loads(self.cli("inspect", home_id).stdout)
        self.assertEqual((data["workspace"], data["repo"], data["state"], data["repo_health"]),
                         ("home", str(Path.home()), "RUNNING", "OK"))
        self.assertEqual(wait_for(lambda: json.loads(self.cli("inspect", home_id).stdout)["cwd"]), str(Path.home()))
        self.assertEqual(data["metadata"]["repo"]["git_dir"], "")
        self.assertEqual(json.loads(self.cli("inspect", repo_id).stdout)["workspace"], "repo")
        p = self.cli("start", "codex", "~")
        self.assertEqual(p.returncode, 0, p.stderr)
        agent = wait_for(lambda: next(Path.home().glob(".agent-*"), None))
        self.assertEqual(json.loads(agent.read_text())["env"]["HUBI6_ACTIVE"], "1")
        listed = json.loads(self.cli("list", "--json").stdout)
        self.assertEqual(sorted(w["workspace"] for w in listed["workloads"]), ["home", "home", "repo"])
        self.assertNotIn(str(Path.home()), [r["path"] for r in listed["repos"]])
        for w in listed["workloads"]:
            self.assertEqual(self.cli("stop", w["id"]).returncode, 0)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_open_without_repository_attaches_and_tidies(self):
        client = Client(["terminal"], ui="dashboard")
        self.addCleanup(client.close)
        w = wait_for(lambda: self.app.runtime_snapshot().workloads)[0]
        self.assertTrue(w.record.repo.plain)
        self.clients(w.record.id, 1)
        client.send(b"echo $PWD > ~/home-pwd.txt; exit\n")
        self.assertEqual(client.p.wait(8), 0)
        self.assertEqual((Path.home()/"home-pwd.txt").read_text().strip(), str(Path.home()))
        (Path.home()/"home-pwd.txt").unlink()
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_dashboard_terminal_in_home_is_remembered_and_reentered(self):
        client = Client([], ui="dashboard", size=(26, 65))
        self.addCleanup(client.close)
        client.read_until("AGENCI I TERMINALE".encode())
        time.sleep(.12)
        client.output = b""
        client.send(b"t")
        client.read_until("Wybierz repozytorium".encode())
        self.assertIn("bez repo", plain(client.output))
        time.sleep(.12)
        client.send(b"h")
        w = wait_for(lambda: self.app.runtime_snapshot().workloads)[0]
        self.assertEqual((w.record.kind, w.record.repo.path), ("terminal", str(Path.home())))
        self.clients(w.record.id, 1)
        client.output = b""
        client.send(b"\x02d")                          # detach: back to the menu, the shell keeps running
        client.read_until("AGENCI I TERMINALE".encode())
        self.clients(w.record.id, 0)
        screen = plain(client.output)
        self.assertIn("terminal/główna", screen)
        self.assertIn("~", screen)
        time.sleep(.12)
        client.send(b"1")                              # remembered: one key brings it back
        self.clients(w.record.id, 1)
        self.assertEqual(self.app.find(w.record.id).record.id, w.record.id)
        client.send(b"exit\n")
        client.read_until("posprzątane".encode(), 10)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_replaced_or_missing_home_stays_stoppable(self):
        scratch = Path.home()/"scratch-home"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir()
        config = self.config('home_workspace = "~/scratch-home"')
        app = App(Config.load(config))
        first = app.start("terminal", None)
        scratch.rename(Path.home()/"scratch-moved")
        self.assertEqual(app.find(first.id).repo_health, RepoHealth.MISSING)
        scratch.mkdir()
        self.assertEqual(app.find(first.id).repo_health, RepoHealth.REPLACED)
        with self.assertRaisesRegex(HubiError, "Previous checkout"):
            app.start("terminal", None)
        app.stop(first.id)
        second = app.start("terminal", None)
        self.assertEqual(app.find(second.id).repo_health, RepoHealth.OK)
        app.stop(second.id)
        shutil.rmtree(Path.home()/"scratch-moved")
        shutil.rmtree(scratch)

    def test_home_with_a_dotfiles_repository_is_never_written(self):
        dotfiles = Path.home()/"dotfiles-home"
        shutil.rmtree(dotfiles, ignore_errors=True)
        dotfiles.mkdir()
        subprocess.run(["git", "-C", str(dotfiles), "init", "-q"], check=True)
        before = tree(dotfiles)
        config = self.config('home_workspace = "~/dotfiles-home"')
        p = self.cli("start", "terminal", str(dotfiles), config=config)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(self.cli("inspect", p.stdout.strip(), config=config).stdout)["workspace"], "home")
        self.assertEqual(self.cli("stop", p.stdout.strip(), config=config).returncode, 0)
        self.assertEqual(tree(dotfiles), before)
        shutil.rmtree(dotfiles)

    def test_agent_commands_need_an_explicit_place(self):
        for args in (["claude"], ["codex"], ["claude", ""], ["start", "codex", ""]):
            p = self.cli(*args)
            self.assertEqual(p.returncode, 2, (args, p.stdout, p.stderr))
        self.assertFalse(self.app.runtime_snapshot().workloads)
        p = self.cli("start", "codex", "~")
        self.assertEqual(p.returncode, 0, p.stderr)              # an explicit ~ is honoured
        self.assertEqual(json.loads(self.cli("inspect", p.stdout.strip()).stdout)["workspace"], "home")

    def test_classic_menu_offers_h_only_when_usable(self):
        for line, offered in (("", True), ('home_workspace = ""', False)):
            config = self.config(line) if line else None
            args = ["--config", config] if config else []
            client = Client(args, ui="classic")
            try:
                client.read_until(b"Choice + Enter:")
                self.assertEqual(b"[h] home" in client.output, offered, line)
                time.sleep(.12)
                client.output = b""
                client.send(b"h\n")
                client.read_until(b"Choice + Enter:")
                self.assertEqual(b"home, no repository" in client.output, offered, line)
            finally:
                client.close()
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_disabled_home_workspace_is_refused_cleanly(self):
        config = self.config('home_workspace = ""')
        p = self.cli("start", "terminal", "~", config=config)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(App(Config.load(config)).runtime_snapshot().workloads)
        doctor = json.loads(self.cli("doctor", "--json", config=config).stdout)
        self.assertEqual(doctor["checks"]["home_workspace"], "disabled")
