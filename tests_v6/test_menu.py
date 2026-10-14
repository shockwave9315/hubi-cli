"""Polish dashboard: pure rendering and key handling anywhere; lifecycle in the lab."""
import ast
from dataclasses import replace
import io
import os
from pathlib import Path
import pty
import re
import signal
import sys
import termios
import time
import unittest
from unittest.mock import patch

from hubi6 import menu
from hubi6.adapters.tmux import NEW_PANE_HINT, label
from hubi6.config import Config
from hubi6.core.model import HubiError, Record, RepoHealth, RepoRef, Session, State, Workload, key_id
from hubi6.i18n import age, instance_of, label_pl, pl, plural
from hubi6.inventory import Snapshot
from hubi6.services import App
from hubi6.tui import terminal
from tests_v6.support import LAB, bootstrap
from tests_v6.test_runtime import Client, wait_for

ROOT = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain(data):
    return ANSI.sub("", data.decode("utf-8", "replace") if isinstance(data, bytes) else data)


def ref(name, inode=1):
    return RepoRef(f"/srv/repos/{name}", 1, inode, f"/srv/repos/{name}/.git", 1, 100+inode, "", "")


def workload(kind, instance, name, state, *, created, attached=0, status=None, exit_signal="", health=RepoHealth.OK):
    r = Record("a"*20, os.getuid(), kind, instance, key_id(kind, ref(name).path, instance), "b"*16, ref(name),
               created=created, pane="%1", invocation="c"*32, phase="running")
    s = Session(r.session, "$1", "%1", state == State.EXITED, 1, attached, r.encoded())
    s.exit_status, s.exit_signal = status, exit_signal
    return Workload(r, state, health, s, None, [])


def snapshot(workloads, names=("alpha", "beta", "praca"), issues=()):
    return Snapshot(list(workloads), [ref(n, i) for i, n in enumerate(names)], list(issues), "OK", None, 1, "0", True, True, 1.0, 2)


def sample():
    now = time.time()
    items = [workload("claude", "rev", "praca", State.RUNNING, created=now-1560),
             workload("codex", "cp5", "praca", State.RUNNING, created=now-1980),
             workload("terminal", "primary", "alpha", State.ATTACHED, created=now-9000, attached=1),
             workload("claude", "smoke", "alpha", State.EXITED, created=now-60, status=1)]
    titles = {items[0].record.session: ("claude", "✳ Claude Code"),
              items[1].record.session: ("node", "⠼ Zaimplementuj CP5: timer i sloty | praca"),
              items[2].record.session: ("vim", "dev@box: ~/repos/alpha")}
    return items, titles


class KeyTests(unittest.TestCase):
    def reader(self, **kw):
        rd, wr = os.pipe()
        self.addCleanup(os.close, rd)
        self.addCleanup(lambda: os.close(wr) if wr in self.open else None)
        self.open = {wr}
        return menu.Keys(rd, **kw), wr

    def test_single_keys_and_names(self):
        cases = {b"1": "1", b"q": "q", b" ": " ", b"\n": "enter", b"\r": "enter", b"\x1b": "esc", b"\x7f": "back",
                 b"\x1b[A": "up", b"\x1bOB": "down", b"\x1b[5~": "pgup", b"\x04": "eof", "ą".encode(): "ą"}
        for raw, expected in cases.items():
            self.assertEqual(menu.parse_key(raw), expected, raw)

    def test_unused_keys_are_ignored_not_bursts(self):
        for raw in (b"\x1b[H", b"\x1b[1;5C", b"\x1a", b"\x15", b"\x1b[I"):
            self.assertEqual(menu.parse_key(raw), "", raw)

    def test_several_keys_in_one_read_are_a_burst(self):
        for raw in (b"1q", b"d1q", b"w\n", b"\x1b[200~d", b"ab\xff", b"\x1b[A\x1b[A"):
            self.assertIsNone(menu.parse_key(raw), raw)

    def test_burst_is_discarded_whole(self):
        reader, wr = self.reader()
        os.write(wr, b"d1q")
        self.assertEqual(reader.key(1), "burst")
        self.assertIsNone(reader.key(.05))          # nothing from the burst survives
        os.write(wr, b"w")
        self.assertEqual(reader.key(1), "w")

    def test_separate_quick_keys_are_both_honoured(self):
        # The line menu dropped everything within 80 ms of Enter. Clean single
        # keys carry no such penalty: type-ahead in separate reads works.
        reader, wr = self.reader()
        os.write(wr, b"w")
        self.assertEqual(reader.key(1), "w")
        os.write(wr, b"b")
        started = time.monotonic()
        self.assertEqual(reader.key(1), "b")
        self.assertLess(time.monotonic()-started, .05)

    def test_bracketed_paste_is_swallowed_across_reads(self):
        reader, wr = self.reader()
        os.write(wr, b"\x1b[200~d\n1\n")
        os.write(wr, b"q\n\x1b[201~")
        self.assertEqual(reader.key(1), "burst")
        self.assertIsNone(reader.key(.05))

    def test_unterminated_paste_times_out(self):
        reader, wr = self.reader(paste_timeout=.02)
        os.write(wr, b"\x1b[200~q")
        self.assertEqual(reader.key(1), "burst")

    def test_settle_drops_queued_confirmation(self):
        reader, wr = self.reader()
        os.write(wr, b"q")
        reader.settle()
        self.assertIsNone(reader.key(.05))

    def test_eof_and_timeout(self):
        reader, wr = self.reader()
        self.assertIsNone(reader.key(.01))
        os.close(wr)
        self.open.clear()
        self.assertEqual(reader.key(1), "eof")


