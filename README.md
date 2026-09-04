# Hubi v4

Hubi is the SSH launcher for the ai-devbox. The menu itself and temporary
shells run outside tmux; only long-lived Codex and Claude agents use tmux.
Detaching or losing SSH therefore leaves agents running.

This repository is development-only. Installation targets such as
`~/.local/bin/hubi`, `~/.tmux.conf`, and `~/.bashrc` must only be updated in a
separate, explicitly approved installation step.

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
```

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
- `⚠ LEGACY/UNMANAGED` — a matching pre-v4 session is available only for an
  explicitly confirmed attach and is never managed by v4 lifecycle actions.

Selecting an `EXITED` agent opens the retained terminal output. Use the
project's stop action to discard that retained session before starting it
again.

Each project has a `primary` Codex instance and a `primary` Claude instance,
which keep the exact v4 tmux session and systemd scope names. The project's
Instances menu can create additional names matching
`^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`, list secondary instances from managed tmux
metadata, and also recover secondary `ORPHANED` instances from active systemd
scope names when their tmux session is gone. It can start, attach, inspect, or
stop one instance without affecting its siblings. A stopped secondary instance
can start a new conversation or ask the installed agent CLI to resume one;
Hubi does not store conversation history.

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

Every v4 agent starts in a uniquely named `systemd --user` scope. Stopping it
sends Ctrl+C first, waits for a bounded grace period, then signals the complete
scope with TERM and finally KILL if necessary. This cgroup boundary includes
descendants that create new process groups. Codex and Claude use separate tmux
sessions and separate scopes.

Startup serialization uses a bounded command-mode `flock --close`: the lock is
owned by a short-lived supervisor and its descriptor is closed before the
worker can create tmux or systemd processes. A busy lock produces a diagnostic
after three seconds instead of freezing the menu. Hubi reconciles the tmux
session and scope independently; an orphan scope can be safely cleaned and a
restart can recover.

Hubi preserves tmux and systemd diagnostics when startup or attachment fails.
A failed/ended pane remains available as `EXITED` rather than disappearing.
Pre-v4 tmux session names are recognized as legacy/unmanaged for migration.
They require an explicit attach confirmation and did not start inside a v4
scope, so Hubi refuses to stop them automatically rather than risk leaving
unidentified descendants behind.

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
checks full-cgroup kill support before creating a managed agent and fails
closed if the local systemd interface cannot provide it.

New conversations invoke `codex` or
`claude --permission-mode bypassPermissions`. Resume invokes `codex resume` or
`claude --permission-mode bypassPermissions --resume`.

Run the isolated test suite with:

```bash
./tests/run.sh
python3 tests/adversarial.py
./tests/multi_instance.sh
```

At this revision the functional harness reports 16 tests and the adversarial
suite contains 31 tests. The focused multi-instance harness reports 12 tests;
all three totals must be fully green for release review.

The harness uses a unique tmux socket, disposable Git repositories, fake agent
processes, and unique systemd scopes. It never attaches to or stops the default
tmux server's Codex/Claude sessions.
