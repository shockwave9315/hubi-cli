from dataclasses import dataclass
import os
import time

from .adapters.git import health
from .core.model import HubiError, Record, State, Workload, derive_state


@dataclass
class Snapshot:
    workloads: list
    repos: list
    issues: list
    runtime_health: str
    service: object
    server_pid: int
    exit_empty: str
    tmux_known: bool
    systemd_known: bool
    elapsed_ms: float
    subprocesses: int

    def data(self):
        return {"schema": 6, "runtime_health": self.runtime_health,
                "workloads": [w.data() for w in self.workloads],
                "repos": [vars(r) for r in self.repos], "issues": self.issues,
                "snapshot_ms": self.elapsed_ms, "subprocesses": self.subprocesses}


def build(config, store, tmux, systemd, git):
    start, calls = time.monotonic(), tmux.r.calls
    try:
        sessions, tk, pid, exit_empty, issues = tmux.inventory()
    except HubiError:
        sessions, tk, pid, exit_empty, issues = {}, False, 0, "unknown", ["tmux inventory unavailable"]
    try:
        units, sk = systemd.inventory()
    except HubiError:
        units, sk = {}, False
        issues.append("systemd inventory unavailable")
    records, errors = store.all()
    issues.extend(errors)
    repos, errors = git.discover()
    issues.extend(errors)
    # Recover independent ownership receipts from either runtime adapter. Disk is
    # a journal, never sufficient authorization to signal a reusable scope name.
    for s in sessions.values():
        try:
            r = Record.decode(s.metadata, config.runtime_id, os.getuid())
            if r.session != s.name:
                raise HubiError("Session name/metadata mismatch")
            records.setdefault(r.id, r)
        except HubiError:
            issues.append(f"Inconsistent session {s.name}; reconcile using its receipt")
    for u in units.values():
        if u.name == config.service:
            continue
        try:
            encoded = u.description.split(":", 3)[3]
            r = Record.decode(encoded, config.runtime_id, os.getuid())
            if u.name != r.scope or not systemd.owns_description(r, u.description):
                raise HubiError("Scope identity mismatch")
            if r.id not in records:
                r.invocation = u.invocation
                records[r.id] = r
        except (HubiError, IndexError):
            issues.append(f"Unverified scope {u.name}; no destructive action allowed")
    service = units.get(config.service)
    rh = "UNKNOWN" if not sk else "DOWN"
    if service and service.active == "active" and tk and pid == service.pid:
        try:
            with open(f"/proc/{pid}/cgroup") as f:
                cg = f.read().strip()
            owned = cg == "0::"+service.cgroup and service.cgroup.endswith("/"+config.service)
        except OSError:
            owned = False
        rh = "OK" if owned and exit_empty == "0" and service.restart == "always" else "DEGRADED"
    workloads = []
    for r in records.values():
        s, u = sessions.get(r.session), units.get(r.scope)
        valid, wi = True, []
        if s:
            try:
                sr = Record.decode(s.metadata, config.runtime_id, os.getuid())
                valid = sr.id == r.id and sr.pane == s.pane and (not r.pane or r.pane == s.pane)
            except HubiError:
                valid = False
            if s.panes != 1:
                wi.append("Extra manually created panes are outside the workload scope; remove them before stop")
                valid = False
        if u and u.live and (not systemd.owns_description(r, u.description) or (r.invocation and u.invocation != r.invocation)):
            valid = False
        state = derive_state(s, u, known=sk and (tk or (s is None and u is not None and u.live)), valid=valid,
                             starting=r.phase in ("prepared", "launching") and time.time()-r.created < 10)
        if not valid:
            wi.append("Inconsistent metadata or generation; use reconcile")
        workloads.append(Workload(r, state, health(r.repo), s, u, wi))
    workloads.sort(key=lambda w: (os.fsencode(w.record.repo.path), w.record.kind, w.record.instance, w.record.generation))
    return Snapshot(workloads, repos, issues, rh, service, pid, exit_empty, tk, sk,
                    (time.monotonic()-start)*1000, tmux.r.calls-calls)
