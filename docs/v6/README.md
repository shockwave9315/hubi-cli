# Hubi v6 — technical reference

Python 3.11+, standard library only. V6 is the package `hubi6` with its own
runtime; installed, it is the `hubi` command. Overview, installation and everyday
use are in the [top-level README](../../README.md). The executable `../../hubi`
is v5, kept unchanged as the rollback source. From a checkout, v6 runs as
`python3 -m hubi6`:

```console
python3 -m hubi6 --version
python3 -m hubi6 doctor --json
python3 -m hubi6 list --json
python3 -m hubi6 start codex my-repo --instance primary
python3 -m hubi6 start claude my-repo --resume
python3 -m hubi6 start terminal my-repo --instance shell2
python3 -m hubi6 claude my-repo              # start if needed, then attach
python3 -m hubi6 codex my-repo cp5 --resume  # named instance, resume picker
python3 -m hubi6 terminal my-repo
python3 -m hubi6 inspect WORKLOAD_ID
python3 -m hubi6 attach WORKLOAD_ID --mode shared
python3 -m hubi6 attach WORKLOAD_ID --mode view
python3 -m hubi6 attach WORKLOAD_ID --mode takeover
python3 -m hubi6 stop WORKLOAD_ID --generation GENERATION
python3 -m hubi6 reconcile WORKLOAD_ID --generation GENERATION
python3 -m hubi6 github list --json
python3 -m hubi6 github list --offline
python3 -m hubi6 github clone owner/repository
```

`stop` explicitly authorizes stopping the specified generation. With extra panes,
`reconcile` repairs the exact session while preserving its primary workload;
otherwise it explicitly stops the inconsistent generation.
Reconcile verifies the independent ownership receipts and removes inconsistent
session metadata; it does not sweep unverified objects. An unambiguous ID prefix
of at least 8 hexadecimal characters is accepted; empty/short IDs are refused. Stop never requires the repository to exist. `--generation` protects
a confirmation made earlier; IDs themselves also include an immutable generation.

## Menu

`hubi` without a command opens the Polish dashboard (`hubi6/menu.py`). Every
frame is rendered by pure functions from one complete snapshot and sent in one
write; afterwards only lines that changed are redrawn, on the alternate screen.
The list refreshes every `[ui] refresh` seconds (2 by default, 10 after a minute
without a key press). No line is ever wider than the terminal: columns are
clipped with an ellipsis, so nothing wraps at 65 columns or at 39.

One key is one action, without Enter:

| Key | Main screen |
| --- | --- |
| `1`-`9` | attach to that session |
| `l` `c` `t` | new Claude / Codex / terminal session, then the repository number: started and attached |
| `d` | stop: session number, then `q` confirms |
| `i` | session details: view, share, take over, stop, repair |
| `r` | repository view with `git status` |
| `h` | home workspace view (sessions outside any repository) |
| `g` | GitHub catalogue and clone |
| `?` | help |
| `q` / `x` | shell (98) / end the SSH session (99, only offered under autologin) |

`l`, `c` and `t` always start a new session; a running one is entered with
its number. Without a typed name the session takes an automatic one
(`menu.free_name`): the default instance if that type has no session in the
repository yet, otherwise the first free number (`codex/2`, `codex/3`). Every
recorded session counts as taken whatever its state, so an automatic start never
enters, reuses or replaces another session and never asks a question. This is
what makes resume dependable: with the agent already running there, choosing
resume still opens the conversation picker, in a new session.

While choosing a repository, `r` switches between a new conversation and resume.
The switch is drawn as two labelled sides with the active one highlighted, the
screen title changes between `NOWY` and `WZNÓW`, and the toggle is confirmed on
the status line. `n` asks for a session name (`Nazwa sesji [automatyczna]:`);
only a typed name can address an existing session (a running one is entered, a
finished one is replaced after `q`). The session name is the workload's
instance. Its stored, stable name stays ASCII and the default stays `primary` in
records, JSON and the command line; people see the default as `główna`, and
typing `główna` selects it. The command line keeps "start if needed, then
attach" for `hubi claude|codex|terminal REPO [NAZWA]`.

