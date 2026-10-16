import os
import hashlib
import re
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
ALLOW_ENV += ("SSH_AUTH_SOCK", "SHELL", "LANG", "LANGUAGE")
LOCALE_ENV = ("LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME", "LC_MONETARY",
              "LC_PAPER", "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE", "LC_MEASUREMENT", "LC_IDENTIFICATION")


class App:
    def __init__(self, config, runner=None):
        self.c = config
        self.r = runner or Runner()
        self.store = Store(config)
        self.tmux, self.systemd, self.git = Tmux(config, self.r), Systemd(config, self.r), Git(config, self.r)

    def snapshot(self, *, include_other=False):
        return build(self.c, self.store, self.tmux, self.systemd, self.git, include_other=include_other)

    def runtime_snapshot(self):
        # Lifecycle never scans repositories or touches unrelated workspaces.
        return build(self.c, self.store, self.tmux, self.systemd, self.git,
                     include_repos=False, include_health=False)

    def anchored(self, snapshot=None):
        s = snapshot or self.runtime_snapshot()
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

    def find(self, identity, snapshot=None, *, health_check=True):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{8,20}(?:-[a-f0-9]{0,16})?", identity):
            raise HubiError("Workload ID requires at least 8 hexadecimal characters; use hubi list")
        s = snapshot or self.runtime_snapshot()
        matches = [w for w in s.workloads if w.record.id == identity or w.record.id.startswith(identity)]
        if len(matches) != 1:
            raise HubiError("Workload ID missing or ambiguous; use hubi list")
        w = matches[0]
        if snapshot is None and health_check:
            w.repo_health = health(w.record.repo)
        return w

    def start(self, kind, repo, instance="primary", resume=False):
        if repo is None and kind != "terminal":
            # Only a plain shell may default to the home workspace; an agent there
            # acts on the whole home directory, so it needs an explicit ~.
            raise HubiError("An agent needs an explicit repository or ~")
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
        ref = pin_workspace(ref)
        workspace_lock = hashlib.sha256(f"{kind}:{instance}:{ref.workspace_id}:{ref.device}:{ref.inode}".encode()).hexdigest()[:20]
        with self.store.lock(workspace_lock), self.store.lock(key):
            s = self.preflight()
            for w in s.workloads:
                old = w.record
                if (old.key != key and old.kind == kind and old.instance == instance and
                    old.repo.workspace_id == ref.workspace_id and
                    (old.repo.device, old.repo.inode) == (ref.device, ref.inode)):
                    raise HubiError(f"Moved workspace already has a workload: {old.id}; stop it before starting here")
            same = [w for w in s.workloads if w.record.key == key]
            for w in same:
                if health(w.record.repo) == RepoHealth.REPLACED:
                    raise HubiError("Previous checkout still has a workload; stop it by ID")
                if w.state in (State.RUNNING, State.ATTACHED):
                    return w.record
                if w.state == State.EXITED:
                    raise HubiError(f"Workload EXITED: {w.record.id}; stop/reconcile it before creating a new generation")
                if w.state == State.ORPHANED:
                    raise HubiError("Orphan requires explicit stop/reconcile by ID")
                if w.record.phase in ("prepared", "launching"):
                    # A respawned worker finishes without the launcher. Give that
                    # generation its bounded startup period before cancelling it.
                    limit = time.monotonic()+max(0, 5-(time.time()-w.record.created))
                    while time.monotonic() < limit:
                        updated = self.find(w.record.id, health_check=False)
                        if updated.state in (State.RUNNING, State.ATTACHED):
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
            # Garbage-collect only journals with proven absence of both objects,
            # under this exact lifecycle lock. Reads merely hide such journals.
            records, _ = self.store.all()
            visible = {w.record.id for w in s.workloads}
            if s.tmux_known and s.systemd_known:
                for old in records.values():
                    if old.key == key and old.id not in visible:
                        self.store.remove(old)
            r = Record(self.c.runtime_id, os.getuid(), kind, instance, key, uuid.uuid4().hex[:16], ref)
            self.store.save(r)
            payload = self.c.runtime/(r.id+".payload")
            # One-use 0600 envelope. Secrets never enter argv, tmux options, logs,
            # systemd environment properties or persistent workload metadata.
            environment = {k: os.environ[k] for k in ALLOW_ENV+LOCALE_ENV if k in os.environ}
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
                    # The worker journals scope ownership just before exec.
                    # Observe that narrow ack/exec race, not later agent health.
                    settle = time.monotonic()+.075
                    while True:
                        if self.find(r.id, health_check=False).state not in (State.RUNNING, State.ATTACHED):
                            raise HubiError(f"Workload failed or EXITED during startup; inspect/stop {r.id}")
                        if time.monotonic() >= settle:
                            break
                        time.sleep(.01)
                    return r
                sessions, known, *_ = self.tmux.inventory()
                if known and sessions.get(r.session) and sessions[r.session].dead:
                    raise HubiError(f"Workload startup failed; inspect {r.id}")
                time.sleep(0.025)
            raise HubiError(f"Start acknowledgement timed out; workload remains inspectable: {r.id}")

    def _stop(self, r, *, expected, repair=False, initial=None):
        if expected != r.generation:
            raise HubiError("Workload generation changed; stale confirmation refused")
        initial = initial or self.runtime_snapshot()
        if not initial.systemd_known:
            raise HubiError("Systemd state UNKNOWN; stop refused")
        if initial.tmux_known:
            self.anchored(initial)
            self.tmux.enforce()
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
            first = ("HUP", self.c.terminal_hup_grace) if r.kind == "terminal" else ("INT", self.c.agent_int_grace)
            term_grace = self.c.terminal_term_grace if r.kind == "terminal" else self.c.agent_term_grace
            for sig, grace in (first, ("TERM", term_grace), ("KILL", self.c.kill_grace)):
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
        selected = self.find(identity, health_check=False)
        r = selected.record
        with self.store.lock(r.key):
            snapshot = self.runtime_snapshot()
            current = self.find(r.id, snapshot, health_check=False)
            if expected and expected != current.record.generation:
                raise HubiError("Workload generation changed; stale confirmation refused")
            if not repair and current.state == State.UNKNOWN:
                raise HubiError("State UNKNOWN; inspect/reconcile before stop")
            if current.session and current.session.panes != 1:
                if not repair:
                    raise HubiError("Extra panes require explicit reconcile before stop")
                self.anchored()
                # Recover immutable primary pane from the live scope receipt if
                # session metadata was damaged. Verify generation before repair.
                unit = self.systemd.unit(r.scope)
                description = unit.description
                receipt = r
                if unit.live:
                    try:
                        receipt = Record.decode(unit.description.split(":", 3)[3], self.c.runtime_id, os.getuid())
                    except (HubiError, IndexError) as e:
                        raise HubiError("Cannot verify scope receipt for pane repair") from e
                    if (receipt.id != r.id or receipt.session != r.session or receipt.scope != unit.name or
                        not self.systemd.owns_description(r, unit.description)):
                        raise HubiError("Scope receipt does not match the exact repair generation")
                # Empty session/journal invocation can be recovered only after
                # decoding the exact scope receipt, then rechecking live identity.
                unit = self.systemd.verify_scope(r, r.invocation or unit.invocation)
                if unit.live and unit.description != description:
                    raise HubiError("Scope receipt changed during pane repair")
                self.tmux.repair_panes(receipt)
                if unit.live:
                    receipt.invocation, receipt.phase = unit.invocation, "running"
                self.store.save(receipt)
                return  # explicit pane repair preserves the verified primary workload
            self._stop(current.record, expected=expected or r.generation, repair=repair, initial=snapshot)

    def reap_clean_exit(self, identity):
        """Remove one generation whose primary process already ended by itself.

        Nothing is signalled: the scope is gone and only the retained dead pane
        and its journal remain. An agent is removed only after status 0; a failed
        agent stays EXITED so its output stays inspectable (contract 19). A
        terminal is removed after any exit status, because `exit` in a shell
        carries the status of the last command and is still the user's own way
        out. A signalled exit (killed, crashed) always stays. Returns the reaped
        workload, or None.
        """
        try:
            w = self.find(identity, health_check=False)
        except HubiError:
            return None
        s = w.session
        if (w.state != State.EXITED or s is None or not s.dead or s.attached or s.panes != 1 or
                s.exit_status is None or s.exit_signal or (w.unit and w.unit.live)):
            return None
        if s.exit_status != 0 and w.record.kind != "terminal":
            return None
        self.stop(w.record.id, expected=w.record.generation)
        return w

    def open(self, kind, repo, instance="primary", resume=False, mode="normal"):
        """Start the workload if needed, attach, and tidy up after a clean exit."""
        record = self.start(kind, repo, instance, resume)
        code = self.attach(record.id, mode)
        self.reap_clean_exit(record.id)
        return code

    def attach(self, identity, mode="normal"):
        if os.environ.get("HUBI6_ACTIVE"):
            raise HubiError("Attach from inside a Hubi workload is refused; detach first")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise HubiError("Attach requires a terminal")
        selected = self.find(identity, health_check=False)
        with self.store.lock(selected.record.key):
            snapshot = self.anchored()
            self.tmux.enforce()
            w = self.find(selected.record.id, snapshot, health_check=False)
            if "attach" not in actions(w.state):
                raise HubiError("Workload cannot be attached in its current state")
            if mode == "normal" and w.session.attached:
                raise HubiError("Workload already has clients; select --mode view/shared/takeover")
        # Never hold a lifecycle lock for the lifetime of a tmux client. Exact
        # generation names make concurrent stop/start safe; tmux -N fails closed.
        return self.tmux.attach(w.record, mode)
