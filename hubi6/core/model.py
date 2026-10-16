from dataclasses import asdict, dataclass, field
from enum import Enum
import base64
import hashlib
import json
import re
import time


class HubiError(Exception):
    """Expected, actionable error. Never contains environment values."""


class State(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    ATTACHED = "ATTACHED"
    EXITED = "EXITED"
    ORPHANED = "ORPHANED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class RepoHealth(str, Enum):
    UNKNOWN = "UNKNOWN"
    OK = "OK"
    MISSING = "MISSING"
    REPLACED = "REPLACED"
    INVALID = "INVALID"


KINDS = ("codex", "claude", "terminal")
HEX20 = re.compile(r"[a-f0-9]{20}\Z", re.ASCII)
HEX16 = re.compile(r"[a-f0-9]{16}\Z", re.ASCII)
INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z", re.ASCII)


def validate_instance(value: str) -> str:
    if not INSTANCE.fullmatch(value):
        raise HubiError("Instance must match [A-Za-z0-9][A-Za-z0-9_-]{0,31}")
    return value


def safe_path(value: str) -> str:
    if not value or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise HubiError("Paths containing control characters are unsupported")
    return value


def display(value: object) -> str:
    """No terminal controls, including bidi controls, from external metadata."""
    import unicodedata
    return "".join(c if unicodedata.category(c)[0] != "C" else f"\\u{ord(c):04x}"
                   for c in str(value))


def tmux_format(value: str) -> str:
    return value.replace("#", "##")


def key_id(kind: str, path: str, instance: str) -> str:
    if kind not in KINDS:
        raise HubiError("Unknown workload type")
    validate_instance(instance)
    safe_path(path)
    return hashlib.sha256(json.dumps([kind, path, instance], ensure_ascii=True).encode()).hexdigest()[:20]


def names(runtime: str, kind: str, key: str, generation: str) -> tuple[str, str]:
    if not HEX20.fullmatch(runtime) or not HEX20.fullmatch(key) or not HEX16.fullmatch(generation) or kind not in KINDS:
        raise HubiError("Invalid runtime/workload identity")
    session = f"hubi6-{runtime}-{kind}-{key}-{generation}"
    return session, session + ".scope"


@dataclass(frozen=True)
class RepoRef:
    path: str
    device: int
    inode: int
    git_dir: str
    git_device: int
    git_inode: int
    origin: str = ""
    workspace_id: str = ""

    @property
    def plain(self):
        """A plain directory (the home workspace), not a Git working tree.

        It is identified by device and inode only; the empty Git fields are the
        marker, so journals keep the same schema as for repositories.
        """
        return not self.git_dir


@dataclass
class Record:
    runtime: str
    uid: int
    kind: str
    instance: str
    key: str
    generation: str
    repo: RepoRef
    created: float = field(default_factory=time.time)
    pane: str = ""
    invocation: str = ""
    phase: str = "prepared"
    schema: int = 6

    @property
    def id(self) -> str:
        return f"{self.key}-{self.generation}"

    @property
    def session(self) -> str:
        return names(self.runtime, self.kind, self.key, self.generation)[0]

    @property
    def scope(self) -> str:
        return names(self.runtime, self.kind, self.key, self.generation)[1]

    @property
    def description(self) -> str:
        return f"hubi6:{self.runtime}:{self.id}"

    def data(self) -> dict:
        return asdict(self)

    def encoded(self) -> str:
        return base64.urlsafe_b64encode(json.dumps(self.data(), ensure_ascii=True).encode()).decode()

    @classmethod
    def parse(cls, data: dict, runtime: str, uid: int):
        try:
            data = dict(data)
            repo = dict(data["repo"])
            repo.pop("git_ctime_ns", None)  # obsolete pre-review diagnostic field
            data["repo"] = RepoRef(**repo)
            r = cls(**data)
            if r.runtime != runtime or r.uid != uid or r.schema != 6:
                raise ValueError("Foreign identity")
            names(r.runtime, r.kind, r.key, r.generation)
            if key_id(r.kind, r.repo.path, r.instance) != r.key or not r.repo.path.startswith("/"):
                raise ValueError("Invalid key")
            if r.pane and not re.fullmatch(r"%[0-9]+", r.pane):
                raise ValueError("Invalid pane")
            if r.invocation and not re.fullmatch(r"[a-f0-9]{32}", r.invocation):
                raise ValueError("Invalid invocation")
            if r.phase not in ("prepared", "launching", "running", "stopping"):
                raise ValueError("Invalid phase")
            if not isinstance(r.created, (float, int)) or not 0 < r.created < float("inf"):
                raise ValueError("Invalid creation time")
            for value in (r.repo.device, r.repo.inode, r.repo.git_device, r.repo.git_inode):
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid filesystem identity")
            if not isinstance(r.repo.git_dir, str) or not isinstance(r.repo.origin, str):
                raise ValueError("Invalid workspace fields")
            if r.repo.plain:
                if r.repo.git_device or r.repo.git_inode or r.repo.origin or r.repo.workspace_id:
                    raise ValueError("Invalid directory workspace")
            else:
                if not r.repo.git_dir.startswith("/"):
                    raise ValueError("Invalid Git directory")
                safe_path(r.repo.git_dir)
            if r.repo.workspace_id and not re.fullmatch(r"[0-9a-f]{32}", r.repo.workspace_id):
                raise ValueError("Invalid workspace identity")
            return r
        except (KeyError, TypeError, ValueError, HubiError) as e:
            raise HubiError("Malformed or foreign workload metadata") from e

    @classmethod
    def decode(cls, value: str, runtime: str, uid: int):
        try:
            if len(value) > 65536:
                raise ValueError("Oversized metadata")
            return cls.parse(json.loads(base64.b64decode(value, altchars=b"-_", validate=True)), runtime, uid)
        except (ValueError, TypeError) as e:
            raise HubiError("Malformed workload metadata") from e


@dataclass
class Unit:
    name: str
    active: str = "unknown"
    invocation: str = ""
    cgroup: str = ""
    pid: int = 0
    description: str = ""
    restart: str = ""

    @property
    def live(self):
        return self.active in ("active", "activating", "deactivating", "reloading")


@dataclass
class Session:
    name: str
    session_id: str
    pane: str
    dead: bool
    pid: int
    attached: int
    metadata: str
    cwd: str = ""
    panes: int = 1
    exit_status: int | None = None
    exit_signal: str = ""


def derive_state(session: Session | None, unit: Unit | None, *, known: bool,
                 valid: bool = True, starting: bool = False) -> State:
    if not known or not valid or (unit is not None and unit.active == "unknown"):
        return State.UNKNOWN
    live = unit is not None and unit.live
    if session is None:
        return State.ORPHANED if live else (State.STARTING if starting else State.STOPPED)
    if live:
        if session.dead:
            return State.ORPHANED
        return State.ATTACHED if session.attached else State.RUNNING
    if session.dead:
        return State.EXITED
    return State.STARTING if starting else State.STALE


def phase_transition(phase: str, event: str) -> str:
    rules = {("prepared", "launch"): "launching", ("launching", "ready"): "running",
             ("prepared", "stop"): "stopping", ("launching", "stop"): "stopping",
             ("running", "stop"): "stopping", ("stopping", "stop"): "stopping"}
    try:
        return rules[phase, event]
    except KeyError as e:
        raise HubiError(f"Invalid lifecycle transition: {phase}/{event}") from e


def actions(state: State) -> tuple[str, ...]:
    if state in (State.RUNNING, State.ATTACHED, State.EXITED):
        return ("inspect", "attach", "view", "shared", "takeover", "stop")
    if state in (State.ORPHANED, State.STARTING, State.STOPPED, State.STALE):
        return ("inspect", "stop", "reconcile")
    return ("inspect", "reconcile")


@dataclass
class Workload:
    record: Record
    state: State
    repo_health: RepoHealth
    session: Session | None
    unit: Unit | None
    issues: list[str] = field(default_factory=list)

    def data(self) -> dict:
        r = self.record
        return {"id": r.id, "type": r.kind, "repo": r.repo.path, "workspace": "home" if r.repo.plain else "repo",
                "repo_health": self.repo_health.value,
                "instance": r.instance, "tmux_session": r.session, "scope": r.scope,
                "generation": r.generation, "invocation": r.invocation,
                "state": self.state.value, "attached_clients": self.session.attached if self.session else 0,
                "exit_status": self.session.exit_status if self.session else None,
                "exit_signal": self.session.exit_signal if self.session else "",
                "age_seconds": max(0, time.time() - r.created), "cwd": self.session.cwd if self.session else "",
                "metadata": r.data(), "issues": self.issues, "actions": actions(self.state)}
