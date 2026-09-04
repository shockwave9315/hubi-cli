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
alphanumerics, `_`, or `-`.

Agent states are:

- `○ STOPPED` — no tmux session exists.
- `● RUNNING` — the agent is alive with no attached clients.
- `● ATTACHED (N)` — the agent is alive with N attached clients.
- `⚠ EXITED` — the agent ended, but its pane and final output were retained.
- `⚠ ORPHANED` — a trusted, verified systemd scope is alive but its tmux
  session is missing.
- `⚠ STALE SESSION` / `⚠ EXITED / STALE SCOPE` — the tmux and systemd halves of
  a managed identity have drifted apart (one alive, the other confirmed gone).
- `⚠ UNKNOWN`, `… STARTING`, `⚠ RUNNING / UNKNOWN SCOPE`,
  `⚠ EXITED / UNKNOWN SCOPE` — the scope's state could not be confirmed (a
  `systemctl --user` query itself failed); Hubi never reports these as
  stopped or as a successful stop (see Identity and ownership below).
- `⚠ CONFLICT` — something exists at the exact name Hubi would use, but does
  not carry verifiable Hubi ownership. Never attached to, never destroyed.
- `⚠ LEGACY/UNMANAGED` — a matching pre-v4 (v3) session is available only for
  an explicitly confirmed attach and is never managed by v4 lifecycle actions.

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

## Identity and ownership

Every managed identity is the tuple `(identity_version, canonical_repository_path, agent, instance)`.
It is encoded unambiguously — each field is written
as `<byte-length>:<bytes>`, so no delimiter concatenation can make two
different tuples (for example a repository key containing `:`) collide — and
hashed to 16 hex characters. tmux session and systemd scope names are that
hash plus a cosmetic slug; the slug is never relied on for uniqueness, and the
identity is rooted in the *canonical* repository path, not a path relative to
`HUBI_REPOS`, so two different roots that happen to contain the same relative
repository name can never alias the same session, scope, lock, or state
record.

A private, atomically-written **trusted state record** — not a predictable
tmux/systemd name, and not a token that only ever lived in tmux's own
(equally guessable) metadata — is what authorizes every destructive lifecycle
action. It lives one file per identity under `state/` inside the Hubi runtime
root described below, is owner-only (`600`), is rejected if it is a symlink
or has the wrong owner/mode, and records: identity version, agent, instance,
canonical path, repository device/inode, the exact session, pane, and scope
names, a per-object random token, and a `STARTING`/`COMMITTED` lifecycle
state. A scope Hubi creates also carries that token in its systemd
`Description`, giving a second, OS-level cross-check. A tmux session or
systemd scope that merely has the right *name* — even with `@hubi-managed=v4`
set, even with a full set of matching-looking options — is never destroyed
without a trusted record whose token matches what is actually live; anything
short of that is reported as `CONFLICT`/`UNVERIFIED` and left alone.

Startup is transactional. The record moves `STARTING → COMMITTED` only once
the tmux session, its metadata, the environment sanitization check, the
window/hook policy, and `respawn-pane` have all succeeded; any earlier
failure rolls the tmux session, any scope it created, and the record back to
absent. The one intentional exception: once `respawn-pane` itself has
succeeded and handed the agent to its scope, the record is committed before
the quick-crash check, so an agent that starts and immediately exits is
preserved as a legitimate `EXITED` object rather than rolled back.

Repository *replacement* — a different Git repository appearing at the same
canonical path — is detected via the recorded device/inode and refused as a
`CONFLICT` rather than silently reused; the old agent is left running
untouched, not attached to and not stopped as if it belonged to the new
repository.

Start, stop, and orphan reconciliation for one identity serialize through the
identical per-identity lock file (keyed by the same structured hash), so a
stop issued while a start is still resolving waits for that resolution
instead of reporting "not running" and then leaving a live agent behind.
Startup runs its actual tmux/systemd work in a `flock --close`-supervised
worker; catching `INT`/`HUP`/`TERM` in the launcher terminates that
supervisor (which releases the lock immediately, independent of whatever it
was blocked on) and rolls back through the trusted record — bounded
cancellation, never an indefinite wait. A `SIGKILL` of the launcher itself
cannot run any of this, by construction; what it can leave behind is a
`STARTING` record for deterministic later reconciliation (see Stale/unknown
objects below), never a `COMMITTED` object with unmet guarantees.

### Old-v4 and v3 compatibility