class RenderTests(unittest.TestCase):
    def frames(self, width, height, ui=None):
        items, titles = sample()
        view = menu.build_view(snapshot(items), titles)
        with patch.object(menu, "branch_of", return_value="fix/a-rather-long-branch-name"):
            view = menu.build_view(snapshot(items), titles)
        g = menu.UNICODE
        catalogue = type("C", (), {"stale": True, "error": ""})()
        repos = [{"nameWithOwner": f"owner/repo-with-a-long-name-{n}", "isPrivate": n % 2, "isFork": n % 3 == 0,
                  "isArchived": False, "local": n % 2 == 0, "local_paths": []} for n in range(14)]
        return {
            "main": menu.frame_main(view, menu.Ui(autologin=True), width, height, g),
            "main-stop": menu.frame_main(view, menu.Ui(verb="stop"), width, height, g),
            "pick": menu.frame_pick(view, menu.Ui(screen="pick", kind="claude", resume=True), width, height, g),
            "repo": menu.frame_repo(view, menu.Ui(screen="repo", repo=ref("alpha"), git="## main\n M a\n?? b\n"*9), width, height, g),
            "detail": menu.frame_detail(view, menu.Ui(screen="detail", selected=items[1].record.id), width, height, g),
            "github": menu.frame_github(view, menu.Ui(screen="github"), width, height, g, catalogue, repos),
            "help": menu.frame_help(view, menu.Ui(screen="help"), width, height, g),
        }

    def test_no_frame_wraps_or_overflows_at_any_size(self):
        for width in (24, 39, 48, 64, 79, 120, 200):
            for height in (8, 12, 18, 25, 50):
                for name, lines in self.frames(width, height).items():
                    rendered = menu.render(lines, menu.Theme(False), width)
                    self.assertLessEqual(len(rendered), height, (name, width, height))
                    for line in rendered:
                        self.assertLessEqual(menu.width_of(line), width, (name, width, height, line))

    def test_phone_sized_main_screen_content(self):
        text = "\n".join(menu.render(self.frames(64, 25)["main"], menu.Theme(False), 64))
        for expected in ("claude/rev", "codex/cp5", "terminal/główna", "Zaimplementuj CP5: timer i sloty",
                         "pracuje", "czeka", "błąd (kod 1)", "AGENCI I TERMINALE", "REPOZYTORIA", "x rozłącz"):
            self.assertIn(expected, text)
        for english in ("RUNNING", "ATTACHED", "EXITED", "Choice", "workloads", "back", "primary"):
            self.assertNotIn(english, text)
        self.assertNotIn("Claude Code", text)        # a generic agent title is noise, not information

    def test_default_instance_is_shown_as_a_polish_label_everywhere(self):
        frames = self.frames(64, 25)
        for name in ("main", "pick", "repo", "detail"):
            text = "\n".join(menu.render(frames[name], menu.Theme(False), 64))
            self.assertNotIn("primary", text, name)
            self.assertNotIn("instanc", text.lower(), name)
        for name in ("pick", "repo"):
            text = "\n".join(menu.render(frames[name], menu.Theme(False), 64))
            self.assertIn("etykieta: główna", text, name)
            self.assertIn("n etykieta", text, name)
        self.assertNotIn("instanc", " ".join(text for _keys, text in menu.HELP).lower())

    def test_repository_row_says_how_many_sessions_did_not_fit(self):
        now = time.time()
        items = [workload(kind, name, "praca", State.RUNNING, created=now+n)
                 for n, (kind, name) in enumerate((("codex", "primary"), ("claude", "rev"), ("terminal", "primary"), ("terminal", "t2")))]
        with patch.object(menu, "branch_of", return_value="cp5-timer-trip"):
            row = next(r for r in menu.build_view(snapshot(items), {}).repos if r.name == "praca")
        draw = lambda width: "".join(menu.render([menu.repo_row("", row, width, menu.UNICODE)], menu.Theme(False), width))
        self.assertTrue(draw(65).rstrip().endswith(">_ główna  ✳ rev  +2"), draw(65))
        self.assertTrue(draw(40).rstrip().endswith("+4"), draw(40))
        self.assertNotIn("+", draw(100))
        self.assertIn("$ t2", draw(100))

    def test_footer_survives_a_short_terminal(self):
        text = "\n".join(menu.render(self.frames(64, 8)["main"], menu.Theme(False), 64))
        self.assertIn("1-9 wejdź", text)

    def test_numbering_follows_creation_order(self):
        items, titles = sample()
        before = [i.id for i in menu.build_view(snapshot(items), titles).items]
        newer = workload("terminal", "extra", "alpha", State.RUNNING, created=time.time())
        after = [i.id for i in menu.build_view(snapshot(items+[newer]), titles).items]
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(after[-1], newer.record.id)

    def test_colour_can_be_disabled(self):
        lines = self.frames(64, 25)["main"]
        self.assertNotIn("\x1b", "".join(menu.render(lines, menu.Theme(False), 64)))
        self.assertIn("\x1b[", "".join(menu.render(lines, menu.Theme(True), 64)))

    def test_hostile_text_cannot_inject_terminal_controls(self):
        evil = workload("codex", "x", "praca", State.RUNNING, created=time.time())
        titles = {evil.record.session: ("node", "⠼ \x1b[2J\x1b]0;pwn\x07 ‮ reversed")}
        view = menu.build_view(snapshot([evil]), titles)
        text = "".join(menu.render(menu.frame_main(view, menu.Ui(), 64, 25, menu.UNICODE), menu.Theme(False), 64))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("‮", text)

    def test_describe(self):
        self.assertEqual(menu.describe("codex", "node", "⠼ Fix tests | praca", "praca"), "Fix tests")
        self.assertEqual(menu.describe("claude", "claude", "✳ Claude Code", "praca"), "")
        self.assertEqual(menu.describe("claude", "claude", "✳ Hubi v6 PR", "praca"), "Hubi v6 PR")
        self.assertEqual(menu.describe("terminal", "bash", "dev@box: ~", "praca"), "")
        self.assertEqual(menu.describe("terminal", "vim", "dev@box: ~", "praca"), "vim")
        # Codex before it has a topic: the title is only spinner frames.
        self.assertEqual(menu.describe("codex", "node", "⠼ ⠼ | praca", "praca"), "")
        self.assertEqual(menu.describe("claude", "claude", "◐ Test odpowiedzi", "praca"), "Test odpowiedzi")

    def test_working_is_read_from_real_agent_titles(self):
        # Recorded on 2026-10-14 from Claude Code 2.1.296 and Codex 0.162.1.
        for title in ("◐ Test odpowiedzi jednym słowem", "⠼ ⠼ | praca", "⠸ Potwierdź | praca", "⠏"):
            self.assertEqual(menu.working(title), title[0], title)
        for title in ("✳ Test odpowiedzi jednym słowem", "✳ Claude Code", "Potwierdź | praca", "praca", "claude · resume",
                      "", "⠸bez spacji", "- lista", "* gwiazdka", "\u2800 pusty brajl"):
            self.assertEqual(menu.working(title), "", title)
        now = time.time()
        claude = workload("claude", "primary", "praca", State.RUNNING, created=now)
        shell = workload("terminal", "primary", "praca", State.RUNNING, created=now+1)
        dead = workload("codex", "primary", "praca", State.EXITED, created=now+2, status=0)
        titles = {w.record.session: ("x", "◐ Praca") for w in (claude, shell, dead)}
        view = menu.build_view(snapshot([claude, shell, dead]), titles)
        self.assertEqual([(i.kind, i.busy) for i in view.items], [("claude", True), ("terminal", False), ("codex", False)])
        self.assertEqual(menu.state_cell(view.items[0], menu.UNICODE), ("pracuje ◐", "busy"))

    def test_message_wraps_instead_of_being_cut(self):
        ui = menu.Ui(message="słowo "*30, level="bad")
        lines = menu.render(menu.message_lines(ui, 40), menu.Theme(False), 40)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(menu.width_of(line) <= 40 for line in lines))

    def test_screen_sends_only_changed_lines(self):
        class Stream(io.StringIO):
            def fileno(self):
                raise OSError()
        stream = Stream()
        screen = menu.Screen(stream)
        screen.paint(["one", "two", "three"])
        first = stream.getvalue()
        self.assertIn("\x1b[2J", first)
        stream.seek(0)
        stream.truncate()
        screen.paint(["one", "two", "three"])
        self.assertEqual(stream.getvalue(), "")
        screen.paint(["one", "TWO", "three"])
        self.assertIn("TWO", stream.getvalue())
        self.assertNotIn("one", stream.getvalue())
        self.assertNotIn("three", stream.getvalue())
        stream.seek(0)
        stream.truncate()
        screen.paint(["one"])
        self.assertIn("\x1b[2;1H\x1b[J", stream.getvalue())