`0`, Esc and Backspace go back; arrows or `j`/`k`
move a cursor and Enter activates it; Space turns pages. Keys are not case
sensitive, and an unknown key is answered with a hint instead of silence.

Each row shows the type icon, `type/name`, repository, activity, attached
clients and age. Activity comes from the pane title the agent sets itself: a
leading spinner glyph means "pracuje" and the rest of the title is shown
underneath. Codex animates a braille spinner; Claude Code shows `◐` while it
works and `✳` while it waits, so only glyphs from spinner sets count
(`menu.working`, recorded from Claude Code 2.1.296 and Codex 0.162.1).
Titles are fetched by a separate, lenient tmux query (`Tmux.titles`) and never
reach the inventory parser, ownership checks or state derivation. Sessions are
numbered in creation order, so starting something never renumbers the others.

Input safety is the same contract as the line menu, adapted to single keys:

- A read that is not exactly one key (a paste, or several keys delivered
  together) is discarded as a whole, reported on screen and followed by 80 ms of
  required silence. Bracketed paste is swallowed up to its closing marker.
- Separate key presses are not throttled, so quick consecutive keys all count.
- Before a stop/remove/repair question is displayed, the terminal must be silent
  for 80 ms and is flushed; the answer is one deliberate `q`. The same drain
  happens before every attach, so nothing typed earlier can reach an agent.
- Cloning still asks for a typed `tak` + Enter.

Long operations (start, stop, GitHub refresh, clone) show a spinner on the
status line. The operation stays on the main thread, so signals interrupt it
exactly as before; a helper thread only draws.

When the primary process of a session ends, a `pane-died` hook detaches every
client, which returns to the menu instead of staying on "Pane is dead". The dead
pane and its exit status are still retained. A session that ended by itself
(no signal, no clients, one pane, scope gone) is then removed automatically by
`App.reap_clean_exit`, after attach in the menu (also for sessions reached with
`Ctrl-b ( ) L`) and in `hubi claude|codex|terminal`; nothing is signalled, because
the scope is already gone, and the stop still carries the exact generation.

The EXITED rule depends on the type. An agent (Claude, Codex) is removed only
after status 0; a failed agent stays EXITED (contract 19) and opens its detail
screen, where `v` shows the output and `d` removes it. A terminal is removed
after `exit` with any status, since a shell's `exit` carries the status of its
last command and is still the user's way out; the menu then says the code
(`terminal/główna: koniec pracy (kod 1), posprzątane`). A terminal killed by a
signal stays EXITED as `przerwany`, and one that died during start (start
reports the failure) stays inspectable as before.

Codex is started with `--no-daemon` by default. Without it, Codex 0.162 starts a
per-user background server (`codex app-server --managed-daemon`) from the first
Codex session, which leaves it inside that workload's scope, and every other
Codex session runs its tools there. Measured on the owner's host: a `sleep`
requested in a second session ran in the first session's cgroup; after `/exit`
the first session stayed ORPHANED because its scope never emptied; stopping it
ended the second session's command and left that session at "Server connection
could not be restored". With `--no-daemon` each session's processes stay in its
own scope, so stop, clean-exit removal and the full-cgroup kill mean what they
say. `[agents] codex = ["codex"]` restores the shared server for a Codex too old
to know the flag.

Ctrl-Z cannot suspend the menu: under SSH autologin the parent is a login shell
still reading its startup files, which waits without job control, so a stopped
menu could not be resumed from the keyboard. The menu disables the terminal's
suspend character and installs a no-op SIGTSTP handler; the tmux server unbinds
`suspend-client` for the same reason.

The reviewed line-oriented menu (`hubi6/tui.py`, English) is unchanged and
remains as a fallback: `hubi --classic` or `HUBI6_UI=classic`. It is also
started automatically if the dashboard raises an unexpected exception, because
Hubi is the SSH login entry point. Human-readable CLI output and error messages
are Polish through `hubi6/i18n.py`; raised messages, logs and JSON keep their
stable English identifiers, and a test fails when a new message has no Polish
wording.

### Home workspace

