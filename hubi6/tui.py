"""Small line-oriented UI. Screens render from one complete snapshot."""
from contextlib import contextmanager
import codecs
import json
import os
import select
import signal
import sys
import termios
import time
import unicodedata

from .core.model import HubiError, actions, display
from .github import GitHub


class SignalExit(BaseException):
    def __init__(self, signum):
        self.code = 128+signum


@contextmanager
def terminal(*, paste=False):
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd) if os.isatty(fd) else None
    except (AttributeError, OSError, ValueError, termios.error):
        fd, old = None, None
    handlers = {}
    def handle(signum, frame):
        raise SignalExit(signum)
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        handlers[sig] = signal.signal(sig, handle)
    if paste:
        # A stopped menu cannot be continued from the keyboard when its parent is
        # the login shell still reading its startup files (SSH autologin): that
        # shell waits without job control. A handler, unlike SIG_IGN, is reset
        # to the default in every child process.
        handlers[signal.SIGTSTP] = signal.signal(signal.SIGTSTP, lambda signum, frame: None)
    try:
        if old:
            mode = termios.tcgetattr(fd)
            if paste:
                # Read bracketed-paste markers before Enter, while our reader
                # still accepts actions only as complete lines. Keep ISIG.
                mode[3] &= ~(termios.ICANON | termios.ECHO)
                mode[3] |= termios.ISIG
                mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
                # No keyboard suspend either: it would also stop helpers that
                # share the foreground process group.
                mode[6][termios.VSUSP] = b"\x00"
            termios.tcsetattr(fd, termios.TCSANOW, mode)
            if paste:
                sys.stdout.write("\033[?2004h")
                sys.stdout.flush()
        yield
    finally:
        try:
            if old:
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, old)
                except (termios.error, OSError):
                    pass  # The PTY may already have been hung up.
                if paste:
                    try:
                        sys.stdout.write("\033[?2004l\033[0m\033[?25h\n")
                        sys.stdout.flush()
                    except OSError:
                        pass
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