class TextTests(unittest.TestCase):
    def messages(self):
        """Every literal HubiError message and appended diagnostic in the package."""
        found = []
        for path in sorted((ROOT/"hubi6").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                first = node.args[0]
                is_error = getattr(node.func, "id", "") == "HubiError"
                is_issue = getattr(node.func, "attr", "") == "append" and path.name in ("inventory.py", "git.py", "storage.py") \
                    or (getattr(node.func, "attr", "") == "append" and path.name == "tmux.py" and isinstance(first, ast.Constant)
                        and isinstance(first.value, str) and " " in first.value)
                if not (is_error or is_issue):
                    continue
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append((path.name, node.lineno, first.value))
                elif isinstance(first, ast.JoinedStr):
                    text = "".join(part.value if isinstance(part, ast.Constant) else "X1" for part in first.values)
                    found.append((path.name, node.lineno, text))
                elif isinstance(first, ast.BinOp) and isinstance(first.left, ast.Constant):
                    found.append((path.name, node.lineno, first.left.value+"X1"))
        return found

    def test_every_error_has_polish_wording(self):
        messages = self.messages()
        self.assertGreater(len(messages), 100)
        missing = [m for m in messages if pl(m[2]) == m[2]]
        self.assertFalse(missing, missing)

    def test_template_fields_are_preserved(self):
        self.assertEqual(pl("Agent executable unavailable: codex"), "Brak programu agenta: codex")
        self.assertIn("0123abcd", pl("Workload EXITED: 0123abcd; stop/reconcile it before creating a new generation"))
        self.assertEqual(pl("Invalid repository x: broken symlink"), "Nieprawidłowe repozytorium x: zerwany symlink")
        self.assertEqual(pl("something nobody translated"), "something nobody translated")

    def test_age_and_plural(self):
        self.assertEqual([age(5), age(125), age(7500), age(200000)], ["5 s", "2 min", "2 h 05", "2 d 7 h"])
        self.assertEqual([plural(n, "a", "b", "c") for n in (1, 2, 5, 12, 22)], ["a", "b", "c", "c", "b"])

    def test_status_label_is_inert(self):
        r = workload("claude", "rev", "pra#[fg=red]ca", State.RUNNING, created=1).record
        self.assertNotIn("#", label(r))
        self.assertIn("claude/rev", label(r))
        self.assertLessEqual(len(label(r)), 60)
        self.assertTrue(NEW_PANE_HINT)
        self.assertIn("codex/główna · praca", label(workload("codex", "primary", "praca", State.RUNNING, created=1).record))

    def test_default_label_maps_both_ways_and_never_changes_identity(self):
        self.assertEqual([label_pl("primary"), label_pl("rev"), label_pl("Primary")], ["główna", "rev", "Primary"])
        self.assertEqual([instance_of(x) for x in ("", "  ", "główna", "GŁÓWNA", " rev ", "primary", "glowna")],
                         ["primary", "primary", "primary", "primary", "rev", "primary", "glowna"])
        # The stored record and its key keep the ASCII name; only what is drawn changes.
        r = workload("claude", "primary", "praca", State.RUNNING, created=1).record
        self.assertEqual((r.instance, r.key), ("primary", key_id("claude", ref("praca").path, "primary")))
        self.assertEqual(r.data()["instance"], "primary")

    def test_ui_configuration(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"c.toml"
            path.write_text("[ui]\nrefresh = 5\ncolor = false\nicons = false\n")
            c = Config.load(str(path))
            self.assertEqual((c.ui_refresh, c.ui_color, c.ui_icons), (5, False, False))
            # Each Codex session must own its processes: no shared background server.
            self.assertEqual(c.agents["codex"], ["codex", "--no-daemon"])
            path.write_text('[agents]\ncodex = ["codex"]\n')
            self.assertEqual(Config.load(str(path)).agents["codex"], ["codex"])
            for bad in ("[ui]\nrefresh = 0\n", "[ui]\ncolor = 1\n", "[ui]\nfancy = true\n"):
                path.write_text(bad)
                with self.assertRaises(HubiError):
                    Config.load(str(path))


class LifecycleUnitTests(unittest.TestCase):
    def app(self, found):
        app = App(Config.load())
        app.stopped = []
        app.find = lambda identity, snapshot=None, health_check=True: found
        app.stop = lambda identity, expected=None, repair=False: app.stopped.append((identity, expected))
        return app

    def test_only_a_clean_unattached_exit_is_reaped(self):
        now = time.time()
        clean = workload("terminal", "a", "alpha", State.EXITED, created=now, status=0)
        app = self.app(clean)
        self.assertIs(app.reap_clean_exit(clean.record.id), clean)
        self.assertEqual(app.stopped, [(clean.record.id, clean.record.generation)])
        for kept in (workload("terminal", "a", "alpha", State.EXITED, created=now, status=1),
                     workload("terminal", "a", "alpha", State.EXITED, created=now, status=0, exit_signal="9"),
                     workload("terminal", "a", "alpha", State.EXITED, created=now, status=0, attached=1),
                     workload("terminal", "a", "alpha", State.EXITED, created=now, status=None),
                     workload("terminal", "a", "alpha", State.RUNNING, created=now)):
            app = self.app(kept)
            self.assertIsNone(app.reap_clean_exit(kept.record.id))
            self.assertEqual(app.stopped, [])

    def test_menu_terminal_mode_cannot_be_suspended(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before, handler = termios.tcgetattr(slave), signal.getsignal(signal.SIGTSTP)
        fake = type("Tty", (), {"fileno": lambda self: slave})()
        with patch("hubi6.tui.sys.stdin", fake), patch("hubi6.tui.sys.stdout", io.StringIO()):
            with terminal(paste=True):
                self.assertEqual(termios.tcgetattr(slave)[6][termios.VSUSP], b"\x00")
                self.assertTrue(callable(signal.getsignal(signal.SIGTSTP)))
            self.assertEqual(termios.tcgetattr(slave), before)
            self.assertEqual(signal.getsignal(signal.SIGTSTP), handler)
            with terminal():                         # plain CLI commands keep normal job control
                self.assertEqual(termios.tcgetattr(slave)[6][termios.VSUSP], before[6][termios.VSUSP])


class FakeTmux:
    def __init__(self, titles):
        self.table = titles

    def titles(self):
        return self.table


class FakeGit:
    def __init__(self):
        self.summaries = 0

    def summary(self, reference):
        self.summaries += 1
        return "## main\n M file\n"

    def resolve(self, value):
        return ref(Path(value).name)


class FakeApp:
    """Records lifecycle calls; nothing here touches tmux, systemd or a PTY."""

    def __init__(self, workloads=(), titles=None):
        self.c = replace(Config.load(), ui_color=False)
        self.workloads, self.calls, self.r = list(workloads), [], None
        self.tmux, self.git = FakeTmux(titles or {}), FakeGit()
        self.fail_start = None

    def snapshot(self):
        return snapshot(self.workloads)

    def start(self, kind, path, instance="primary", resume=False):
        self.calls.append(("start", kind, path, instance, resume))
        if self.fail_start:
            raise HubiError(self.fail_start)
        w = workload(kind, instance, Path(path).name, State.RUNNING, created=time.time())
        self.workloads.append(w)
        return w.record

    def attach(self, identity, mode="normal"):
        self.calls.append(("attach", identity, mode))
        return 0

    def stop(self, identity, expected=None, repair=False):
        self.calls.append(("stop", identity, expected, repair))
        self.workloads = [w for w in self.workloads if w.record.id != identity]

    def find(self, identity, snapshot=None, health_check=True):
        for w in self.workloads:
            if w.record.id == identity:
                return w
        raise HubiError("Workload ID missing or ambiguous; use hubi list")

    def reap_clean_exit(self, identity):
        return None


class ControllerTests(unittest.TestCase):
    def make(self, workloads=(), titles=None, **env):
        rd, wr = os.pipe()
        self.addCleanup(os.close, rd)
        self.addCleanup(os.close, wr)
        self.wr = wr

        class Stream(io.StringIO):
            def fileno(self):
                raise OSError()
        app = FakeApp(workloads, titles)
        with patch.dict(os.environ, env):
            m = menu.Menu(app, menu.Keys(rd), menu.Screen(Stream()))
        m.refresh()
        return m, app

    def later(self, data, delay=.2):
        import threading
        timer = threading.Timer(delay, os.write, (self.wr, data))
        timer.start()
        self.addCleanup(timer.join)

    def press(self, m, *keys):
        result = None
        for key in keys:
            with patch("hubi6.tui.sys.stdout", io.StringIO()):
                result = m.handle(key)
            m.refresh()
            m.draw()
        return result

    def text(self, m):
        return plain(m.screen.stream.getvalue())

    def test_digit_attaches_at_once(self):
        items, titles = sample()
        m, app = self.make(items[:2], titles)
        first = menu.build_view(app.snapshot(), titles).items[0]
        self.assertIsNone(self.press(m, "1"))
        self.assertEqual(app.calls, [("attach", first.id, "normal")])

    def test_attached_session_asks_how_to_join(self):
        busy = workload("terminal", "primary", "alpha", State.ATTACHED, created=1, attached=2)
        m, app = self.make([busy])
        self.later(b"s")
        self.press(m, "1")
        self.assertEqual(app.calls, [("attach", busy.record.id, "shared")])
        self.assertIn("ma już 2 klientów", self.text(m))
        m, app = self.make([busy])
        self.later(b"z")
        self.press(m, "1")
        self.assertEqual(app.calls, [])

    def test_stop_requires_q_and_passes_the_generation(self):
        items, titles = sample()
        m, app = self.make(items[:2], titles)
        target = menu.build_view(app.snapshot(), titles).items[0]
        self.later(b"n")
        self.press(m, "d", "1")
        self.assertEqual(app.calls, [])
        self.assertIn("Anulowano", self.text(m))
        self.later(b"q")
        self.press(m, "d", "1")
        self.assertEqual(app.calls, [("stop", target.id, target.generation, False)])
        self.assertIn("zatrzymano", self.text(m))

    def test_any_other_key_cancels_a_pending_stop(self):
        items, titles = sample()
        m, app = self.make(items[:2], titles)
        self.press(m, "d", "g")
        self.assertEqual((m.ui.verb, m.ui.screen, app.calls), ("", "main", []))

    def test_new_agent_is_two_keys_and_attaches(self):
        m, app = self.make()
        self.press(m, "L", "3")                      # keys are not case sensitive
        started = app.workloads[-1].record
        self.assertEqual(app.calls, [("start", "claude", ref("praca").path, "primary", False), ("attach", started.id, "normal")])
        self.assertEqual(m.ui.screen, "main")

    def test_named_instance_and_resume(self):
        m, app = self.make()
        os.write(self.wr, b"rev\n")
        self.press(m, "c", "r", "n", "1")
        self.assertEqual(app.calls[0], ("start", "codex", ref("alpha").path, "rev", True))
        self.assertEqual((m.ui.instance, m.ui.resume), ("primary", False))   # options do not leak into the next start

    def test_invalid_instance_name_is_refused_in_polish(self):
        m, app = self.make()
        os.write(self.wr, "zła nazwa\n".encode())
        self.press(m, "l", "n")
        self.assertEqual(m.ui.instance, "primary")
        self.assertIn("Etykieta", m.ui.message)
        self.assertNotIn("instanc", m.ui.message.lower())
        self.assertEqual(app.calls, [])

    def test_label_prompt_and_the_default_label(self):
        for typed in ("\n", "główna\n", "Główna \n"):
            m, app = self.make()
            os.write(self.wr, typed.encode())
            self.press(m, "t", "n", "1")
            self.assertEqual(app.calls[0], ("start", "terminal", ref("alpha").path, "primary", False), typed)

    def test_terminal_has_no_resume(self):
        m, app = self.make()
        self.press(m, "t", "r")
        self.assertFalse(m.ui.resume)
        self.assertIn("nic tu nie robi", m.ui.message)

    def test_existing_instance_is_entered_not_restarted(self):
        running = workload("claude", "primary", "alpha", State.RUNNING, created=1)
        m, app = self.make([running])
        self.press(m, "l", "1")
        self.assertEqual(app.calls, [("attach", running.record.id, "normal")])

    def test_finished_instance_is_replaced_only_after_q(self):
        dead = workload("claude", "primary", "alpha", State.EXITED, created=1, status=1)
        m, app = self.make([dead])
        self.later(b"n")
        self.press(m, "l", "1")
        self.assertEqual(app.calls, [])
        self.later(b"q")
        self.press(m, "l", "1")
        self.assertEqual([call[0] for call in app.calls], ["stop", "start", "attach"])

    def test_failed_start_is_reported_in_polish(self):
        m, app = self.make()
        app.fail_start = "Agent executable unavailable: codex"
        self.press(m, "c", "1")
        self.assertEqual(m.ui.message, "Brak programu agenta: codex")
        self.assertEqual(m.ui.level, "bad")

    def test_exit_keys(self):
        m, _ = self.make()
        self.assertEqual(self.press(m, "q"), 98)
        self.assertEqual(self.press(m, "eof"), 0)
        self.assertIsNone(self.press(m, "x"))
        self.assertIn("nic tu nie robi", m.ui.message)
        m, _ = self.make(HUBI_AUTOLOGIN="1")
        self.assertEqual(self.press(m, "x"), 99)

    def test_burst_and_unknown_keys_are_explained(self):
        m, app = self.make()
        self.press(m, "burst")
        self.assertIn("Zignorowano serię znaków", m.ui.message)
        self.press(m, "z")
        self.assertIn("Klawisz „z” nic tu nie robi", m.ui.message)
        self.press(m, "9")
        self.assertIn("Nie ma sesji numer 9", m.ui.message)
        self.assertEqual(app.calls, [])

    def test_back_from_every_screen(self):
        m, _ = self.make()
        for opener in ("l", "r", "?"):
            for back in ("0", "esc", "back"):
                self.press(m, opener)
                self.assertNotEqual(m.ui.screen, "main", opener)
                self.press(m, back)
                self.assertEqual(m.ui.screen, "main", (opener, back))

    def test_repository_view_and_details(self):
        items, titles = sample()
        m, app = self.make(items, titles)
        self.press(m, "r", "3")
        self.assertEqual((m.ui.screen, m.ui.repo.path, app.git.summaries), ("repo", ref("praca").path, 1))
        self.assertIn("M file", self.text(m))
        self.press(m, "0", "i", "1")
        self.assertEqual(m.ui.screen, "detail")
        self.press(m, "v")
        self.assertEqual(app.calls[-1][2], "view")

    def test_github_open_local_and_clone_needs_typed_tak(self):
        m, app = self.make()
        catalogue = type("C", (), {"stale": False, "error": "", "repos": []})()
        entries = [{"nameWithOwner": "o/alpha", "isPrivate": False, "isFork": False, "isArchived": False, "local": True,
                    "local_paths": [ref("alpha").path]},
                   {"nameWithOwner": "o/fresh", "isPrivate": True, "isFork": False, "isArchived": False, "local": False,
                    "local_paths": []}]
        cloned = []
        with patch.object(m.github, "cached", return_value=catalogue), patch.object(m.github, "view", return_value=entries), \
                patch.object(m.github, "clone", side_effect=lambda name: cloned.append(name) or ref("fresh")):
            self.press(m, "g", "1")
            self.assertEqual((m.ui.screen, m.ui.repo.path), ("repo", ref("alpha").path))
            self.press(m, "0", "g")
            self.later(b"nie\n")
            self.press(m, "2")
            self.assertEqual(cloned, [])
            self.assertIn("anulowane", m.ui.message)
            self.later(b"tak\n")
            self.press(m, "2")
            self.assertEqual(cloned, ["o/fresh"])
            self.assertEqual((m.ui.screen, m.ui.repo.path), ("repo", ref("fresh").path))


@unittest.skipUnless(LAB, "dashboard lifecycle only under the disposable UID")
class DashboardRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        self.root = Path.home()/("menu-root-"+self._testMethodName)
        self.root.mkdir(exist_ok=True)
        self.path = self.root/"only"
        if not self.path.exists():
            import subprocess
            self.path.mkdir()
            subprocess.run(["git", "-C", str(self.path), "init", "-q"], check=True)
        self.config = Path.home()/("menu-"+self._testMethodName+".toml")
        base = (Path.home()/".config/hubi6/config.toml").read_text()
        self.config.write_text('repos_root = "'+str(self.root)+'"\n'+base)
        self.app = App(Config.load(str(self.config)))

    def tearDown(self):
        for w in self.app.runtime_snapshot().workloads:
            self.app.stop(w.record.id, repair=True)
            if any(x.record.id == w.record.id for x in self.app.runtime_snapshot().workloads):
                self.app.stop(w.record.id, repair=True)

    def menu(self, **env):
        client = Client([], command=[sys.executable, "-m", "hubi6", "--config", str(self.config)], ui="dashboard",
                        env=env, size=(26, 65))
        self.addCleanup(client.close)
        client.read_until("AGENCI I TERMINALE".encode())
        return client

    def press(self, client, key, needle, timeout=5):
        time.sleep(.12)                              # a person never follows a redraw within the quiet window
        client.output = b""
        client.send(key)
        client.read_until(needle.encode(), timeout)

    def only(self):
        return wait_for(lambda: self.app.runtime_snapshot().workloads)[0]

    def attached(self, identity):
        def check():
            session = self.app.find(identity).session
            return session is not None and session.attached == 1
        wait_for(check)

    def test_start_attaches_and_clean_exit_returns_to_a_tidy_menu(self):
        client = self.menu()
        self.press(client, b"t", "Wybierz repozytorium")
        client.output = b""
        time.sleep(.12)
        client.send(b"1")
        w = self.only()
        self.attached(w.record.id)
        self.assertEqual(self.app.find(w.record.id).record.kind, "terminal")
        client.send(b"exit\n")
        client.read_until("posprzątane".encode(), 10)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_label_is_asked_in_polish_and_the_default_stays_primary(self):
        from contextlib import redirect_stdout
        from hubi6.cli import main
        client = self.menu()
        self.press(client, b"t", "Wybierz repozytorium")
        self.assertIn("etykieta: główna", plain(client.output))
        self.press(client, b"n", "Etykieta [główna]:")
        client.output = b""
        client.send(b"drugi\n")
        client.read_until("Wybierz repozytorium".encode())
        self.assertIn("etykieta: drugi", plain(client.output))
        time.sleep(.12)
        client.send(b"1")
        named = self.only().record
        self.attached(named.id)
        self.assertEqual(named.instance, "drugi")
        client.send(b"\x02d")
        client.read_until("AGENCI I TERMINALE".encode())
        # The default label is display only: identity, record and JSON keep "primary".
        default = self.app.start("terminal", str(self.path))
        self.assertEqual(default.instance, "primary")
        show = lambda *args: self.app.tmux.command(*args).stdout
        self.assertEqual(show("show-options", "-v", "-t", default.pane, "@hubi6-label").strip(), "$ terminal/główna · only")
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--config", str(self.config), "list"])
        self.assertIn("terminal/główna", out.getvalue())
        self.assertIn("terminal/drugi", out.getvalue())
        self.assertNotIn("terminal/primary", out.getvalue())
        with redirect_stdout(io.StringIO()):
            main(["--config", str(self.config), "start", "terminal", str(self.path), "--instance", "główna", "--json"])
        self.assertEqual(sorted(w.record.instance for w in self.app.runtime_snapshot().workloads), ["drugi", "primary"])
        client.read_until("terminal/główna".encode(), 10)   # the live list picks the new session up by itself

    def test_session_ended_after_switching_inside_tmux_is_tidied_too(self):
        first = self.app.start("terminal", str(self.path), "first")
        second = self.app.start("terminal", str(self.path), "second")
        client = self.menu()
        time.sleep(.12)
        client.send(b"1")
        self.attached(first.id)
        self.app.tmux.command("switch-client", "-c", self.app.tmux.command(
            "list-clients", "-F", "#{client_name}").stdout.strip(), "-t", "="+second.session)
        self.attached(second.id)
        client.send(b"exit\n")
        client.read_until("terminal/second".encode(), 10)
        client.read_until("posprzątane".encode(), 10)
        self.assertEqual([w.record.id for w in self.app.runtime_snapshot().workloads], [first.id])

    def test_failed_exit_is_kept_and_reported(self):
        self.config.write_text(self.config.read_text().replace(
            'codex = [', 'codex = ["/bin/sh", "-c", "sleep 1; exit 3"]\n#codex = [', 1))
        self.app = App(Config.load(str(self.config)))
        client = self.menu()
        self.press(client, b"c", "Wybierz repozytorium")
        client.output = b""
        time.sleep(.12)
        client.send(b"1")
        client.read_until("kod 3".encode(), 10)
        w = self.only()
        self.assertEqual(w.state, State.EXITED)
        self.assertEqual(w.session.exit_status, 3)

    def test_stop_needs_a_deliberate_q_and_a_paste_cannot_give_it(self):
        r = self.app.start("terminal", str(self.path))
        client = self.menu()
        time.sleep(.12)
        client.output = b""
        client.send(b"d1q")                           # one burst: nothing happens
        client.read_until("Zignorowano".encode())
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        self.press(client, b"d", "ZATRZYMAJ")
        client.send(b"1")
        time.sleep(.03)
        client.send(b"q")                             # typed before the question is on screen: dropped
        client.read_until("q = TAK".encode())
        time.sleep(.3)
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        self.press(client, b"n", "Anulowano")
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        self.press(client, b"d", "ZATRZYMAJ")
        self.press(client, b"1", "q = TAK")
        self.press(client, b"q", "zatrzymano", 10)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_suspend_keys_and_signal_do_not_freeze_the_menu(self):
        client = self.menu()
        client.send(b"\x1a")
        client.p.send_signal(signal.SIGTSTP)
        time.sleep(.3)
        with open(f"/proc/{client.p.pid}/stat") as stream:
            self.assertNotEqual(stream.read().rsplit(")", 1)[1].split()[0], "T")
        self.press(client, b"?", "POMOC")

    def test_exit_codes_and_terminal_restoration(self):
        for sig, code in ((signal.SIGINT, 130), (signal.SIGHUP, 129), (signal.SIGTERM, 143)):
            client = self.menu()
            client.p.send_signal(sig)
            self.assertEqual(client.p.wait(3), code)
            self.assertEqual(termios.tcgetattr(client.slave), client.before)
        for data, code, env in ((b"q", 98, {}), (b"\x04", 0, {}), (b"x", 99, {"HUBI_AUTOLOGIN": "1"})):
            client = self.menu(**env)
            time.sleep(.12)
            client.send(data)
            self.assertEqual(client.p.wait(3), code)
            self.assertEqual(termios.tcgetattr(client.slave), client.before)
        client = self.menu()                           # outside autologin `x` is not an exit
        self.press(client, b"x", "nic tu nie robi")

    def test_session_presentation_and_guards_in_tmux(self):
        r = self.app.start("terminal", str(self.path), "named")
        show = lambda *args: self.app.tmux.command(*args).stdout
        self.assertEqual(show("show-options", "-v", "-t", r.pane, "@hubi6-label").strip(), "$ terminal/named · only")
        self.assertIn("detach-client -s ="+r.session, show("show-hooks", "-p", "-t", r.pane))
        # Replacing the placeholder process must not leave a hook error parked in the pane.
        self.assertEqual(show("display-message", "-p", "-t", r.pane, "#{pane_in_mode}").strip(), "0")
        self.assertEqual(show("show-options", "-w", "-v", "-t", r.pane, "window-size").strip(), "latest")
        self.app.preflight()
        keys = show("list-keys", "-T", "prefix")
        self.assertFalse([line for line in keys.splitlines() if line.split()[3] == "C-z"], keys)
        self.assertEqual(self.app.tmux.titles().keys(), {r.session})

    def test_one_command_opens_and_tidies(self):
        client = Client(["--config", str(self.config), "terminal", "only"], ui="dashboard")
        self.addCleanup(client.close)
        w = self.only()
        self.attached(w.record.id)
        client.send(b"exit\n")
        self.assertEqual(client.p.wait(5), 0)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_dashboard_fault_falls_back_to_the_line_menu(self):
        script = ("from unittest.mock import patch;from hubi6.cli import main;import sys;"
                  "patch('hubi6.menu.Menu.draw',side_effect=RuntimeError('injected')).start();"
                  "sys.exit(main(['--config',"+repr(str(self.config))+"]))")
        client = Client([], command=[sys.executable, "-c", script], ui="dashboard")
        self.addCleanup(client.close)
        client.read_until(b"Choice + Enter:")
        self.assertIn("menu klasyczne".encode(), client.output)


if __name__ == "__main__":
    unittest.main()
