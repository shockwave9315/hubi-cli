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

Each managed agent is identified by repository, agent, and instance name. The
project screen lists Claude and Codex instances separately; its instance menu
can create, attach, inspect, or stop one exact instance. Names are 1–32
characters, start with an alphanumeric character, and otherwise contain only
alphanumerics, `_`, or `-`. `primary` retains the original v4 tmux name, scope,
startup lock, and metadata compatibility. Existing v4 sessions without
`@hubi-instance` are recognized as `primary` without being renamed or mutated.
Secondary instances use names and scopes containing the agent, validated
instance, repository hash, and a hash of the complete identity.

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

New instances can start a new conversation or enter the agent's own supported
resume picker. Claude is invoked with `--resume`; Codex is invoked with its
`resume` subcommand. Hubi does not select models or interpret conversation IDs.

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

The startup lock is per repository, agent, and instance, so simultaneous
starters for the same identity converge while different instances may start
independently. Status, capture, signals, orphan cleanup, and stop operations use
the selected instance's exact scope and pinned pane.

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

Project and host shells remain temporary and outside the managed lifecycle.
They display a warning that processes started there may end when SSH
disconnects and direct users to managed Claude/Codex instances for persistence.

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

Run the isolated test suite with:

```bash
./tests/run.sh
python3 tests/adversarial.py
python3 tests/multi_instance.py
```

At this revision the functional harness reports 16 tests, the adversarial
suite contains 32 tests, and the multi-instance suite contains 8 tests; all
totals must be fully green for release review.

The harness uses a unique tmux socket, disposable Git repositories, fake agent
processes, and unique systemd scopes. It never attaches to or stops the default
tmux server's Codex/Claude sessions.