Work that belongs to no repository (a persistent shell for general work on the
machine, or an agent doing machine administration) runs in the home workspace,
`home_workspace` in the configuration (default `~`, the account's passwd home;
`""` disables it). It must lie outside `repos_root`, so that no path is both a
repository and the plain home workspace: an explicit setting inside it is a
configuration error, while the default simply switches off (for `repos_root =
"~"`). In the dashboard `h` opens it, and `l`/`c`/`t` followed by
`h` starts a session there and attaches. The command line accepts `~` (or the
directory itself) wherever it takes a repository. Only `hubi terminal` without a
repository means the home workspace: an agent there acts on the whole home
directory (Claude runs with `bypassPermissions` by default), so `hubi claude` and
`hubi codex` require an explicit `~`, an empty repository argument is an error,
and `App.start` refuses an agent without a repository. The line menu
(`--classic`) has the same `h` entry, shown only when the home workspace is usable.

It is a plain directory, never treated as a Git working tree even when it holds
a dotfiles repository. Its identity is the directory's device and inode; Hubi
writes nothing into it (no workspace UUID) and runs no Git subprocess for it, so
there is no `git status` view. A workload record keeps the usual schema: empty
Git fields mark the plain workspace, and JSON adds `"workspace": "home"` (or
`"repo"`). Health still applies: a removed or replaced directory shows as
MISSING or REPLACED and the session stays stoppable. Only the configured
directory is accepted for new sessions; sessions created before a change of
`home_workspace` remain visible and stoppable. Agents started there act on the
whole home directory with their usual permissions.

## Configuration and deployment materials

Configuration: `$XDG_CONFIG_HOME/hubi6/config.toml` (default
`~/.config/hubi6/config.toml`). See [example](../../config/hubi6.example.toml).
`--config FILE` selects another TOML file. Agent entries are argv arrays, not
shell commands. Defaults retain the v5 Claude flags; Codex adds `--no-daemon`
(see Menu). Terminal defaults
to the valid $SHELL/passwd interactive shell with normal rc files. Workloads
get HUBI6_ACTIVE=1; terminals also get HUBI_NOAUTO=1 to suppress v5 autologin.
`[ui]` accepts `refresh` (seconds), `color` and `icons` (false = ASCII only);
`NO_COLOR` and `TERM=dumb` also disable colour.
Self-nesting and attach from a Hubi workload are refused. SSH_AUTH_SOCK, SHELL,
LANG/LANGUAGE and standard LC_* join the explicit API/proxy/SSL allowlist.
TERM identifies the virtual tmux terminal (screen-256color), independent of a
launcher running under systemd with TERM=dumb.

For a **later, separately authorized clean installation**, the directory bundle
is produced with `make build` at `dist/hubi6`. Its `hubi` wrapper can run from
that directory; the pane worker uses real Python module paths. The bundle
contains the v6 [service](../../systemd/user/hubi6-tmux.service) and
[tmux config](../../config/hubi6-tmux.conf). The service expects the latter at
`~/.config/hubi6/tmux.conf`. Enable linger and the v6 service only as part of
that later deployment. Development does not replace `~/.local/bin/hubi`.
The bundle includes install.py; see the [install/uninstall, clean cutover and
rollback runbook](CUTOVER.md). Fresh builds remove stale files and record dirty
state plus source hashes; production installation rejects dirty bundles.

