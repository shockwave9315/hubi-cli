"""The Codex limit guard (strażnik), seen and steered from Hubi without an agent.

The guard is a shell script with its own directory and a transient systemd user
unit (see the README in that directory). Hubi never edits the script's state,
lock or consent files, never runs the reset helper and never writes to Codex. It
reads the directory and the unit, and it starts and stops the unit the way the
README does. A restart keeps the unit's PATH, WD_* settings and restart options.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import time

from .core.model import HubiError, display
from .storage import atomic_json, private_json

try:
    from zoneinfo import ZoneInfo
    POLISH = ZoneInfo("Europe/Warsaw")
except Exception:                                   # no tz database: say UTC instead of guessing
    POLISH = None

CP = re.compile(r"CP[0-9]+[a-z]?")
# The guard's own rule (spec_ok in watchdog.sh): kind/repo[/name].
NAME = re.compile(r"(codex|claude)/([A-Za-z0-9._-]+)(?:/([A-Za-z0-9][A-Za-z0-9_-]*))?")
VERSION_FILE = re.compile(r"watchdog\.v([0-9])([0-9]*)\.sh")
UNIT = re.compile(r"[A-Za-z0-9_.:@-]+")
ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
SAFE = re.compile(r"[A-Za-z0-9_./:%+,@=-]*")
LOG_LINE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (\S+) (\S+) ([A-Z0-9_]+): (.*)")
PAUSED = ("PAUSE_REQUESTED", "PAUSED_5H", "RESUME_REQUESTED")
WEEKLY = "WEEKLY_STOP"
STATE_KEYS = ("CP", "STATE", "PAUSE_WINDOW", "PAUSE_SENT_AT", "RESUMED_AT", "WEEK_SENT", "NOTIFIED",
              "RESET_KEY", "RESET_TRIES", "REPAUSES", "REPAUSE_WIN")
# What the README starts the guard with, used only when no earlier setup is known.
DEFAULT_ENV = (("WD_IDLE_MIN", "720"), ("WD_LOW", "7"), ("WD_WEEK_LOW", "1"))
DEFAULT_RESTART = (("Restart", "on-failure"), ("RestartSec", "30s"), ("RestartPreventExitStatus", "1 2 3"))
RESTART_VALUES = {"no", "always", "on-success", "on-failure", "on-abnormal", "on-abort", "on-watchdog"}
LIMIT_KEYS = ("status", "five_left", "five_reset", "week_left", "week_reset", "allowed", "reason")
LIMITS_EVERY = 120                                  # README: limits.py at most every 2 minutes
# What `hubi guard limits` sets, with the defaults of watchdog.sh itself (not the README start).
THRESHOLDS = (("WD_LOW", "7"), ("WD_WEEK_LOW", "1"), ("WD_IDLE_MIN", "60"), ("WD_MAX_HOURS", "24"))
THRESHOLD_RANGES = {"WD_LOW": (0, 100), "WD_WEEK_LOW": (0, 100),
                    "WD_IDLE_MIN": (1, 10080),                 # at most a week
                    "WD_MAX_HOURS": (1, 720)}                  # at most 30 days
RUNNING_STATES = ("active", "activating", "deactivating", "reloading", "refreshing")
STATE_PL = {
    "RUNNING": "Codex pracuje, strażnik pilnuje limitu",
    "PAUSE_REQUESTED": "wysłano LIMIT-5, czeka na pauzę Codexa",
    "PAUSED_5H": "pauza do odnowienia limitu 5 h",
    "RESUME_REQUESTED": "wysłano „wznów”, czeka, aż Codex ruszy",
    WEEKLY: "blokada tygodniowa: strażnik nie wystartuje",
}


def when(epoch, *, day=True):
    """Polish wall-clock time of an epoch, e.g. 24.10 17:49."""
    moment = datetime.datetime.fromtimestamp(epoch, POLISH or datetime.timezone.utc)
    text = moment.strftime("%d.%m %H:%M" if day else "%H:%M")
    return text if POLISH else text+" UTC"


def zone():
    return "czas polski" if POLISH else "UTC"


def canonical(name, *, kind=None):
    """kind/repo[/name] as the guard needs it; the default session is never spelt out.

    The guard keys its state file on this text: codex/praca and codex/praca/1
    are one session to Hubi but two state files to the guard.
    """
    if not isinstance(name, str) or name.startswith("%"):
        raise HubiError("Guard sessions are Hubi names like codex/praca, not pane numbers")
    match = NAME.fullmatch(name.strip())
    if not match or (kind and match[1] != kind):
        raise HubiError(f"Invalid guard session name: {display(name)}")
    instance = match[3] if match[3] not in (None, "1", "primary") else None
    return "/".join(p for p in (match[1], match[2], instance) if p)


def key_of(codex):
    return codex.replace("/", "-")


def threshold(key, value):
    """One threshold as watchdog.sh takes it: a plain whole number, never "7%"."""
    low, high = THRESHOLD_RANGES[key]
    text = str(value).strip()
    if re.fullmatch(r"[0-9]{1,5}", text) and low <= int(text) <= high:
        return str(int(text))                             # 007 -> 7: the log and the menu show plain numbers
    shown = display(text) or "\"\""
    if key == "WD_LOW":
        raise HubiError(f"The 5-hour pause threshold must be a whole number 0-100, without %: {shown}")
    if key == "WD_WEEK_LOW":
        raise HubiError(f"The weekly stop threshold must be a whole number 0-100, without %: {shown}")
    if key == "WD_IDLE_MIN":
        raise HubiError(f"Idle minutes must be a whole number 1-10080: {shown}")
    raise HubiError(f"Max hours must be a whole number 1-720: {shown}")


def version_label(path):
    match = VERSION_FILE.fullmatch(Path(path).name)
    return f"v{match[1]}.{match[2]}" if match and match[2] else (f"v{match[1]}" if match else "")


def version_file(label):
    match = re.fullmatch(r"v?([0-9])\.?([0-9]*)", label.strip())
    if not match:
        raise HubiError(f"Invalid guard version: {display(label)}")
    return f"watchdog.v{match[1]}{match[2]}.sh"


@dataclass(frozen=True)
class Setup:
    """What the guard runs with: arguments, environment and restart options."""
    cp: str
    codex: str
    contact: str
    env: tuple = DEFAULT_ENV
    restart: tuple = DEFAULT_RESTART

    def __post_init__(self):
        if not CP.fullmatch(self.cp):
            raise HubiError(f"Invalid checkpoint name: {display(self.cp)}")
        if canonical(self.codex, kind="codex") != self.codex:
            raise HubiError(f"Invalid guard session name: {display(self.codex)}")
        contact = canonical(self.contact)
        if contact != self.contact:
            raise HubiError(f"Invalid guard session name: {display(self.contact)}")
        # The guard's own rule: another Codex can be the contact only under its own name.
        if contact.startswith("codex/") and (contact == self.codex or contact.count("/") != 2):
            raise HubiError("The contact must be a Claude session or another named Codex session")
        for key, value in self.env:
            if not ENV_KEY.fullmatch(key) or not SAFE.fullmatch(value):
                raise HubiError(f"Unusual guard setting {display(key)}; change the guard by hand")
        options = dict(self.restart)
        if (set(options) != {"Restart", "RestartSec", "RestartPreventExitStatus"} or options["Restart"] not in RESTART_VALUES
                or not re.fullmatch(r"[0-9a-zµ ]+", options["RestartSec"])
                or not re.fullmatch(r"[0-9A-Z ]*", options["RestartPreventExitStatus"])):
            raise HubiError("Unusual guard restart options; change the guard by hand")

    def data(self):
        return {"cp": self.cp, "codex": self.codex, "contact": self.contact,
                "env": dict(self.env), "restart": dict(self.restart)}

    @classmethod
    def parse(cls, data):
        try:
            return cls(data["cp"], data["codex"], data["contact"], tuple(sorted(data["env"].items())),
                       tuple((k, data["restart"][k]) for k, _ in DEFAULT_RESTART))
        except (KeyError, TypeError, AttributeError) as e:
            raise HubiError("Saved guard setup is unreadable") from e

    def argv(self):
        return ["bash", "./watchdog.sh", self.cp, self.codex, self.contact]

    def words(self):
        return f"{self.cp} {self.codex} {self.contact}"

    def thresholds(self):
        """The thresholds this guard runs with: its WD_* setting, else the script's default."""
        env = dict(self.env)
        return {key: env.get(key, default) for key, default in THRESHOLDS}

    def with_thresholds(self, values):
        return replace(self, env=tuple(sorted({**dict(self.env), **values}.items())))


