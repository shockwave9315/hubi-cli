# Hubi v5

Hubi is the SSH launcher for the ai-devbox. The menu itself and temporary
shells run outside tmux. Long-lived Codex and Claude agents and persistent
project terminals run beneath the lingering systemd user manager and use a
dedicated, systemd-owned tmux server. SSH, the Hubi launcher, and tmux clients
are only a disposable control plane.

This repository is development-only. Installation targets such as
`~/.local/bin/hubi`, `~/.tmux.conf`, and `~/.bashrc` must only be updated in a
separate, explicitly approved installation step.

## Reliability contract

Hubi v5 supports continued operation after loss of any or all SSH clients,
network connectivity, the Hubi launcher, or tmux client processes while the
Debian container and the user's systemd manager remain alive. This includes
abrupt TCP loss and simultaneous disappearance of every SSH login.

Hubi does not guarantee restoration after a tmux server crash, a user systemd
manager restart or `terminate-user`, or a container/host reboot. Linger must be
enabled for the Hubi user; `KillUserProcesses` is diagnostic information only
and is not a v5 persistence requirement.

## Use

Menu choices are entered as one character followed by Enter. Hubi reads the
whole line, so arrow keys, PageUp/PageDown, escape sequences, and pasted text
are rejected instead of being interpreted byte-by-byte. Bracketed paste is
structurally quarantined through its closing marker even across embedded
newlines. An unterminated bracketed paste is discarded after a five-second
bound instead of blocking the launcher indefinitely.

Raw/unbracketed queued input receives best-effort short-window draining at
menu, shell, and tmux boundaries. Input delayed enough to be indistinguishable
from deliberate typing is intentionally treated as ordinary user input; Hubi
does not claim an absolute quarantine guarantee for terminals that omit
bracketed-paste markers.

```text
hubi
hubi codex REPO [INSTANCE [new|resume]]
hubi claude REPO [INSTANCE [new|resume]]
hubi shell REPO
hubi sessions
hubi doctor
```

`hubi doctor` is a read-only readiness report. It displays the Hubi and tmux
versions, dedicated socket, linger and the live login1 Manager's informational
`KillUserProcesses` state, user-manager reachability, service state, tmux PID
and cgroup ownership,
effective `exit-empty`, full-cgroup kill support, and managed-scope inventory.
It uses tmux's no-start mode and never starts a server, service, session, or
scope and never changes configuration. A failed linger query is treated as
unknown and creation readiness fails closed.

The versioned server templates are [`config/tmux-server.conf`](config/tmux-server.conf)
and [`systemd/user/hubi-tmux.service`](systemd/user/hubi-tmux.service). They are
intended to be installed later as `~/.config/hubi/tmux-server.conf` and
`~/.config/systemd/user/hubi-tmux.service`; repository tests do not install or
enable them. The Hubi config sources `~/.tmux.conf` first and then forces
`exit-empty` off. The service runs `tmux -D` in the foreground on
`$XDG_RUNTIME_DIR/tmux-$UID/hubi`, creates that private socket directory on a
clean start, removes a stale socket after stop, and retains the audited
`Restart=on-failure` policy.

`REPO` must resolve to a Git repository root beneath `~/repos` (or
`$HUBI_REPOS`). Both normal clones and Git worktrees are supported. Repository
and session lists paginate after nine entries. Immediately before tmux and
systemd creation, Hubi revalidates the repository path, root, containment, and
filesystem identity so a vanished or replaced repository cannot fall back to
the home directory.

Agent states are:

- `○ STOPPED` — no tmux session exists.
- `● RUNNING` — the agent is alive with no attached clients.
- `● ATTACHED (N)` — the agent is alive with N attached clients.
- `⚠ EXITED` — the agent ended, but its pane and final output were retained.
- `⚠ ORPHANED` — the systemd scope is alive but its tmux session is missing.

Selecting an `EXITED` agent opens the retained terminal output. Use the
project's stop action to discard that retained session before starting it
again.

Each project has a `primary` Codex instance and a `primary` Claude instance,
with deterministic tmux session and systemd scope names. The project's
Instances menu can create additional names matching
`^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`, list secondary instances from managed tmux
metadata, and also recover secondary `ORPHANED` instances from active systemd
scope names when their tmux session is gone. It can start, attach, inspect, or
stop one instance without affecting its siblings. A stopped secondary instance
can start a new conversation or ask the installed agent CLI to resume one;
Hubi does not store conversation history.

