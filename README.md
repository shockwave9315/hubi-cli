# hubi-cli

`hubi-cli` is the terminal launcher used on the HUBINET AI devbox. It provides a small Bash UI for working with local Git repositories, persistent `tmux` sessions, OpenAI Codex CLI and Claude Code over SSH.

The repository currently reflects the deployed **Hubi v3** setup.

## Current environment

Hubi expects:

- Linux / Bash
- `tmux`
- `git`
- project repositories under `~/repos`
- Codex CLI available at `~/.npm-global/bin/codex` or in `$PATH`
- Claude Code available at `~/.local/bin/claude` or in `$PATH`

The production files on the devbox are:

| Repository file | Deployed path |
| --- | --- |
| `hubi` | `~/.local/bin/hubi` |
| `tmux.conf` | `~/.tmux.conf` |
| `bashrc-autologin.sh` | HUBI AUTOLOGIN section in `~/.bashrc` |

## What Hubi does

On an interactive SSH login, the `~/.bashrc` integration starts Hubi automatically when:

- the shell is interactive,
- the session came through SSH,
- the user is not already inside `tmux`,
- `HUBI_ACTIVE` is not set,
- `HUBI_NOAUTO` is not set,
- `~/.local/bin/hubi` is executable.

The main menu automatically discovers Git repositories directly below `~/repos` and shows the host, current user and active tmux-session count.

For each project Hubi provides:

- **Codex** — start or resume a persistent `codex-<repo>` tmux session,
- **Claude** — start or resume a persistent `claude-<repo>` tmux session,
- **Project shell** — Bash opened directly in the repository,
- **Git status** — branch / dirty state plus recent commits,
- controls for stopping Codex or Claude sessions.

The global menu also provides:

- a view of active tmux sessions,
- a normal devbox shell,
- **plain prompt** mode that leaves the Hubi menu for the current SSH shell,
- logout.

## Direct commands

Hubi can also be called without navigating the menu:

```bash
hubi
hubi codex REPO
hubi claude REPO
hubi shell REPO
hubi sessions
```

## tmux behavior

The shipped `tmux.conf` enables:

- mouse support,
- a large scrollback history,
- clipboard / OSC52 integration,
- automatic cleanup of exited panes,
- window and pane numbering from `1`,
- automatic renumbering,
- new windows and panes opening in the current working directory,
- a simple session / host / time status bar.

Hubi itself runs outside tmux and attaches to project sessions when needed. This keeps the launcher separate from long-running Codex and Claude sessions and allows SSH clients to disconnect and reconnect without killing the agents.

## SSH disconnect handling

`hubi` traps `HUP` and `TERM`, so a dropped SSH connection does not leave the launcher process hanging in the background. Agent processes remain in their tmux sessions.

## Autologin return codes

The menu uses two special return codes for the `.bashrc` integration:

- `98` — leave Hubi and continue with a normal shell; `HUBI_NOAUTO=1` prevents the launcher from immediately starting again,
- `99` — log out of the SSH shell.

## Deployment

The repository files are the reviewable source for the devbox configuration. After a change is reviewed and accepted, deploy the corresponding files to their production paths.

Example:

```bash
install -m 0755 hubi ~/.local/bin/hubi
cp tmux.conf ~/.tmux.conf
```

For `.bashrc`, keep the contents of `bashrc-autologin.sh` inside the marked `HUBI AUTOLOGIN` section rather than replacing the whole file.

## Repository layout

```text
README.md             project documentation
hubi                  Hubi v3 launcher
bashrc-autologin.sh   SSH autostart integration for ~/.bashrc
tmux.conf             tmux configuration used by the devbox
```

## Development rule

Make and review changes in this repository first. Do not edit the deployed production files as the primary development workflow; deploy only after the repository version has been reviewed and accepted.
