"""Internal pane entrypoint. No shell evaluation and no secret argv."""
import os
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hubi6.config import worker_config
from hubi6.core.model import HubiError, RepoHealth, phase_transition
from hubi6.adapters.git import health
from hubi6.adapters.process import Runner
from hubi6.adapters.systemd import Systemd
from hubi6.storage import Store, private_json


def main():
    mode, record_path, payload_path = sys.argv[1:]
    # The journal's explicit canonical path supplies state location. Validate
    # its owned/private layout and recompute runtime identity independently.
    c, r = worker_config(record_path, payload_path)
    store = Store(c)
    if (c.runtime/(r.id+".cancel")).exists():
        return 0
    if mode == "scope":
        if health(r.repo) != RepoHealth.OK:
            raise HubiError("Repository changed before scope creation")
        for key in ("TMUX", "TMUX_PANE", "HUBI_ACTIVE", "HUBI6_ACTIVE"):
            os.environ.pop(key, None)
        description = r.description+":"+r.encoded()
        os.execvp("systemd-run", ["systemd-run", "--user", "--scope", "--collect", "--quiet",
                  "--expand-environment=no", "--unit="+r.scope, "--description="+description,
                  "--property=KillMode=control-group", "--", sys.executable, str(Path(__file__).resolve()),
                  "exec", record_path, payload_path])
    if mode != "exec":
        raise HubiError("Invalid worker mode")
    unit = Systemd(c, Runner()).unit(r.scope)
    with open("/proc/self/cgroup") as f:
        cg = f.read().strip()
    if not unit.live or not unit.invocation or cg != "0::"+unit.cgroup or not unit.cgroup.endswith("/"+r.scope):
        raise HubiError("Worker scope ownership could not be verified")
    if (c.runtime/(r.id+".cancel")).exists():
        return 0
    payload = private_json(Path(payload_path))
    Path(payload_path).unlink()
    # fchdir pins the validated filesystem object instead of falling back to HOME.
    fd = os.open(r.repo.path, os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
    try:
        if health(r.repo) != RepoHealth.OK:
            raise HubiError("Repository changed before exec")
        s = os.fstat(fd)
        if (s.st_dev, s.st_ino) != (r.repo.device, r.repo.inode):
            raise HubiError("Repository was replaced during startup")
        os.fchdir(fd)
    finally:
        os.close(fd)
    r.invocation, r.phase = unit.invocation, phase_transition(r.phase, "ready")
    store.save(r)
    env = {"HOME": str(c.home), "USER": c.user, "LOGNAME": c.user, "LANG": "C.UTF-8", "TERM": "screen-256color",
           "XDG_RUNTIME_DIR": str(c.runtime_base), "DBUS_SESSION_BUS_ADDRESS": f"unix:path={c.runtime_base}/bus"}
    env.update(payload["env"])
    # Markers describe the workload, not an inherited launcher. RC files can
    # use HUBI_NOAUTO to suppress v5 autologin; Hubi refuses self-nesting.
    env["HUBI6_ACTIVE"] = "1"
    if r.kind == "terminal":
        env["HUBI_NOAUTO"] = "1"
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    os.execvpe(payload["argv"][0], payload["argv"], env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HubiError, OSError, ValueError) as exc:
        # Environment and agent arguments are intentionally never printed.
        print(f"Hubi v6 worker: {type(exc).__name__}: start odrzucony", file=sys.stderr)
        raise SystemExit(1)