@dataclass
class Status:
    configured: bool
    unit: str = "unknown"            # systemd ActiveState, "" = no such unit
    since: float | None = None
    setup: Setup | None = None
    source: str = ""                 # "unit": read from the running unit; "saved": last one Hubi knew
    problem: str = ""                # why the running unit's setup cannot be reused
    state: dict = field(default_factory=dict)
    versions: list = field(default_factory=list)
    changed: bool = False            # watchdog.sh replaced after the unit started
    log: list = field(default_factory=list)
    limits: dict | None = None
    limits_at: float | None = None
    resume_delay: int = 120

    @property
    def running(self):
        return self.unit in RUNNING_STATES

    def data(self):
        """The JSON shape of `hubi list --json` and `hubi guard status --json`."""
        if not self.configured:
            return {"configured": False}
        setup = self.setup.data() if self.setup else None
        if setup:
            setup["env"] = {k: v for k, v in setup["env"].items() if k != "PATH"}
            setup["thresholds"] = self.setup.thresholds()
        return {"configured": True, "unit": self.unit, "running": self.running, "since": self.since,
                "setup": setup, "setup_source": self.source, "problem": self.problem, "state": dict(self.state),
                "versions": list(self.versions), "changed_after_start": self.changed, "resume_at": self.resume_at,
                "limits": self.limits, "limits_at": self.limits_at, "log": list(self.log)}

    @property
    def resume_at(self):
        window = self.state.get("PAUSE_WINDOW", "")
        if self.state.get("STATE") in PAUSED and window.isdigit():
            return int(window)+self.resume_delay
        return None


