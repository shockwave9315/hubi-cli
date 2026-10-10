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
    timeout: float = 10
    lock_timeout: float = 3

    @classmethod
    def load(cls, path: str | None = None):
        home = Path.home().resolve()
        config_base = Path(os.environ.get("XDG_CONFIG_HOME", home/".config"))/"hubi6"
        source = Path(path) if path else config_base/"config.toml"
        data = {}
        if source.exists():
            try:
                data = tomllib.loads(source.read_text())
            except (OSError, ValueError) as e:
                raise HubiError(f"Invalid config: {source}: {e}") from e
        uid = os.getuid()
        runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"))
        if runtime_base != Path(f"/run/user/{uid}"):
            raise HubiError("XDG_RUNTIME_DIR must identify this UID's /run/user directory")
        repos = Path(data.get("repos_root", str(home/"repos"))).expanduser().resolve()
        safe_path(str(repos))
        state = Path(os.environ.get("XDG_STATE_HOME", home/".local/state"))/"hubi6"
        cache = Path(os.environ.get("XDG_CACHE_HOME", home/".cache"))/"hubi6"
        socket = runtime_base/"hubi6/tmux.sock"
        service = "hubi6-tmux.service"
        # Reconfiguring repo root must not hide existing workloads. The runtime
        # is its UID, socket, service and journal; workspace paths belong to keys.
        rid = hashlib.sha256(json.dumps([uid, str(home), str(socket), service, str(state)]).encode()).hexdigest()[:20]
        defaults = {"codex": ["codex"], "claude": ["claude", "--permission-mode", "bypassPermissions"],
                    "terminal": ["/bin/bash", "--noprofile", "--norc", "-i"]}
        for name, spec in data.get("agents", {}).items():
            if name not in defaults or not isinstance(spec, list) or not spec or any(not isinstance(x, str) or "\0" in x for x in spec):
                raise HubiError("agents must contain nonempty argv arrays for codex, claude or terminal")
            defaults[name] = spec
        github = data.get("github", {})
        ttl = github.get("cache_ttl", 600)
        owner = github.get("owner", "")
        if not isinstance(ttl, int) or ttl < 0 or not isinstance(owner, str):
            raise HubiError("Invalid GitHub configuration")
        return cls(home, repos, runtime_base, state, cache, config_base, socket, service, rid,
                   defaults, owner, ttl)

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