SSH autologin: append [config/hubi6-autologin.sh](../../config/hubi6-autologin.sh)
to the **end of `~/.profile`**, not to `~/.bashrc`. A typical `~/.profile`
sources `~/.bashrc` first and extends PATH afterwards; a menu started from
`~/.bashrc` gives every workload the PATH from before those additions (for
example the system Node instead of the user's). The block sets
`HUBI_AUTOLOGIN=1`, which is how the menu knows that `x` ends the SSH session.

Runtime socket: `/run/user/UID/hubi6/tmux.sock`. Locks and one-use startup
envelopes: `/run/user/UID/hubi6/RUNTIME_ID`. Journal:
`$XDG_STATE_HOME/hubi6/workloads/RUNTIME_ID`. Log: `$XDG_STATE_HOME/hubi6/hubi6.log`
(1 MiB, three rotations). GitHub cache: `$XDG_CACHE_HOME/hubi6`. Every owned
directory is private. There is no fallback to `/tmp` for runtime or locks.
A different `XDG_STATE_HOME` creates a different Hubi runtime namespace.
Workloads in one namespace do not appear in another namespace's inventory.
Doctor reports `other_runtime_objects: N` for verified foreign-namespace sessions
and live scopes (each object counted once) on the same v6 server/user manager.
This is informational, does not change PASS, and never offers adoption or stop.

Each working tree receives a random identity in its Git administrative
directory, `hubi6-workspace-id`. This does not change tracked files or the index.
Together with canonical path, filesystem device/inode and Git-dir identity it
detects checkout replacement, including inode reuse. Git status runs with
`GIT_OPTIONAL_LOCKS=0`.

## Runtime and ownership

```text
user@UID.service (linger)
  app.slice
    hubi6-tmux.service (tmux -D, Restart=always)
    hubi6-RUNTIME-TYPE-KEY-GENERATION.scope  # one workload
    hubi6-RUNTIME-clone-GENERATION.service # temporary, bounded clone
SSH/session/client cgroups contain the disposable control layer.
```

The runtime identity binds UID, HOME, socket, service and journal directory.
Changing repo root does not hide existing workloads. Logical workload keys bind
type, canonical absolute workspace path and ASCII instance; generation names
are never reused. Two runtimes and v5/v6 cannot claim each other's scopes.

One snapshot uses one tmux client invocation and one batched `systemctl show`.
All repository discovery and state derivation then runs in Python. Lifecycle
uses a separate runtime snapshot without repository discovery or unrelated repo
health reads; find/inspect probes only the selected workspace. Stale journals
with proven absence of both runtime objects are hidden without writes, and safely
collected under the same-key lifecycle lock on the next start. No Git
subprocess per row. No cached runtime inventory. Fresh stable service generation,
MainPID/socket PID/cgroup/UID ownership checks guard mutations. Tmux clients
always use `-N` and the explicit socket. Scope signals require fresh systemd
state, exact unit, its InvocationID, description and cgroup. No PID-based workload
signals, unit globs for mutations, implicit orphan killing or global cleanup.

The private journal records intent before tmux creation. Tmux session metadata
and systemd description contain independent receipts. Interrupted starts can
recover on the next start. Startup API environment and argv use a one-use 0600
envelope in the private runtime directory, removed before agent exec. Secrets
do not enter helper argv, tmux options, systemd properties or logs. Only the
allowlist in `services.py` reaches the workload; `TMUX`, `TMUX_PANE` and launcher
markers are removed. The session gets the umask of the process that launched
it, not the service's `UMask=0077`: `App.start` reads it from
`/proc/self/status` (`os.umask` would have to change it, and the menu runs a
spinner thread), the envelope freezes it with the environment, and the worker
validates it and applies it just before exec, after its own journal writes. An
absent or invalid value refuses the start. The tmux server and Hubi's own state
keep 0077 and explicit 0600/0700 modes. A normal stop sends INT (agent) or HUP (terminal), then TERM
and KILL to the complete verified cgroup and removes the exact session.

The service restarts even after a clean tmux exit. Its configuration and every
creation preflight force `exit-empty off`; an option hook also guards ordinary
changes. Sourcing a hostile-to-lifetime user config cannot leave the runtime
permanently down. This is protection from accidental self-destruction, not a
sandbox against deliberate socket targeting or `systemd-run` by an agent.

Inside a session the status bar names it (`type/name · repository`) in a
colour per type; the generation hash is no longer the only label. `Ctrl-b d`
returns to the menu, `Ctrl-b )` / `(` / `L` move between sessions without
leaving tmux, and `Ctrl-b s` lists them with their labels. Windows follow the
most recently used client (`window-size latest`), so a small terminal is not
left with a cropped slice of a window sized for a larger idle client.

Manually adding panes/windows through tmux command mode is outside the
single-primary-pane workload contract, as in v5. V6 disables their default
bindings (reasserted in preflight), detects extras as UNKNOWN and offers an
explicit reconcile to remove extra panes from the verified session while keeping
the primary workload. Inspect remains available; normal stop works after repair.
It never adopts outside-scope processes into the workload cgroup.

## GitHub

Discovery uses `gh repo list --json` with complete pagination. For the default
account it also merges paginated `gh api user/repos` results to include
collaborator and organization repositories. An explicit `[github].owner`
limits discovery to that owner. Private/fork/archived flags remain visible.
LOCAL matches normalized GitHub origins across HTTPS, SCP and SSH URL spellings,
independent of directory names; worktrees use common Git configuration.

Hubi does not obtain or persist a GitHub token. HTTPS clones use existing `gh`
authentication via `gh auth git-credential`. The 10-minute JSON cache is private;
offline/auth/network failures can display the previous catalogue with a stale
marker. The TUI displays cache first; refresh is explicit (`r`). Network refresh
is synchronous, bounded at 30 seconds, and never part of a runtime render.

Clone asks for confirmation in the TUI, displays Cloning/result, and preserves
bounded gh diagnostics after redacting credentials and known secrets. It runs
in its own transient service with a configurable 300-second default bound and full-cgroup
cleanup, stages below repo root, validates the origin, then publishes using
Linux `renameat2(RENAME_NOREPLACE)`. An existing directory or symlink is never
overwritten. A name collision between forks is reported; Hubi does not pick an
alternative name automatically. SIGINT/TERM clean helpers and staging in finally.
After uncatchable SIGKILL, the hidden marked staging directory is recovered under
the clone lock on the next clone; the bounded service prevents an indefinite
helper. A partial clone is never advertised as a local repository.

## Tests and independent review

```console
make test        # units anywhere; real-runtime cases skip without disposable UID
make integration # sudo supervisor creates separate UID, tests, benchmarks, cleans
make build       # creates an uninstalled bundle
```

Never run legacy v5 destructive harnesses on the owner UID. The v6 lab uses
`hubi6test`, a marked disposable HOME, its own lingering user manager, private
bus/socket/units/repos, a separate loopback sshd allowing only this test user,
and CPU/memory/process limits. Runtime tests refuse UID 0/1000. Kernel probes
verify owner signals, sockets and cgroup writes are denied. `tools/test_v6.py`
always tears down the lab in finally. For focused development, use
`sudo python3 tools/v6_lab.py create|sync|run|cleanup` explicitly.

The input audit is preserved in [AUDIT_INPUT.md](AUDIT_INPUT.md); the owner's
implementation request supersedes its optional v5 fix and cutover suggestions.
The [implementation report](IMPLEMENTATION_REPORT.md) maps all 19 contracts and
F1–F15 to code/tests, records measurements, isolation and live fingerprints.

## Doctor and exit semantics

Doctor is read-only and returns JSON status PASS/WARN/FAIL. PASS means all core
runtime checks plus configured Codex, Claude, terminal and gh are available
(ready=true, rc=0). Missing Codex/Claude/gh with a healthy core means WARN
(ready=false, runtime_ready=true, rc=3). Missing core dependencies, private
layout, ownership, linger, kill capability or inconsistent runtime means FAIL
(rc=1). Clean cutover requires full PASS. Repo issues remain separate diagnostics.
`list --json` always emits inventory when runtime can be represented, but exits
1 for DOWN/DEGRADED/UNKNOWN; healthy inventory exits 0. EXITED includes tmux's
exit_status/exit_signal. Start on EXITED refuses with an actionable error.
Initial start also checks the immediate exec/ack window (75 ms plus its final
snapshot); an agent exiting there causes failure and remains inspectable as
EXITED. This does not guarantee the agent's later availability.
EOF/closed stdin is 0, Ctrl+C 130, SIGHUP/PTY hangup 129, SIGTERM 143. --debug
includes tracebacks; unknown keys/types and explicit missing config are errors.

Agent stop defaults: INT up to 3 s, TERM up to 2 s, then KILL and verification up
to 2 s. Terminal stop: HUP up to 1 s, TERM up to 1 s, then the same KILL check.
The windows are configurable under [stop]; early clean exit does not wait out
the entire grace period. Ownership/invocation is reverified before every signal.

Stable lock files are intentional: unlinking them while waiters may exist would
create lock inode races. Uninstall removes them only after global v6 zero-state.
Mutation paths remove private abandoned atomic-write temps older than a day.
Workspace UUIDs also detect a moved checkout and prevent a duplicate of the same
type/instance there; distinct intentional instances remain supported.