class Guard:
    def __init__(self, app, *, clock=time.time, sleep=time.sleep):
        self.app, self.c, self.r = app, app.c, app.r
        self.dir = self.c.guard_dir
        self.unit = self.c.guard_unit+".service"
        self.saved_path = self.c.state_base/"guard.json"
        self.limits_path = self.c.state_base/"guard-limits.json"
        self.clock, self.sleep = clock, sleep

    @property
    def configured(self):
        return self.dir is not None and (self.dir/"watchdog.sh").is_file()

    # ---- reading

    def unit_info(self):
        p = self.r.run(["systemctl", "--user", "show", self.unit, "--no-pager", "--timestamp=unix",
                        "--property=Id,LoadState,ActiveState,MainPID,ActiveEnterTimestamp,ExecStart,Environment,"
                        "Restart,RestartUSec,RestartPreventExitStatus,WorkingDirectory"], check=False, timeout=15)
        fields = {}
        for line in p.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key] = value
        if p.returncode or fields.get("Id") != self.unit:
            return {"state": "unknown"}
        state = "" if fields.get("LoadState") == "not-found" else fields.get("ActiveState", "unknown")
        since = fields.get("ActiveEnterTimestamp", "")
        fields["state"] = state
        fields["since"] = float(since[1:]) if re.fullmatch(r"@[0-9]+(\.[0-9]+)?", since) else None
        return fields

    def setup_of(self, info):
        """The running unit's setup, or HubiError when it was started some other way."""
        argv = re.search(r"argv\[\]=(.*?) ;", info.get("ExecStart", ""))
        words = argv[1].split(" ") if argv else []
        cwd = info.get("WorkingDirectory", "")
        if (len(words) != 5 or Path(words[0]).name != "bash" or words[1] not in ("./watchdog.sh", str(self.dir/"watchdog.sh"))
                or not cwd.startswith("/") or Path(cwd).resolve() != self.dir.resolve()):
            raise HubiError("The guard unit was started some other way; change it by hand")
        try:
            entries = shlex.split(info.get("Environment", ""))
        except ValueError as e:
            raise HubiError("Unusual guard setting Environment; change the guard by hand") from e
        env = []
        for entry in entries:
            key, sep, value = entry.partition("=")
            if not sep:
                raise HubiError("Unusual guard setting Environment; change the guard by hand")
            env.append((key, value))
        restart = (("Restart", info.get("Restart", "")), ("RestartSec", info.get("RestartUSec", "")),
                   ("RestartPreventExitStatus", info.get("RestartPreventExitStatus", "")))
        return Setup(words[2], words[3], words[4], tuple(sorted(env)), restart)

    def saved(self):
        try:
            return Setup.parse(private_json(self.saved_path))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, HubiError):
            return None                                # never block the guard on Hubi's own memo

    def remember(self, setup):
        if setup and self.saved() != setup:
            self.c.prepare()
            atomic_json(self.saved_path, setup.data())

    def read_state(self, codex):
        """The guard's state file for one Codex: known keys and plain values only."""
        return self.read_state_file("state."+key_of(codex))

    def paused_files(self):
        """Every state file of a Codex named the Hubi way that holds a pause.

        Files named by a pane number (state.5) are v3.2 leftovers and never count.
        """
        return sorted(path.name for path in self.dir.glob("state.codex-*")
                      if self.read_state_file(path.name).get("STATE") in PAUSED)

    def read_state_file(self, name):
        state = {}
        try:
            with open(self.dir/name, encoding="utf-8", errors="replace") as f:
                lines = f.read(65536).splitlines()
        except FileNotFoundError:
            return state
        except OSError:
            return {"STATE": "?"}
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and key in STATE_KEYS and re.fullmatch(r"[A-Za-z0-9_,-]*", value):
                state[key] = value
        return state

    def versions(self):
        """(label, file) of every kept version, oldest first."""
        found = [(version_label(p), p) for p in self.dir.glob("watchdog.v*.sh") if version_label(p)]
        return sorted(found, key=lambda v: [int(x) for x in v[0][1:].split(".")])

    def current_versions(self):
        """Labels of the kept versions identical to watchdog.sh (only same-size files are read)."""
        try:
            current = (self.dir/"watchdog.sh").read_bytes()
        except OSError:
            return []
        same = []
        for label, path in self.versions():
            try:
                if path.stat().st_size == len(current) and path.read_bytes() == current:
                    same.append(label)
            except OSError:
                pass
        return same

    def log_tail(self, lines=5):
        try:
            with open(self.dir/"log", "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell()-32768))
                tail = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        return tail[-lines:] if lines else []

    def cached_limits(self):
        try:
            data = private_json(self.limits_path)
            return data["fields"], float(data["at"])
        except (OSError, ValueError, KeyError, TypeError, HubiError):
            return None, None

    def status(self, *, remember=False, log_lines=5):
        if not self.configured:
            return Status(False)
        info = self.unit_info()
        s = Status(True, info["state"], info.get("since"))
        if s.running:
            try:
                s.setup, s.source = self.setup_of(info), "unit"
            except HubiError as e:
                s.problem = str(e)
        if s.setup is None:
            s.setup, s.source = self.saved(), "saved"
            if s.setup is None:
                s.source = ""
        elif remember:
            self.remember(s.setup)
        if s.setup:
            s.state = self.read_state(s.setup.codex)
            delay = dict(s.setup.env).get("WD_RESUME_DELAY", "120")
            s.resume_delay = int(delay) if delay.isdigit() else 120
        s.versions = self.current_versions()
        try:
            s.changed = bool(s.running and s.since and (self.dir/"watchdog.sh").stat().st_ctime > s.since+1)
        except OSError:
            pass
        s.log = self.log_tail(log_lines)
        s.limits, s.limits_at = self.cached_limits()
        return s

    def limits(self, setup=None):
        """Read-only limits from limits.py, at most once every two minutes (README)."""
        fields, at = self.cached_limits()
        if fields is not None and self.clock()-at < LIMITS_EVERY:
            return fields, at
        env = dict(os.environ)
        if setup and dict(setup.env).get("PATH"):
            env["PATH"] = dict(setup.env)["PATH"]
        p = self.r.run(["python3", "-I", str(self.dir/"limits.py")], env=env, cwd=str(self.dir), check=False, timeout=60)
        fields = {}
        for word in p.stdout.split():
            key, sep, value = word.partition("=")
            if sep and key in LIMIT_KEYS and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                fields[key] = value
        if not fields:
            fields = {"status": "error", "reason": f"exit-{p.returncode}"}
        at = self.clock()
        self.c.prepare()
        atomic_json(self.limits_path, {"fields": fields, "at": at})
        return fields, at

    # ---- control

    @contextmanager
    def lock(self):
        self.c.prepare()
        fd = os.open(self.c.state_base/"guard.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            end = time.monotonic()+60
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise HubiError("Another guard change is in progress; try again")
                    time.sleep(.1)
            yield
        finally:
            os.close(fd)

    def require(self):
        if not self.configured:
            raise HubiError("Guard is not configured: no watchdog.sh in the guard directory")
        if not UNIT.fullmatch(self.c.guard_unit):
            raise HubiError("Invalid guard unit name in the configuration")

    def check_weekly(self, codex):
        state = self.read_state(codex)
        if state.get("STATE") == WEEKLY:
            raise HubiError(f"Weekly limit lock in state.{key_of(codex)}: when the limit is back, send Codex "
                            f"\"{state.get('CP', 'CP')} wznów\", delete that file and start the guard again")

    def check_change(self, old, new):
        """Changing CP or the guarded Codex is allowed only while that Codex is RUNNING.

        In a pause the guard keeps the pause under the CP name: a new CP or
        another Codex would drop it, and the paused Codex would never get "wznów".
        Restarting with the same CP and Codex is always allowed.
        """
        if old is not None and new.cp == old.cp and new.codex == old.codex:
            return
        mine = "state."+key_of(new.codex)
        # Without a known last setup, any Codex may be the one waiting for "wznów".
        files = {mine, "state."+key_of(old.codex)} if old else {mine, *self.paused_files()}
        for name in sorted(files):
            state = self.read_state_file(name)
            if state.get("STATE") in PAUSED and (name != mine or new.cp != state.get("CP")):
                raise HubiError(f"Guard is in {state['STATE']} for {name}: a new CP or Codex would lose the pause; "
                                "wait for RUNNING or restart with the same CP and Codex")

    def check_session(self, name):
        self.app.git.resolve(name.split("/")[1])                     # an unknown repository is an error, not an endless wait

    def stop(self):
        with self.lock():
            self.require()
            info = self.unit_info()
            if info["state"] == "unknown":
                raise HubiError("Systemd state UNKNOWN; operation refused")
            if info["state"] not in RUNNING_STATES:
                return False
            try:
                self.remember(self.setup_of(info))
            except HubiError:
                pass
            self._stop()
            return True

    def _stop(self):
        self.r.run(["systemctl", "--user", "stop", self.unit], timeout=90)
        if self.unit_info()["state"] in RUNNING_STATES:
            raise HubiError("The guard did not stop; check systemctl --user status "+self.unit)

    def start(self, setup=None):
        """Start the guard; without a setup, the last one Hubi knows."""
        with self.lock():
            self.require()
            info = self.unit_info()
            if info["state"] == "unknown":
                raise HubiError("Systemd state UNKNOWN; operation refused")
            if info["state"] in RUNNING_STATES:
                raise HubiError("The guard is already running; use cp, who or version to change it")
            last = self.saved()
            if setup is None:
                if last is None:
                    raise HubiError("No earlier guard setup known; give CP, Codex and contact")
                setup = last
            elif last and setup.cp == last.cp and setup.codex == last.codex:
                setup = replace(last, contact=setup.contact)  # the same guard: keep its settings
            elif last:
                setup = replace(setup, env=last.env, restart=last.restart)
            self.check_change(last, setup)
            self.check_session(setup.codex)
            self.check_session(setup.contact)
            self.check_weekly(setup.codex)
            self._start(setup, failed=info["state"] == "failed")
            return setup

    def _start(self, setup, *, failed=False):
        path = dict(setup.env).get("PATH") or os.environ.get("PATH", "")
        if not shutil.which("codex", path=path):
            # The guard reads limits through codex; a short PATH silently breaks it.
            raise HubiError("The guard PATH has no codex; start Hubi from a login shell")
        if failed:
            self.r.run(["systemctl", "--user", "reset-failed", self.unit], check=False)
        env = dict(setup.env)
        env.setdefault("PATH", path)
        argv = ["systemd-run", "--user", "--unit="+self.c.guard_unit, "--collect", "--same-dir", "--quiet"]
        argv += ["--setenv="+k+"="+v for k, v in sorted(env.items())]
        for key, value in setup.restart:
            argv += ["-p", f"{key}={value}"]
        argv += ["--"]+setup.argv()
        started = self.clock()
        self.r.run(argv, cwd=str(self.dir), timeout=30)
        # The guard refuses bad arguments, a second instance and the weekly lock
        # within its first second. Report that instead of a false "started", and
        # keep the last setup that worked.
        for _ in range(8):
            self.sleep(.25)
        info = self.unit_info()
        if info["state"] not in RUNNING_STATES:
            raise HubiError("The guard stopped right after start: "+self.journal(started))
        self.remember(replace(setup, env=tuple(sorted(env.items()))))

    def journal(self, since):
        p = self.r.run(["journalctl", "--user", "--unit="+self.unit, f"--since=@{int(since)}", "--output=cat",
                        "--no-pager", "--lines=6"], check=False, timeout=15)
        lines = [display(line) for line in p.stdout.splitlines() if line.strip()]
        return " / ".join(lines[-3:]) or "no reason in the journal"

    def restart_with(self, change):
        """Stop the running guard and start it again with change(setup)."""
        with self.lock():
            return self._restart_with(change)

    def _restart_with(self, change):
        self.require()
        info = self.unit_info()
        if info["state"] == "unknown":
            raise HubiError("Systemd state UNKNOWN; operation refused")
        running = info["state"] in RUNNING_STATES
        old = self.setup_of(info) if running else self.saved()
        if old is None:
            raise HubiError("No earlier guard setup known; give CP, Codex and contact")
        new = change(old)
        self.check_change(old, new)
        self.check_session(new.codex)
        self.check_session(new.contact)
        self.check_weekly(new.codex)
        if running:
            self.remember(old)
            self._stop()
        try:
            self._start(new, failed=info["state"] == "failed")
        except HubiError as e:
            if not running:
                raise
            self.go_back(old, e)
            raise HubiError(f"The new guard setup did not start: {e}; the guard runs again with {old.words()}") from e
        return old, new

    def go_back(self, old, error, script=None):
        """The guard was running a moment ago: never leave Codex without it."""
        try:
            if script is not None:
                self.install(script)
            self._start(old, failed=True)
        except (HubiError, OSError) as again:
            raise HubiError(f"Guard stopped; the new start failed: {error}; going back failed too: {again}. "
                            f"Previous setup: hubi guard start {old.words()}") from again

    def install(self, source):
        """watchdog.sh becomes a copy of source by rename: bash reads a script while running it."""
        fd, temp = tempfile.mkstemp(prefix=".hubi6-watchdog-", dir=self.dir)
        os.close(fd)
        try:
            shutil.copy2(source, temp)               # like cp -p: mode and times of the version
            os.replace(temp, self.dir/"watchdog.sh")
        finally:
            Path(temp).unlink(missing_ok=True)

    def change_cp(self, cp):
        cp = cp.strip()
        if not CP.fullmatch(cp):
            raise HubiError(f"Invalid checkpoint name: {display(cp)}")
        return self.restart_with(lambda old: replace(old, cp=cp))

    def change_who(self, codex, contact):
        codex, contact = canonical(codex, kind="codex"), canonical(contact)
        return self.restart_with(lambda old: replace(old, codex=codex, contact=contact))

    def change_thresholds(self, values):
        """New WD_LOW, WD_WEEK_LOW (and idle or max hours) for the same CP and Codex.

        A running guard restarts with them: the same CP and Codex keep any pause,
        because the guard reads its state back from the file. Only the weekly lock
        refuses, as for every start. A guard that is not running only gets them
        saved for its next start. Returns (old, new, restarted).
        """
        values = {key: threshold(key, value) for key, value in values.items()}
        if not values:
            raise HubiError("Give the thresholds to change")
        with self.lock():
            self.require()
            info = self.unit_info()
            if info["state"] == "unknown":
                raise HubiError("Systemd state UNKNOWN; operation refused")
            if info["state"] not in RUNNING_STATES:
                old = self.saved()
                if old is None:
                    raise HubiError("No earlier guard setup known; give CP, Codex and contact")
                new = old.with_thresholds(values)
                self.remember(new)
                return old, new, False
            old = self.setup_of(info)
            if old.with_thresholds(values) == old:
                return old, old, False
            old, new = self._restart_with(lambda setup: setup.with_thresholds(values))
            return old, new, True

    def switch_version(self, label):
        """Make watchdog.sh a copy of one kept version; restart the guard if it runs."""
        with self.lock():
            self.require()
            name = version_file(label)
            source = self.dir/name
            if not source.is_file():
                raise HubiError(f"No such guard version: {name}")
            target = self.dir/"watchdog.sh"
            if source.read_bytes() == target.read_bytes():
                return False
            previous = self.current_versions()
            if not previous:
                # Overwriting would destroy an edit kept nowhere else.
                raise HubiError("watchdog.sh matches no kept watchdog.v*.sh; save it as a version first")
            back = self.dir/version_file(previous[-1])
            info = self.unit_info()
            if info["state"] == "unknown":
                raise HubiError("Systemd state UNKNOWN; operation refused")
            running = info["state"] in RUNNING_STATES
            setup = self.setup_of(info) if running else None
            if running:
                self.check_weekly(setup.codex)
                self.remember(setup)
                self._stop()
            try:
                self.install(source)
            except OSError as e:
                # The rename never happened: watchdog.sh is still the script that ran.
                reason = HubiError(f"Could not replace watchdog.sh ({type(e).__name__})")
                if running:
                    self.go_back(setup, reason)
                    raise HubiError(f"Could not replace watchdog.sh ({type(e).__name__}); the guard runs as before") from e
                raise reason from e
            if running:
                try:
                    self._start(setup)
                except HubiError as e:
                    # e.g. v4.8 takes no Codex as the contact: back to the version that ran.
                    self.go_back(setup, e, back)
                    raise HubiError(f"Guard version {version_label(source)} did not start: {e}; "
                                    f"back to {previous[-1]}, the guard runs") from e
            return True


# ---- presentation (Polish)

def log_line(line, codex=""):
    match = LOG_LINE.fullmatch(line.strip())
    if not match:
        return display(line)
    try:
        moment = datetime.datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return display(line)
    stamp = moment.astimezone(POLISH or datetime.timezone.utc).strftime("%d.%m %H:%M:%S")
    who = "" if match[3] == codex else " "+match[3]
    return display(f"{stamp} {match[2]}{who} {match[4]}: {match[5]}")


def thresholds_text(setup):
    t = setup.thresholds()
    return (f"pauza 5 h przy <= {t['WD_LOW']}%, stop tygodniowy przy <= {t['WD_WEEK_LOW']}%; "
            f"koniec po {t['WD_IDLE_MIN']} min bezczynności albo po {t['WD_MAX_HOURS']} h pracy")


def thresholds_note(setup):
    """Warnings for thresholds the guard takes but that rarely make sense."""
    t = setup.thresholds()
    notes = []
    # The guard resumes only when the 5-hour limit is back to WD_RESUME_MIN (20%):
    # a pause threshold at or above it pauses Codex again right after "wznów".
    resume = dict(setup.env).get("WD_RESUME_MIN", "20")
    if resume.isdigit() and int(t["WD_LOW"]) >= int(resume):
        notes.append(f"uwaga: pauza 5 h ({t['WD_LOW']}%) nie jest niżej niż próg wznowienia ({int(resume)}%); "
                     "Codex dostanie pauzę zaraz po wznowieniu")
    if int(t["WD_WEEK_LOW"]) > int(t["WD_LOW"]):
        notes.append(f"uwaga: stop tygodniowy ({t['WD_WEEK_LOW']}%) jest wyżej niż pauza 5 h ({t['WD_LOW']}%); "
                     "zwykle jest niżej")
    return "; ".join(notes)


def version_text(s):
    if s.changed:
        return "nieznana: watchdog.sh podmieniony po starcie"
    return ", ".join(s.versions) if s.versions else "własna (watchdog.sh nie odpowiada żadnej watchdog.v*.sh)"


def limits_text(fields, at, now):
    if not fields:
        return ""
    old = max(0, int((now-at)//60)) if at else 0
    ago = "przed chwilą" if old < 1 else f"sprzed {old} min"
    if fields.get("status") != "ok":
        return f"limity ({ago}): błąd odczytu {fields.get('reason', '')}".rstrip()

    def part(left, reset, label):
        value = fields.get(left, "?")
        moment = fields.get(reset, "")
        return f"{label} {value}%"+(f" (reset {when(int(moment))})" if moment.isdigit() else "")
    allowed = {"true": "tak", "false": "NIE"}.get(fields.get("allowed", ""), "?")
    return f"limity ({ago}): {part('five_left', 'five_reset', '5 h')}, {part('week_left', 'week_reset', 'tydzień')}, zwykłe użycie: {allowed}"


def summary(s):
    """One line for hubi list and the menu."""
    if not s.configured:
        return ""
    if s.unit == "unknown":
        return "stan nieznany (systemd nie odpowiada)"
    head = "działa" if s.running else "nie działa"
    if not s.setup:
        return head+(f"; {s.problem}" if s.problem else "")
    state = s.state.get("STATE", "")
    words = [head, s.setup.cp]
    if state:
        words.append(state)
    if s.resume_at:                                  # before the names: a narrow line keeps it
        words.append("wznowienie "+when(s.resume_at))
    words.append("wersja ?" if s.changed else (s.versions[-1] if s.versions else "wersja własna"))
    words.append(f"{s.setup.codex} → {s.setup.contact}")
    if not s.running:
        words[1] = "ostatnio "+words[1]
    return "  ".join(words)


def report(s, now=None):
    """Every line of `hubi guard status` and of the menu's guard screen."""
    now = now or time.time()
    if not s.configured:
        return ["Strażnik nie jest skonfigurowany (brak watchdog.sh w katalogu strażnika)."]
    lines = []
    if s.unit == "unknown":
        lines.append("Strażnik: stan nieznany (systemd nie odpowiada)")
    elif s.running:
        lines.append("Strażnik: działa"+(f" od {when(s.since)} ({zone()})" if s.since else ""))
    else:
        lines.append("Strażnik: nie działa")
    if s.problem:
        lines.append("  uwaga: "+s.problem)
    if s.setup:
        prefix = "  " if s.source == "unit" else "  ostatnio: "
        lines.append(f"{prefix}CP {s.setup.cp}, cel {s.setup.codex}, kontakt {s.setup.contact}")
        lines.append("  progi: "+thresholds_text(s.setup))
        own = {key for key, _ in THRESHOLDS} | {"PATH"}
        env = " ".join(f"{k}={v}" for k, v in s.setup.env if k not in own)
        options = dict(s.setup.restart)
        lines.append(f"  ustawienia: {env+'; ' if env else ''}restart {options['Restart']} po {options['RestartSec']}")
        state = s.state.get("STATE", "")
        if state:
            other = s.state.get("CP", "")
            note = f" (zapisany dla {other})" if other and other != s.setup.cp else ""
            lines.append(f"  stan: {state}, {STATE_PL.get(state, 'nieznany stan')}{note}")
        else:
            lines.append("  stan: brak pliku stanu (start od zera)")
        if s.resume_at:
            lines.append(f"  wznowienie: {when(s.resume_at)} ({zone()})")
        if state == WEEKLY:
            lines.append(f"  po powrocie limitu: wpisz Codexowi „{s.state.get('CP', s.setup.cp)} wznów”, "
                         f"usuń plik state.{key_of(s.setup.codex)} i włącz strażnika ponownie")
    lines.append("  wersja: "+version_text(s))
    text = limits_text(s.limits, s.limits_at, now)
    if text:
        lines.append("  "+text)
    if s.log:
        lines.append(f"Ostatnie wpisy logu ({zone()}):")
        lines += ["  "+log_line(line, s.setup.codex if s.setup else "") for line in s.log]
    return lines
