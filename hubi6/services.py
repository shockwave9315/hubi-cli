import os
from pathlib import Path
import shutil
import sys
import time
import uuid

from .adapters.git import Git, health, pin_workspace
from .adapters.process import Runner
from .adapters.systemd import Systemd
from .adapters.tmux import Tmux
from .core.model import HubiError, Record, RepoHealth, State, actions, key_id, phase_transition
from .inventory import build
from .storage import Store, atomic_json


ALLOW_ENV = ("PATH", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION",
             "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
             "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS")


class App:
    def __init__(self, config, runner=None):
        self.c = config
        self.r = runner or Runner()
        self.store = Store(config)
        self.tmux, self.systemd, self.git = Tmux(config, self.r), Systemd(config, self.r), Git(config, self.r)

    def snapshot(self):
        return build(self.c, self.store, self.tmux, self.systemd, self.git)

    def anchored(self, snapshot=None):
        s = snapshot or self.snapshot()
        u = s.service
        if not s.tmux_known or not s.systemd_known or not u or u.active != "active" or u.pid <= 0 or s.server_pid != u.pid:
            raise HubiError("Runtime unavailable or unowned; run hubi doctor")
        try:
            with open(f"/proc/{u.pid}/cgroup") as f:
                cg = f.read().strip()
            owner = os.stat(f"/proc/{u.pid}").st_uid
            socket_owner = self.c.socket.stat().st_uid
        except OSError as e:
            raise HubiError("Cannot verify tmux server ownership") from e
        if cg != "0::"+u.cgroup or not u.cgroup.endswith("/"+self.c.service) or owner != os.getuid() or socket_owner != os.getuid():
            raise HubiError("tmux server cgroup/UID mismatch")
        after = self.systemd.unit(self.c.service)
        if (after.active, after.pid, after.cgroup, after.invocation) != (u.active, u.pid, u.cgroup, u.invocation):
            raise HubiError("Runtime generation changed")
        return s

    def preflight(self):
        self.c.prepare()
        if self.systemd.linger() != "yes":
            raise HubiError(f"Creation requires Linger=yes; run loginctl enable-linger {self.c.user}")
        if not self.systemd.capabilities():
            raise HubiError("Full-cgroup kill support unavailable")
        s = self.anchored()
        if s.service.restart != "always":
            raise HubiError("Runtime service must use Restart=always")
        self.tmux.enforce()
        return s

    def find(self, identity, snapshot=None):
        s = snapshot or self.snapshot()
        matches = [w for w in s.workloads if w.record.id == identity or w.record.id.startswith(identity)]
        if len(matches) != 1:
            raise HubiError("Workload ID missing or ambiguous; use hubi list")
        return matches[0]

    def start(self, kind, repo, instance="primary", resume=False):
        ref = self.git.resolve(repo)
        key = key_id(kind, ref.path, instance)
        argv = list(self.c.agents[kind])
        if resume:
            if kind == "terminal":
                raise HubiError("A terminal has no conversation to resume")
            argv += ["resume"] if kind == "codex" else ["--resume"]
        executable = shutil.which(argv[0])
        if not executable:
            raise HubiError(f"Agent executable unavailable: {argv[0]}")
        argv[0] = executable
        with self.store.lock(key):
            s = self.preflight()
            same = [w for w in s.workloads if w.record.key == key]
            for w in same:
                if w.repo_health == RepoHealth.REPLACED:
                    raise HubiError("Previous checkout still has a workload; stop it by ID")
                if w.state in (State.RUNNING, State.ATTACHED, State.EXITED):
                    return w.record
                if w.state == State.ORPHANED:
                    raise HubiError("Orphan requires explicit stop/reconcile by ID")
                if w.record.phase in ("prepared", "launching"):
                    # A respawned worker finishes without the launcher. Give that
                    # generation its bounded startup period before cancelling it.
                    limit = time.monotonic()+max(0, 5-(time.time()-w.record.created))
                    while time.monotonic() < limit:
                        updated = self.find(w.record.id)
                        if updated.state in (State.RUNNING, State.ATTACHED, State.EXITED):
                            return updated.record
                        if updated.unit and updated.unit.live:
                            raise HubiError("Start in progress; retry shortly")
                        if updated.session and updated.session.dead:
                            break
                        time.sleep(0.03)
                    self._stop(w.record, expected=w.record.generation, repair=True)
                elif w.state == State.STOPPED:
                    self.store.remove(w.record)
                else:
                    raise HubiError("Inconsistent workload; use reconcile by ID")
            self.git.validate(ref)  # immediately before creation, under the identity lock
            ref = pin_workspace(ref)
            self.git.validate(ref)
            r = Record(self.c.runtime_id, os.getuid(), kind, instance, key, uuid.uuid4().hex[:16], ref)
            self.store.save(r)
            payload = self.c.runtime/(r.id+".payload")
            # One-use 0600 envelope. Secrets never enter argv, tmux options, logs,
            # systemd environment properties or persistent workload metadata.
            environment = {k: os.environ[k] for k in ALLOW_ENV if k in os.environ}
            atomic_json(payload, {"argv": argv, "env": environment})
            self.tmux.create(r)
            self.store.save(r)
            self.git.validate(ref)
            r.phase = phase_transition(r.phase, "launch")
            self.store.save(r)
            worker = Path(__file__).with_name("worker.py").resolve()
            self.tmux.launch(r, [sys.executable, str(worker), "scope", str(self.store.path(r)), str(payload)])
            end = time.monotonic()+5
            while time.monotonic() < end:
                r = self.store.load(self.store.path(r))
                if r.invocation:
                    self.tmux.metadata(r)
                    return r
                sessions, known, *_ = self.tmux.inventory()
                if known and sessions.get(r.session) and sessions[r.session].dead:
                    raise HubiError(f"Workload startup failed; inspect {r.id}")
                time.sleep(0.025)
            raise HubiError(f"Start acknowledgement timed out; workload remains inspectable: {r.id}")

    def _stop(self, r, *, expected, repair=False):
        if expected != r.generation:
            raise HubiError("Workload generation changed; stale confirmation refused")
        initial = self.snapshot()
        if not initial.systemd_known:
            raise HubiError("Systemd state UNKNOWN; stop refused")
        if initial.tmux_known:
            self.anchored(initial)
        elif initial.service and initial.service.active not in ("inactive", "failed"):
            raise HubiError("tmux ownership UNKNOWN; stop refused")
        atomic_json(self.c.runtime/(r.id+".cancel"), {"generation": r.generation})
        u = self.systemd.unit(r.scope)
        # Worker and systemd-run may still be crossing into the scope. It checks
        # cancellation before and after scope creation, before invoking the agent.
        if r.phase in ("prepared", "launching") and not u.live:
            if initial.tmux_known:
                self.tmux.remove(r, repair=repair)
            end = time.monotonic()+0.3
            while time.monotonic() < end:
                u = self.systemd.unit(r.scope)
                if u.live:
                    break
                time.sleep(0.025)
        invocation = r.invocation
        if u.live and not invocation:
            if not self.systemd.owns_description(r, u.description):
                raise HubiError("Cannot recover scope ownership")
            invocation = u.invocation
        if u.live:
            for sig, grace in (("HUP" if r.kind == "terminal" else "INT", 0.2), ("TERM", 0.4), ("KILL", 2.0)):
                self.systemd.kill(r, invocation, sig)
                end = time.monotonic()+grace
                while time.monotonic() < end:
                    u = self.systemd.verify_scope(r, invocation)
                    if not u.live:
                        break
                    time.sleep(0.025)
                if not u.live:
                    break
            if u.live:
                raise HubiError("Scope did not stop; keeping metadata and session")
        sessions, known, *_ = self.tmux.inventory()
        if not known and initial.tmux_known:
            raise HubiError("Cannot confirm session cleanup; keeping receipt")
        if r.session in sessions:
            if sessions[r.session].panes != 1:
                raise HubiError("Remove manually created extra panes before stopping this session")
            self.tmux.remove(r, repair=repair)
        if self.systemd.unit(r.scope).live:
            raise HubiError("Scope reappeared; preserving receipt")
        self.store.remove(r)

    def stop(self, identity, expected=None, repair=False):
        selected = self.find(identity)
        r = selected.record
        with self.store.lock(r.key):
            current = self.find(r.id)
            if not repair and current.state == State.UNKNOWN:
                raise HubiError("State UNKNOWN; inspect/reconcile before stop")
            if current.session and current.session.panes != 1:
                raise HubiError("Remove manually created extra panes before stop")
            self._stop(current.record, expected=expected or r.generation, repair=repair)

    def attach(self, identity, mode="normal"):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise HubiError("Attach requires a terminal")
        selected = self.find(identity)
        with self.store.lock(selected.record.key):
            self.anchored()
            w = self.find(selected.record.id)
            if "attach" not in actions(w.state):
                raise HubiError("Workload cannot be attached in its current state")
            if mode == "normal" and w.session.attached:
                raise HubiError("Workload already has clients; select --mode view/shared/takeover")
        # Never hold a lifecycle lock for the lifetime of a tmux client. Exact
        # generation names make concurrent stop/start safe; tmux -N fails closed.
        return self.tmux.attach(w.record, mode)
