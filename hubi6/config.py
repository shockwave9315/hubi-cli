from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import tomllib

from .core.model import HubiError, safe_path


def private_dir(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as e:
        raise HubiError(f"Private runtime directory unavailable: {path}") from e
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HubiError(f"Require an owned directory with mode 0700: {path}")
    return path


@dataclass(frozen=True)
class Config:
    home: Path
    repos: Path
    runtime_base: Path
    state_base: Path
    cache_base: Path
    config_base: Path
    socket: Path
    service: str
    runtime_id: str
    agents: dict[str, list[str]]
    github_owner: str = ""
    github_ttl: int = 600
    lock_timeout: float = 3
    agent_int_grace: float = 3
    agent_term_grace: float = 2
    terminal_hup_grace: float = 1
    terminal_term_grace: float = 1
    kill_grace: float = 2
    clone_timeout: float = 300

    @classmethod
    def load(cls, path: str | None = None):
        # Account home is shared with systemd's %h and worker reconstruction;
        # a stale launcher/server HOME must not create an impossible identity.
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        config_base = xdg_path("XDG_CONFIG_HOME", home/".config")/"hubi6"
        source = Path(path) if path else config_base/"config.toml"
        data = {}
        if path and not source.is_file():
            raise HubiError(f"Config file does not exist: {source}")
        if source.exists():
            try:
                data = tomllib.loads(source.read_text())
            except (OSError, ValueError) as e:
                raise HubiError(f"Invalid config: {source}: {e}") from e
        if not isinstance(data, dict) or set(data)-{"repos_root", "agents", "github", "stop", "lock_timeout"}:
            raise HubiError("Unknown configuration keys")
        if "repos_root" in data and not isinstance(data["repos_root"], str):
            raise HubiError("repos_root must be a path string")
        for section in ("agents", "github", "stop"):
            if not isinstance(data.get(section, {}), dict):
                raise HubiError(f"{section} must be a TOML table")
        uid = os.getuid()
        runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"))
        if runtime_base != Path(f"/run/user/{uid}"):
            raise HubiError("XDG_RUNTIME_DIR must identify this UID's /run/user directory")
        repos = Path(data.get("repos_root", str(home/"repos"))).expanduser().resolve()
        safe_path(str(repos))
        state = xdg_path("XDG_STATE_HOME", home/".local/state")/"hubi6"
        cache = xdg_path("XDG_CACHE_HOME", home/".cache")/"hubi6"
        socket = runtime_base/"hubi6/tmux.sock"
        service = "hubi6-tmux.service"
        # Reconfiguring repo root must not hide existing workloads. The runtime
        # is its UID, socket, service and journal; workspace paths belong to keys.
        rid = runtime_identity(home, socket, service, state)
        defaults = {"codex": ["codex"], "claude": ["claude", "--permission-mode", "bypassPermissions"],
                    "terminal": [interactive_shell(), "-i"]}
        for name, spec in data.get("agents", {}).items():
            if name not in defaults or not isinstance(spec, list) or not spec or not spec[0] or any(not isinstance(x, str) or "\0" in x for x in spec):
                raise HubiError("agents must contain nonempty argv arrays for codex, claude or terminal")
            defaults[name] = spec
        github = data.get("github", {})
        ttl = github.get("cache_ttl", 600)
        owner = github.get("owner", "")
        if set(github)-{"cache_ttl", "owner", "clone_timeout"}:
            raise HubiError("Unknown github configuration keys")
        if type(ttl) is not int or ttl < 0 or not isinstance(owner, str):
            raise HubiError("Invalid GitHub configuration")
        stop = data.get("stop", {})
        grace_defaults = {"agent_int_grace": 3, "agent_term_grace": 2, "terminal_hup_grace": 1,
                          "terminal_term_grace": 1, "kill_grace": 2}
        if set(stop)-set(grace_defaults):
            raise HubiError("Unknown stop configuration keys")
        values = {name: positive_number(stop.get(name, default), "stop."+name)
                  for name, default in grace_defaults.items()}
        return cls(home, repos, runtime_base, state, cache, config_base, socket, service, rid,
                   defaults, owner, ttl, lock_timeout=positive_number(data.get("lock_timeout", 3), "lock_timeout"),
                   clone_timeout=positive_number(github.get("clone_timeout", 300), "github.clone_timeout"), **values)

    @property
    def runtime(self):
        return self.runtime_base/"hubi6"/self.runtime_id

    @property
    def records(self):
        return self.state_base/"workloads"/self.runtime_id

    def prepare(self):
        private_dir(self.runtime_base)
        for p in (self.runtime_base/"hubi6", self.runtime, self.runtime/"locks",
                  self.state_base, self.state_base/"workloads", self.records):
            private_dir(p, create=True)

    @property
    def user(self):
        return pwd.getpwuid(os.getuid()).pw_name


def positive_number(value, name):
    import math
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
        raise HubiError(f"{name} must be a positive number up to 86400 seconds")
    return value


def xdg_path(name, default):
    path = Path(os.environ.get(name, default)).expanduser()
    if not path.is_absolute():
        raise HubiError(f"{name} must be an absolute path")
    try:
        return path.resolve()
    except (OSError, RuntimeError) as e:
        raise HubiError(f"Invalid {name} path") from e


def runtime_identity(home, socket, service, state):
    return hashlib.sha256(json.dumps([os.getuid(), str(home), str(socket), service, str(state)]).encode()).hexdigest()[:20]


def interactive_shell():
    import shutil
    for candidate in (os.environ.get("SHELL", ""), pwd.getpwuid(os.getuid()).pw_shell, "/bin/bash"):
        if candidate.startswith("/") and shutil.which(candidate) and Path(candidate).is_file():
            return candidate
    raise HubiError("No usable interactive shell")


def worker_config(record_path, payload_path):
    """Reconstruct layout from the explicit private receipt path, never tmux env."""
    from .storage import private_json
    from .core.model import Record
    path = Path(record_path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise HubiError("Worker journal must use a canonical absolute path")
    records, workloads, state = path.parent, path.parent.parent, path.parent.parent.parent
    if workloads.name != "workloads" or state.name != "hubi6":
        raise HubiError("Worker journal layout invalid")
    for directory in (state, workloads, records):
        private_dir(directory)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    runtime_base = Path(f"/run/user/{os.getuid()}")
    socket, service = runtime_base/"hubi6/tmux.sock", "hubi6-tmux.service"
    rid = runtime_identity(home, socket, service, state)
    c = Config(home, Path("/"), runtime_base, state, home/".cache/hubi6", home/".config/hubi6",
               socket, service, rid, {})
    r = Record.parse(private_json(path), rid, os.getuid())
    if records != c.records or path != c.records/(r.id+".json") or Path(payload_path) != c.runtime/(r.id+".payload"):
        raise HubiError("Worker paths do not match runtime identity")
    for directory in (runtime_base, runtime_base/"hubi6", c.runtime):
        private_dir(directory)
    return c, r


def check_layout(config):
    """Read-only creation feasibility: validate existing private dirs/ancestors."""
    for path in (config.runtime_base, config.runtime_base/"hubi6", config.runtime, config.runtime/"locks",
                 config.state_base, config.state_base/"workloads", config.records):
        if path.exists() or path.is_symlink():
            private_dir(path)
        else:
            ancestor = path.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
                raise HubiError(f"Cannot create private directory: {path}")
    return True
