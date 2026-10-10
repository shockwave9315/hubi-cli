"""Small line-oriented UI. Screens render from one complete snapshot."""
from contextlib import contextmanager
import json
import os
import select
import signal
import sys
import termios
import time

from .core.model import HubiError, actions, display
from .github import GitHub


class SignalExit(BaseException):
    def __init__(self, signum):
        self.code = 128+signum


@contextmanager
def terminal(*, paste=False):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    handlers = {}
    def handle(signum, frame):
        raise SignalExit(signum)
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        handlers[sig] = signal.signal(sig, handle)
    try:
        if old:
            mode = termios.tcgetattr(fd)
            if paste:
                # Read bracketed-paste markers before Enter, while our reader
                # still accepts actions only as complete lines. Keep ISIG.
                mode[3] &= ~(termios.ICANON | termios.ECHO)
                mode[3] |= termios.ISIG
                mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
            termios.tcsetattr(fd, termios.TCSANOW, mode)
            if paste:
                sys.stdout.write("\033[?2004h")
                sys.stdout.flush()
        yield
    finally:
        if old:
            termios.tcsetattr(fd, termios.TCSANOW, old)
            if paste:
                sys.stdout.write("\033[?2004l\033[0m\033[?25h\n")
                sys.stdout.flush()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


class Input:
    def __init__(self, fd, *, echo=False, paste_timeout=5):
        self.fd = fd
        self.pending = bytearray()
        self.pasting = False
        self.paste_end = 0.0
        self.echo, self.paste_timeout = echo, paste_timeout

    def line(self):
        while True:
            if not self.pasting and b"\x1b[200~" in self.pending:
                self.pasting = True
                self.paste_end = time.monotonic()+self.paste_timeout
            if self.pasting:
                if b"\x1b[201~" in self.pending:
                    _, _, trailing = self.pending.partition(b"\x1b[201~")
                    self.pending = bytearray(trailing.lstrip(b"\r\n"))
                    self.pasting = False
                    return ""
                # Keep a possible split closing marker; discard all paste data.
                self.pending = self.pending[-5:]
            if b"\n" not in self.pending:
                timeout = max(0, self.paste_end-time.monotonic()) if self.pasting else None
                ready, _, _ = select.select([self.fd], [], [], timeout)
                if not ready:
                    self.pasting = False
                    self.pending.clear()
                    return ""
                chunk = os.read(self.fd, 4096)
                if not chunk:
                    return None
                if chunk == b"\x04" and not self.pending and not self.pasting:
                    return None
                if self.echo and not self.pasting and b"\x1b" not in chunk:
                    printable = chunk.decode("utf-8", errors="replace").replace("\n", "")
                    sys.stdout.write(display(printable)+( "\n" if b"\n" in chunk else ""))
                    sys.stdout.flush()
                self.pending.extend(chunk)
                if len(self.pending) > 65536:
                    if self.pasting or b"\x1b[200~" in self.pending:
                        self.pasting = True
                        self.paste_end = time.monotonic()+self.paste_timeout
                        self.pending = self.pending[-5:]
                        continue
                    self.pending.clear()
                    return ""
                if self.pasting or b"\x1b[200~" in self.pending:
                    continue
            if b"\n" not in self.pending:
                continue
            raw, _, rest = self.pending.partition(b"\n")
            self.pending = bytearray(rest)
            edited = bytearray()
            for c in raw:
                if c in (8, 127) and edited:
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
            rows = ["Hubi v6   runtime: "+snapshot.runtime_health, ""]
            choices = []
            if screen == "main":
                choices = snapshot.repos
                active = len(snapshot.workloads)
                rows += [f"Workloads: {active}   [w] workloads  [g] GitHub repositories  [q] shell  [x] exit SSH"]
                for index, ref in enumerate(choices[page*9:page*9+9], 1):
                    ws = [w for w in snapshot.workloads if w.record.repo.path == ref.path]
                    status = ", ".join(w.record.kind+":"+w.state.value for w in ws) or "no workloads"
                    rows.append(f"[{index}] {display(os.path.basename(ref.path))}  {status}")
            elif screen == "workloads":
                choices = snapshot.workloads
                for index, w in enumerate(choices[page*9:page*9+9], 1):
                    rows.append(f"[{index}] {w.record.kind}/{display(w.record.instance)}  {display(w.record.repo.path)}  "
                                f"{w.state.value}  repo:{w.repo_health.value}  clients:{w.session.attached if w.session else 0}")
            elif screen == "repo":
                rows += [display(selected.path), "[c] Codex  [l] Claude  [t] persistent terminal",
                         "[w] workloads  [i] inspect Git status"]
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
                            selected = github.clone(picked["nameWithOwner"])
                        screen = "repo"
                    page = 0
                elif screen == "repo" and action in ("c", "l", "t"):
                    sys.stdout.write("Instance [primary] + Enter: ")
                    sys.stdout.flush()
                    instance = reader.line()
                    if instance is None:
                        return 0
                    resume = False
                    if action != "t":
                        sys.stdout.write("Conversation [new/resume] + Enter: ")
                        sys.stdout.flush()
                        mode = reader.line()
                        if mode is None:
                            return 0
                        if mode not in ("", "new", "resume"):
                            raise HubiError("Choose new or resume")
                        resume = mode == "resume"
                    r = app.start({"c":"codex", "l":"claude", "t":"terminal"}[action],
                                  selected.path, instance or "primary", resume)
                    selected, screen = r.id, "detail"
                elif screen == "repo" and action == "i":
                    message = app.git.summary(selected)
                elif screen == "detail":
                    if action in ("a", "v", "s", "o"):
                        app.attach(selected, {"a":"normal", "v":"view", "s":"shared", "o":"takeover"}[action])
                    elif action == "i":
                        message = json.dumps(w.data(), ensure_ascii=True, indent=2)
                    elif action in ("d", "f"):
                        token = w.record.generation
                        sys.stdout.write("Type yes to stop this generation: ")
                        sys.stdout.flush()
                        if reader.line() == "yes":
                            app.stop(selected, expected=token, repair=action == "f")
                            screen = "workloads"
                elif screen == "github" and action == "r":
                    catalogue = github.list(refresh=True)
            except HubiError as e:
                message = str(e)
                if screen == "detail" and not any(w.record.id == selected for w in app.snapshot().workloads):
                    screen = "workloads"