class Input:
    def __init__(self, fd, *, echo=False, paste_timeout=5):
        self.fd = fd
        self.pending = bytearray()
        self.pasting = False
        self.paste_end = 0.0
        self.echo, self.paste_timeout = echo, paste_timeout
        self.last_byte = 0.0
        self.quarantine_until = 0.0
        self.echo_buffer = []
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def arm(self):
        if os.isatty(self.fd):
            sys.stdout.write("\033[?2004h")
            sys.stdout.flush()

    def finish_line(self):
        # Return the action immediately. Rendering consumes the quiet window;
        # only the next logical read waits for its remaining part.
        self.pending.clear()
        self.quarantine_until = self.last_byte+.08

    def discard_burst(self, *, force=False):
        """Drain before the next read, until 80 ms since the last received byte."""
        if not self.quarantine_until and not force:
            return
        self.pending.clear()
        deadline = max(self.quarantine_until, time.monotonic()+.08 if force else 0)
        while True:
            ready, _, _ = select.select([self.fd], [], [], max(0, deadline-time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.last_byte = time.monotonic()
            deadline = self.last_byte+.08
        self.quarantine_until = 0.0

    def echo_chunk(self, chunk):
        for char in self.decoder.decode(chunk):
            if char in ("\b", "\x7f"):
                if self.echo_buffer:
                    previous = self.echo_buffer.pop()
                    width = 0 if unicodedata.combining(previous) else (2 if unicodedata.east_asian_width(previous) in "WF" else 1)
                    sys.stdout.write("\b \b"*width)
            elif char in ("\n", "\r"):
                sys.stdout.write("\n")
                break  # Do not echo the discarded remainder of a multiline burst.
            else:
                self.echo_buffer.append(char)
                sys.stdout.write(display(char))
        sys.stdout.flush()

    def prompt(self, text, *, destructive=False):
        if destructive:
            # Drain BEFORE exposing the confirmation prompt. Normal typing
            # after the prompt must never be flushed as stale input.
            self.arm()
            self.discard_burst(force=not bool(self.quarantine_until))
            if os.isatty(self.fd):
                termios.tcflush(self.fd, termios.TCIFLUSH)
        self.arm()
        sys.stdout.write(text)
        sys.stdout.flush()
        return self.line(armed=True)

    def line(self, *, armed=False):
        if not armed:
            self.arm()
        self.discard_burst()
        self.echo_buffer.clear()
        self.decoder.reset()
        while True:
            if not self.pasting and b"\x1b[200~" in self.pending:
                self.pasting = True
                self.paste_end = time.monotonic()+self.paste_timeout
            if self.pasting:
                if b"\x1b[201~" in self.pending:
                    self.pasting = False
                    self.finish_line()
                    return ""
                # Keep a possible split closing marker; discard all paste data.
                self.pending = self.pending[-5:]
            if b"\n" not in self.pending:
                timeout = max(0, self.paste_end-time.monotonic()) if self.pasting else None
                ready, _, _ = select.select([self.fd], [], [], timeout)
                if not ready:
                    self.pasting = False
                    self.finish_line()
                    return ""
                chunk = os.read(self.fd, 4096)
                if not chunk:
                    return None
                if chunk == b"\x04" and not self.pending and not self.pasting:
                    return None
                self.last_byte = time.monotonic()
                if self.echo and not self.pasting and b"\x1b" not in chunk:
                    self.echo_chunk(chunk)
                self.pending.extend(chunk)
                if len(self.pending) > 65536:
                    if self.pasting or b"\x1b[200~" in self.pending:
                        self.pasting = True
                        self.paste_end = time.monotonic()+self.paste_timeout
                        self.pending = self.pending[-5:]
                        continue
                    self.finish_line()
                    return ""
                if self.pasting or b"\x1b[200~" in self.pending:
                    continue
            if b"\n" not in self.pending:
                continue
            raw, _, _ = self.pending.partition(b"\n")
            self.finish_line()
            edited = bytearray()
            for c in raw:
                if c in (8, 127):
                    if edited:
                        while edited and (edited[-1] & 0xc0) == 0x80:
                            edited.pop()
                        if edited:
                            edited.pop()
                else:
                    edited.append(c)
            raw = bytes(edited)
            if b"\x1b" in raw or any(c < 32 and c != 13 for c in raw):
                return ""
            try:
                return raw.decode("utf-8").strip()
            except UnicodeError:
                return ""


def run(app):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise HubiError("Menu requires a TTY; use list --json, start, stop or doctor")
    if os.environ.get("HUBI6_ACTIVE"):
        raise HubiError("Hubi is already active")
    with terminal(paste=True):
        reader = Input(sys.stdin.fileno(), echo=True)
        screen, selected, page, message = "main", None, 0, ""
        github = GitHub(app)
        catalogue = None
        while True:
            snapshot = app.snapshot()
            try:
                home = app.git.home()
            except HubiError:
                home = None
            rows = ["Hubi v6   runtime: "+snapshot.runtime_health, ""]
            choices = []
            if screen == "main":
                choices = snapshot.repos
                active = len(snapshot.workloads)
                rows += [f"Workloads: {active}   [w] workloads  {'[h] home (no repository)  ' if home else ''}[g] GitHub repositories  [q] shell  [x] exit SSH"]
                rows += [f"{w.record.kind}/{display(w.record.instance)} {w.state.value} repo:{w.repo_health.value}  {display(w.record.repo.path)}"
                         for w in snapshot.workloads if w.repo_health.value != "OK"]
                for index, ref in enumerate(choices[page*9:page*9+9], 1):
                    ws = [w for w in snapshot.workloads if w.record.repo.path == ref.path]
                    status = ", ".join(w.record.kind+":"+w.state.value+"/"+w.repo_health.value for w in ws) or "no workloads"
                    rows.append(f"[{index}] {display(os.path.basename(ref.path))}  {status}")
            elif screen == "workloads":
                choices = snapshot.workloads
                for index, w in enumerate(choices[page*9:page*9+9], 1):
                    rows.append(f"[{index}] {w.record.kind}/{display(w.record.instance)}  {display(w.record.repo.path)}  "
                                f"{w.state.value}  repo:{w.repo_health.value}  clients:{w.session.attached if w.session else 0}")
            elif screen == "repo":
                rows += [display(selected.path)+("  (home, no repository)" if selected.plain else ""),
                         "[c] Codex  [l] Claude  [t] persistent terminal",
                         "[w] workloads" if selected.plain else "[w] workloads  [i] inspect Git status"]
            elif screen == "detail":
                try:
                    w = app.find(selected, snapshot)
                except HubiError:
                    screen, page, message = "workloads", 0, "Workload was stopped by another client"
                    continue
                rows += [display(w.record.repo.path), f"{w.record.kind}/{display(w.record.instance)} {w.state.value} repo:{w.repo_health.value}",
                         f"Clients: {w.session.attached if w.session else 0}  Age: {int(time.time()-w.record.created)}s",
                         "  ".join(label for action, label in (("attach", "[a] attach"), ("view", "[v] view"),
                          ("shared", "[s] share"), ("takeover", "[o] takeover"), ("stop", "[d] stop"),
                          ("inspect", "[i] inspect"), ("reconcile", "[f] reconcile")) if action in actions(w.state))]
            else:
                if catalogue is None:
                    try:
                        catalogue = github.cached() or github.list()
                    except HubiError as e:
                        screen, message = "main", str(e)
                        continue
                choices = github.view(catalogue)
                rows += ["GitHub repositories  [r] refresh"+ ("  CACHE / STALE" if catalogue.stale else ""), catalogue.error]
                for index, repo in enumerate(choices[page*9:page*9+9], 1):
                    flags = " ".join(k for k, v in (("private", repo["isPrivate"]), ("fork", repo["isFork"]), ("archived", repo["isArchived"])) if v)
                    rows.append(f"[{index}] {repo['nameWithOwner']}  {'LOCAL' if repo['local'] else 'NOT LOCAL'}  {flags}")
            if not choices and screen in ("main", "workloads", "github"):
                rows.append("No entries")
            if choices:
                rows.append(f"Page {page+1}/{max(1, (len(choices)+8)//9)}  [>] next  [<] previous")
            rows += [display(i) for i in snapshot.issues[:3]]
            rows += ["[b] back  [q] shell  [x] exit SSH"]
            rows += [display(line) for line in message.splitlines()[:24]]
            rows += ["Choice + Enter: "]
            sys.stdout.write("\033[2J\033[H"+"\n".join(rows))
            sys.stdout.flush()
            message = ""
            action = reader.line()
            if action is None:
                return 0
            if action == "q":
                return 98
            if action == "x":
                return 99
            try:
                if action in (">", "<"):
                    page = min(max(0, page+(1 if action == ">" else -1)), max(0, (len(choices)-1)//9))
                elif action == "b":
                    screen, page = "main", 0
                elif action == "w":
                    screen, page = "workloads", 0
                elif action == "g" and screen == "main":
                    screen, page = "github", 0
                elif action == "h" and screen == "main" and home:
                    selected, screen, page = app.git.validate(home), "repo", 0
                elif len(action) == 1 and action in "123456789" and (page*9+int(action)-1) < len(choices):
                    picked = choices[page*9+int(action)-1]
                    if screen == "main":
                        selected, screen = picked, "repo"
                    elif screen == "workloads":
                        selected, screen = picked.record.id, "detail"
                    elif screen == "github":
                        if picked["local"]:
                            selected = app.git.resolve(picked["local_paths"][0])
                        else:
                            answer = reader.prompt("Clone "+picked["nameWithOwner"]+" to "+str(app.c.repos/picked["nameWithOwner"].split("/")[1])+"? Type yes: ", destructive=True)
                            if answer is None:
                                return 0
                            if answer != "yes":
                                message = "Clone cancelled"
                                continue
                            sys.stdout.write("Cloning...\n")
                            sys.stdout.flush()
                            selected = github.clone(picked["nameWithOwner"])
                            message = "Cloned "+picked["nameWithOwner"]
                        screen = "repo"
                    page = 0
                elif screen == "repo" and action in ("c", "l", "t"):
                    instance = reader.prompt("Instance [primary] + Enter: ")
                    if instance is None:
                        return 0
                    resume = False
                    if action != "t":
                        mode = reader.prompt("Conversation [new/resume] + Enter: ")
                        if mode is None:
                            return 0
                        if mode not in ("", "new", "resume"):
                            raise HubiError("Choose new or resume")
                        resume = mode == "resume"
                    r = app.start({"c":"codex", "l":"claude", "t":"terminal"}[action],
                                  selected.path, instance or "primary", resume)
                    selected, screen = r.id, "detail"
                elif screen == "repo" and action == "i" and not selected.plain:
                    message = app.git.summary(selected)
                elif screen == "detail":
                    if action in ("a", "v", "s", "o"):
                        try:
                            app.attach(selected, {"a":"normal", "v":"view", "s":"shared", "o":"takeover"}[action])
                        finally:
                            reader.arm()
                            # The client owned input while attached. Reestablish
                            # a fresh boundary, but let the screen render now.
                            reader.last_byte = time.monotonic()
                            reader.finish_line()
                    elif action == "i":
                        message = json.dumps(w.data(), ensure_ascii=True, indent=2)
                    elif action in ("d", "f"):
                        token = w.record.generation
                        text = "Type yes to repair/reconcile this generation: " if action == "f" else "Type yes to stop this generation: "
                        if reader.prompt(text, destructive=True) == "yes":
                            app.stop(selected, expected=token, repair=action == "f")
                            screen = "workloads"
                elif screen == "github" and action == "r":
                    catalogue = github.list(refresh=True)
            except HubiError as e:
                message = str(e)
                if screen == "detail" and not any(w.record.id == selected for w in app.snapshot().workloads):
                    screen = "workloads"