`Shell projektu` remains an ephemeral Bash shell: exiting Hubi or losing its
SSH connection ends that shell. `Terminale persistent` instead creates named
Bash sessions in the repository root. Each managed primary pane starts through
`systemd-run --user --scope --collect` in its own deterministic
`hubi-terminal-REPO_HASH-INSTANCE.scope`, recorded as `@hubi-scope`. A project
can have multiple terminal instances using the same bounded name grammar as
agent instances; they survive tmux detach and SSH disconnect and can be
reattached from another client.

Stopping a persistent terminal sends TERM to its complete scope, waits for a
bounded interval, escalates to KILL for the entire cgroup when necessary,
verifies inactivity, and removes only the exact tmux session if tmux has not
already removed it. This includes descendants that call `setsid` or ignore
TERM. Orphan terminal scopes remain discoverable and can be reconciled without
touching siblings; their `ORPHANED` menu entries remain selectable for an
explicit confirmed stop.

The lifecycle guarantee covers the Hubi-created primary pane and its scope.
Extra windows manually created in the same tmux session receive separate
`tmux-spawn-*` scopes from systemd and are not swept by Hubi v5 core. This is a
documented limitation; Hubi deliberately avoids broad cgroup discovery or
sweeps.

When a live session already has a client, Hubi asks whether to attach in one of
three modes:

- View only: tmux read-only mode; that client's keys cannot reach the agent.
- Share control: writable without disconnecting another client.
- Take over: writable and disconnect all other clients.

Agent windows use tmux's `largest` sizing policy, so a smaller phone viewer does
not shrink a larger laptop window. Hubi pins the exact agent pane in session
metadata and installs a session-local hook so new windows in that managed
session also receive `largest` and `remain-on-exit`; unrelated tmux sessions are
not changed.

## Lifecycle and signals

Every v5 agent starts in a uniquely named `systemd --user` scope. Stopping it
sends Ctrl+C first, waits for a bounded grace period, then signals the complete
scope with TERM and finally KILL if necessary. This cgroup boundary includes
descendants that create new process groups. Codex and Claude use separate tmux
sessions and separate scopes.

Each agent and persistent terminal has one bounded command-mode
`flock --close` lifecycle lock for its exact repository/type/instance identity.
Each lock is owned by a short-lived supervisor and its descriptor is closed
before the worker can create tmux or systemd processes. Sibling identities
remain independent. A busy lock produces a diagnostic after three seconds
instead of freezing the menu. Under the lock, Hubi rechecks the tmux session
and exact deterministic scope independently; an orphan scope can be safely
cleaned and a restart can recover. Destructive confirmation records the
scope's systemd `InvocationID` and refuses the action if that reusable scope
name now refers to a replacement generation. No lifecycle lock is held while
waiting for user input.

Hubi preserves tmux and systemd diagnostics when startup or attachment fails.
A failed/ended pane remains available as `EXITED` rather than disappearing.
V5 is intentionally a clean start: it does not enumerate, route to, migrate,
or stop sessions on the default/v4 socket and has no dual-socket mode.

All production tmux client operations use the one dedicated Hubi socket with
tmux 3.6b's `-N` no-start option. New work is created only after Hubi verifies
that `hubi-tmux.service` is active with a nonzero `MainPID` and absolute
`ControlGroup`, binds the PID reported by the dedicated socket exactly to that
`MainPID`, and requires `/proc/MainPID/cgroup` to equal `ControlGroup`. A second
systemd snapshot rejects a service change during verification. The ownership
check and `-N` are separate defenses: if the service disappears after
verification, the creation command fails instead of auto-spawning an
unanchored tmux server.

The resulting ownership tree is `user@UID.service` (kept alive by linger) →
`app.slice` → `hubi-tmux.service`, agent scopes, and persistent-terminal
scopes. SSH shells contain only the Hubi launcher and tmux clients, so killing
an isolated login/client scope does not kill the server or managed work.

Launcher signal behavior is explicit:

- EOF exits the menu normally without retrying.
- Ctrl+C (`INT`) exits Hubi with status 130 and returns an autologin user to the
  ordinary SSH shell.
