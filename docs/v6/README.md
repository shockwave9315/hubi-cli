# Hubi v6 — development/review build

Python 3.11+, standard library only. The unchanged executable `../../hubi` is
v5. V6 is `python3 -m hubi6`, a separate package and runtime. This branch does
not install anything on the owner account, disable v5 or perform a cutover.

```console
python3 -m hubi6 --version
python3 -m hubi6 doctor --json
python3 -m hubi6 list --json
python3 -m hubi6 start codex my-repo --instance primary
python3 -m hubi6 start claude my-repo --resume
python3 -m hubi6 start terminal my-repo --instance shell2
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

`stop` and `reconcile` explicitly authorize stopping the specified generation.
Reconcile verifies the independent ownership receipts and removes inconsistent
session metadata; it does not sweep unverified objects. An unambiguous ID prefix
is accepted. Stop never requires the repository to exist. `--generation` protects
a confirmation made earlier; IDs themselves also include an immutable generation.

The menu uses a complete snapshot, one screen write, line + Enter input and a
bounded bracketed-paste quarantine. Common keys: `w` workloads, `g` GitHub,
`b` back, `>`/`<` pages, `q` shell (98), `x` disconnect (99). Repository actions:
`c` Codex, `l` Claude, `t` terminal; agent creation asks new/resume. Workload
actions: `a` attach, `v` view, `s` share, `o` takeover, `d` stop, `i` inspect,
`f` reconcile. Actions unavailable in the current state are hidden.

## Configuration and deployment materials

Configuration: `$XDG_CONFIG_HOME/hubi6/config.toml` (default
`~/.config/hubi6/config.toml`). See [example](../../config/hubi6.example.toml).
`--config FILE` selects another TOML file. Agent entries are argv arrays, not
shell commands. Defaults retain the v5 Codex/Claude flags. Terminal defaults
to Bash without startup files, avoiding autologin recursion.

For a **later, separately authorized clean installation**, the directory bundle
is produced with `make build` at `dist/hubi6`. Its `hubi` wrapper can run from
that directory; the pane worker uses real Python module paths. The bundle
contains the v6 [service](../../systemd/user/hubi6-tmux.service) and
[tmux config](../../config/hubi6-tmux.conf). The service expects the latter at
`~/.config/hubi6/tmux.conf`. Enable linger and the v6 service only as part of
that later deployment. Development does not replace `~/.local/bin/hubi`.

Runtime socket: `/run/user/UID/hubi6/tmux.sock`. Locks and one-use startup
envelopes: `/run/user/UID/hubi6/RUNTIME_ID`. Journal:
`$XDG_STATE_HOME/hubi6/workloads/RUNTIME_ID`. Log: `$XDG_STATE_HOME/hubi6/hubi6.log`
(1 MiB, three rotations). GitHub cache: `$XDG_CACHE_HOME/hubi6`. Every owned
directory is private. There is no fallback to `/tmp` for runtime or locks.

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
All repository discovery and state derivation then runs in Python. No Git
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
markers are removed. A normal stop sends INT (agent) or HUP (terminal), then TERM
and KILL to the complete verified cgroup and removes the exact session.

The service restarts even after a clean tmux exit. Its configuration and every
creation preflight force `exit-empty off`; an option hook also guards ordinary
changes. Sourcing a hostile-to-lifetime user config cannot leave the runtime
permanently down. This is protection from accidental self-destruction, not a
sandbox against deliberate socket targeting or `systemd-run` by an agent.

Manually adding panes/windows through tmux command mode is outside the
single-primary-pane workload contract, as in v5. V6 disables their default
bindings, detects extras as UNKNOWN and refuses stop until they are removed.
It never claims those outside-scope processes were cleaned.

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

Clone runs in its own transient service with a 60-second bound and full-cgroup
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