Sessions created by Hubi **before** this identity model existed ("old-v4")
have no trusted record, no `@hubi-token`, and no `@hubi-instance`. They are
never renamed, killed, or otherwise mutated on discovery. A `primary`
instance only adopts one when every independently-checkable fact lines up:
the exact old deterministic session name, `@hubi-managed=v4`, matching
`@hubi-agent`/`@hubi-repo`/empty `@hubi-instance`, the pinned pane resolving
inside that exact session, the pane's actual working directory matching the
canonical repository being requested, and the stored `@hubi-scope` matching
the old deterministic scope name. Adoption is additive only — it enriches the
session with a token and a canonical-path option, then creates a fresh
trusted record — and never touches a field that was already there. If
ownership cannot be established this way, the session is treated as
legacy/unverified rather than destructively managed.

Pre-v4 (v3) session names remain fully unmanaged. v3 passed the repository
directory name straight to tmux as `<agent>-<repo>` with no character
restriction; tmux itself replaces `:` and `.` with `_` in a session name at
creation time, so legacy detection reconstructs that exact transformation
rather than merely widening a character-class regex. Because that
reconstruction can still collide between differently-named repositories,
legacy attach is only offered after confirming the candidate session's pane
working directory actually matches the canonical repository being requested.
Legacy sessions never receive destructive lifecycle actions.

### Stale/unknown objects

A repository that is deleted, renamed, or moved out from under `HUBI_REPOS`
does not strand its managed agent: `[o] Osierocone/nieznane obiekty` on the
main menu lists every trusted record directly (independent of repository
discovery), diagnoses it (`OK`, `ORPHANED`, `STALE-REPO`, `STARTING`,
`UNKNOWN`, `MALFORMED`, ...), and can stop one by its record — using the same
token-verified session/scope checks as normal stop, never by name alone. If a
record cannot be trusted (malformed, wrong owner/mode, a symlink), it is
surfaced as such rather than silently treated as absent or used to authorize
anything.

Every `systemctl --user` query used for scope state is tri-state: `active`,
confirmed `inactive`, or `error` (the query itself failed to answer). A
stop/terminate never treats "error" as "inactive" — it fails closed, reports
the ambiguity, and preserves whatever tmux/state evidence exists rather than
claiming success.

## Lifecycle and signals

Every v4 agent starts in a uniquely named `systemd --user` scope, created with
a `Description` binding it to its trusted-record token. Stopping it sends
Ctrl+C first, waits for a bounded grace period, then signals the complete
scope with TERM and finally KILL if necessary. This cgroup boundary includes
descendants that create new process groups. Codex and Claude use separate tmux
sessions and separate scopes.

Startup serialization uses a bounded `flock --close`: the lock is owned by a
short-lived supervisor and its descriptor is closed before the worker can
create tmux or systemd processes. A busy lock produces a diagnostic after
three seconds instead of freezing the menu. Hubi reconciles the tmux session
and scope independently; a *verified* orphan scope can be safely cleaned and a
restart can recover.

The startup lock is per identity (repository, agent, and instance), so
simultaneous starters for the same identity converge while different
identities start independently — and stop/orphan-reconciliation for that same
identity serialize through the very same lock file. Status, capture, signals,
orphan cleanup, and stop operations use the selected instance's exact scope
and pinned pane.

Hubi preserves tmux and systemd diagnostics when startup or attachment fails.
A failed/ended pane remains available as `EXITED` rather than disappearing.

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

Locks and trusted state live under a private, validated Hubi runtime root —
`$HUBI_RUNTIME_DIR` if set, else `$XDG_RUNTIME_DIR/hubi-$UID`, else
`/tmp/hubi-$UID` — as `locks/` and `state/` subdirectories. That root and each
subdirectory must be a real directory (never a symlink), owned by the current
user, mode `700`; an existing unsafe object is never repaired in place, only
refused. `HUBI_RUNTIME_DIR` exists primarily so tests (and any sandboxed
invocation) can point Hubi at a fully disposable root without touching a
real one.

Run the isolated test suite with:

```bash
./tests/run.sh
python3 tests/adversarial.py
python3 tests/multi_instance.py
python3 tests/lifecycle_hardening.py
```

At this revision `tests/run.sh` reports 21 checks, the adversarial suite
contains 32 tests, the multi-instance suite contains 11 tests, and
`lifecycle_hardening.py` (identity/ownership/lifecycle regression coverage for
the P1–P3 hardening batch) contains 14 tests; all totals must be fully green
for release review.

Every suite uses a unique tmux socket, a private `HUBI_RUNTIME_DIR`,
disposable Git repositories, fake agent processes, and unique systemd scopes.
None of them attach to, stop, or otherwise touch the default tmux server, the
caller's real lock/state directory, or real Codex/Claude sessions.