- `HUP` exits with 129 and `TERM` exits with 143; neither is converted to
  success.
- `q` returns 98 so autologin leaves the user at a normal SSH prompt.
- `x` returns 99 so autologin disconnects the SSH shell.
- Exiting a temporary shell returns to Hubi.

`HUBI_ACTIVE` prevents nested launchers. If autologin is broken, bypass it with:

```bash
ssh -t HOST 'HUBI_NOAUTO=1 bash -il'
```

## Requirements and tests

Runtime dependencies are Bash, Git, tmux, core Debian utilities, and a running
systemd user manager (`systemd-run --user` / `systemctl --user`). Claude keeps
`--permission-mode bypassPermissions`; Codex receives no added permission flag.
Both programs and their arguments are passed as separate argv elements. Hubi
probes the real systemd user-manager bus and checks full-cgroup kill support
before creating managed work. Creation also requires a positively verified
`Linger=yes`, an active `hubi-tmux.service`, and exact server cgroup ownership.
An unavailable bus, unavailable kill semantics, `Linger=no` or unknown linger
state, inactive service, stale socket, or ownership mismatch fails closed.
Hubi does not enable linger, run `sudo`, start services, or change systemd or
logind configuration. When linger is disabled it prints the remediation command
`loginctl enable-linger USER`, which must be run separately with the appropriate
privileges for the machine.

New conversations invoke `codex` or
`claude --permission-mode bypassPermissions`. Resume invokes `codex resume` or
`claude --permission-mode bypassPermissions --resume`.

Run the isolated test suite with:

```bash
env -u HUBI_AGENT_INSTANCE bash -n hubi bashrc-autologin.sh tests/*.sh tests/lib/*.sh
env -u HUBI_AGENT_INSTANCE shellcheck hubi bashrc-autologin.sh tests/*.sh tests/lib/*.sh
./tests/run.sh
env -u HUBI_NOAUTO python3 tests/adversarial.py
./tests/multi_instance.sh
./tests/persistent_terminal.sh
./tests/v5_doctor.sh
./tests/v5_tmux_server.sh
./tests/v5_ownership.sh
./tests/v5_preflight.sh
./tests/v5_lifetime.sh
```

At this revision the functional harness reports 18 tests and the adversarial
suite contains 31 tests. The focused multi-instance harness reports 14 tests;
the focused persistent-terminal harness reports 22 tests. The v5 doctor,
server, ownership, preflight, and login-scope lifetime harnesses report 9, 7,
12, 6, and 2 tests respectively. That is 121 behavior tests in total; every
harness must be fully green for release review.

The harnesses use unique private tmux sockets, disposable Git repositories and
processes, and exact test-only systemd units/scopes. They never address the
production/default tmux socket, production `hubi-tmux.service`, or existing
Hubi scopes. Tests require a reachable systemd user manager but do not require
root and never change real linger settings.

## Later production installation (do not run during repository development)

This is a clean v5 cutover, not a live migration. First finish all v4 work and
confirm that no v4 session needs to be preserved. From a reviewed v5 checkout,
the proposed later installation sequence is:

```bash
cd "$HOME/repos/hubi-cli"
install -d -m 700 "$HOME/.config/hubi"
install -m 644 config/tmux-server.conf "$HOME/.config/hubi/tmux-server.conf"
install -d -m 755 "$HOME/.config/systemd/user"
install -m 644 systemd/user/hubi-tmux.service "$HOME/.config/systemd/user/hubi-tmux.service"

# Run with the privileges required by this machine's login manager:
loginctl enable-linger "$(id -un)"

systemctl --user daemon-reload
systemctl --user enable --now hubi-tmux.service
./hubi doctor

install -d -m 755 "$HOME/.local/bin"
install -m 755 hubi "$HOME/.local/bin/hubi"
"$HOME/.local/bin/hubi" doctor
```

Do not copy the repository's `tmux.conf` over `~/.tmux.conf`: the Hubi-owned
server config deliberately sources the existing user file and applies its
`exit-empty off` invariant afterward. No `.bashrc` change is required for this
v5 runtime cutover. The default/v4 tmux socket and sessions are not migrated,
stopped, or adopted by these steps.
