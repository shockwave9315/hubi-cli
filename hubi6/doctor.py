import shutil
import sys

from .core.model import HubiError, State
from .config import check_layout


def doctor(app):
    checks = {}
    try:
        snapshot = app.snapshot()
        checks["runtime"] = snapshot.runtime_health
        checks["exit_empty_off"] = snapshot.exit_empty == "0"
        checks["restart_always"] = bool(snapshot.service and snapshot.service.restart == "always")
        checks["systemd_reachable"] = snapshot.systemd_known
        runtime_issues = [i for i in snapshot.issues if not i.startswith(("Invalid repository ", "Cannot read repo root:"))]
        checks["inventory_consistent"] = not runtime_issues and all(w.state != State.UNKNOWN for w in snapshot.workloads)
        checks["diagnostics"] = snapshot.issues
        try:
            app.anchored(snapshot)
            checks["exact_server_ownership"] = True
        except HubiError:
            checks["exact_server_ownership"] = False
    except HubiError as e:
        checks["runtime"] = "UNKNOWN"
        checks["inventory_error"] = str(e)
    checks["python"] = sys.version.split()[0]
    checks["python_supported"] = sys.version_info >= (3, 11)
    for name in ("tmux", "systemctl", "systemd-run", "loginctl", "git", "gh"):
        checks["binary_"+name] = bool(shutil.which(name))
    for kind, argv in app.c.agents.items():
        checks["agent_"+kind] = bool(shutil.which(argv[0]))
    try:
        checks["private_layout"] = check_layout(app.c)
    except (HubiError, OSError) as e:
        checks["private_layout"] = False
        checks["layout_error"] = str(e)
    try:
        checks["linger"] = app.systemd.linger()
        checks["full_cgroup_kill"] = app.systemd.capabilities()
    except HubiError:
        checks["linger"], checks["full_cgroup_kill"] = "unknown", False
    checks["namespace"] = {"service": app.c.service, "socket": str(app.c.socket), "runtime_id": app.c.runtime_id,
                           "records": str(app.c.records)}
    required = ("exit_empty_off", "restart_always", "systemd_reachable", "inventory_consistent", "exact_server_ownership",
                "python_supported", "full_cgroup_kill", "private_layout", "agent_terminal", "binary_loginctl",
                "binary_tmux", "binary_systemctl", "binary_systemd-run", "binary_git")
    runtime_ready = all(checks.get(k) is True for k in required) and checks["linger"] == "yes" and checks["runtime"] == "OK"
    warnings = [name for name in ("agent_codex", "agent_claude", "binary_gh") if not checks.get(name)]
    status = "FAIL" if not runtime_ready else ("WARN" if warnings else "PASS")
    return {"status": status, "ready": status == "PASS", "runtime_ready": runtime_ready,
            "warnings": warnings, "checks": checks}
