# Hubi v6 — clean cutover and rollback runbook

These are operator instructions for a **separately authorized future cutover**.
They have not been run on the owner's account. No session import, live handover,
or dual production runtime is supported. Run user commands as the target owner,
not root. Destructive development validation belongs only in the disposable UID.
Keep launchers and automation quiescent during install/uninstall; these operator
steps require zero-state and are not concurrent workload lifecycle operations.

## Install / uninstall

Build from a clean checkout of the reviewed commit:

```console
make build
cat dist/hubi6/REVISION
cat dist/hubi6/SOURCE.json
```

The builder recreates output from scratch. `REVISION` is HEAD for a clean tree;
otherwise it includes `-dirty-SOURCE_DIGEST`, and SOURCE.json records source file
hashes and dirty state. The installer rejects dirty bundles by default. The
explicit `--allow-dirty` option is for disposable development installations only.

```console
python3 dist/hubi6/install.py install
# Installs ~/.local/bin/hubi6 and its bundle/config/unit; starts the v6 service.
# Add --as-hubi only during the clean cutover to replace the hubi launcher.
python3 dist/hubi6/install.py install --as-hubi
hubi6 doctor --json
```

The installer verifies bundle hashes, refuses an active v5 runtime and refuses
any existing v6 scopes/sessions/clients. Existing config.toml is preserved;
service/tmux policy are updated. `--as-hubi` saves the old launcher for rollback.
Linger must already be enabled; the installer does not elevate privileges.

To uninstall, stop workloads through their exact v6 IDs first. The uninstaller
refuses live or unknown v6 scopes, sessions and clients across runtime IDs:

```console
hubi6 list --json
hubi6 stop FULL_WORKLOAD_ID --generation GENERATION
# Repeat for every workload, including retained EXITED sessions.
python3 dist/hubi6/install.py uninstall
```

Uninstall disables only `hubi6-tmux.service`, removes its installed bundle,
managed launcher, configuration, private state/cache/runtime and lock/temp
files. It restores the saved v5 launcher if it still owns the hubi alias.
Workspace UUIDs in readable repos under the configured repo root are removed;
markers in moved/outside/unreadable repos are preserved. Repository contents,
Git index and any v5 configuration/services are preserved. Keep your source
bundle outside the installed prefix for uninstall; the installed bundle also
contains install.py. Run uninstall from the retained separate bundle so that the
command remains available if cleanup is interrupted after deleting the installed
prefix. The private installation manifest remains until cleanup completes.
Correct the reported problem (for example cache permissions), then run the same
uninstall command again. Missing files and an already absent/disabled v6 unit are
accepted after confirming zero-state and that the service is stopped. Restoring
the v5 fallback is idempotent; a restored launcher is not rewritten. Changed
managed files or unknown registry entries are preserved and reported.

## Clean cutover

1. Finish or stop **every v5 workload**, including persistent terminals, using
   v5's normal workflow. Detach all clients. Do not rely on disabling the tmux
   service to stop agent scopes.
2. Verify v5 zero-state with explicit read-only targets:

   ```console
   systemctl --user list-units --all --no-legend 'hubi-*.scope'
   tmux -N -S "/run/user/$(id -u)/tmux-$(id -u)/hubi" list-sessions
   tmux -N -S "/run/user/$(id -u)/tmux-$(id -u)/hubi" list-clients
   ```

   There must be zero workload scopes, sessions and clients. An unavailable
   inventory is not proof of zero-state; resolve it before proceeding. An empty
   tmux server can report “no sessions”. Inspect any inactive/failed units too.
3. Only after confirmed zero-state:

   ```console
   systemctl --user disable --now hubi-tmux.service
   ```

4. Build the reviewed commit from a clean tree, verify REVISION/SOURCE.json, then:

   ```console
   python3 dist/hubi6/install.py install --as-hubi
   hubi doctor --json
   ```

5. Require **status=PASS, ready=true, exit code 0**. WARN is not sufficient for
   cutover. Verify linger, all core dependencies, Codex, Claude and gh. Ensure
   existing gh HTTPS auth works; Hubi never creates a private token store.
   If SSH autologin is used, its block belongs at the end of `~/.profile`
   (see `config/hubi6-autologin.sh`); remove any older copy from `~/.bashrc`.
6. Owner smoke: fresh Codex/Claude sessions and deliberate stop/resume, terminal
   RC/SSH agent, normal/shared/takeover, missing repo inspect/stop, GitHub private
   clone and LOCAL detection. Stop the smoke workloads, verify zero-state.
7. Start fresh v6 workloads. Old tmux sessions, scopes and agents are not imported.

## Rollback

1. Stop **all v6 workloads by exact ID**. If extra panes exist, confirm reconcile
   to repair the exact session, then stop its primary workload. Resolve UNKNOWN
   from surviving receipts; do not guess ownership. Verify all v6 scopes,
   sessions and clients are absent.
2. Run the uninstaller, which disables the v6 runtime and restores its backed-up
   launcher. If needed, reinstall v5 from the pinned fallback source:

   ```console
   install -d -m 700 "$HOME/.config/hubi"
   install -m 644 config/tmux-server.conf "$HOME/.config/hubi/tmux-server.conf"
   install -d -m 755 "$HOME/.config/systemd/user" "$HOME/.local/bin"
   install -m 644 systemd/user/hubi-tmux.service "$HOME/.config/systemd/user/hubi-tmux.service"
   install -m 755 hubi "$HOME/.local/bin/hubi"
   systemctl --user daemon-reload
   systemctl --user enable --now hubi-tmux.service
   ```

3. Verify v5's doctor/preflight through its supported workflow and create fresh
   workloads. Rollback does not recover previous sessions, scopes or agents.
