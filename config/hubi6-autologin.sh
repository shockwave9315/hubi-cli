# shellcheck shell=bash
# >>> HUBI AUTOLOGIN >>>
# Append this block to the END of ~/.profile, not to ~/.bashrc.
#
# ~/.profile usually sources ~/.bashrc first and only then extends PATH (version
# managers, ~/bin, tool directories). A menu started from ~/.bashrc hands every
# agent and terminal the PATH as it was at that moment, without those entries.
# Recovery/bypass: ssh -t HOST 'HUBI_NOAUTO=1 bash -il'
if [ -n "${BASH_VERSION:-}" ] && [[ $- == *i* ]] \
   && [[ -n "${SSH_TTY:-}" ]] \
   && [[ -z "${TMUX:-}" ]] \
   && [[ -z "${HUBI_ACTIVE:-}" ]] \
   && [[ -z "${HUBI6_ACTIVE:-}" ]] \
   && [[ -z "${HUBI_NOAUTO:-}" ]] \
   && [[ -x "$HOME/.local/bin/hubi" ]]; then

    # HUBI_AUTOLOGIN tells the menu that exit code 99 really ends the SSH session.
    HUBI_AUTOLOGIN=1 "$HOME/.local/bin/hubi"
    hubi_autologin_rc=$?

    if [[ "$hubi_autologin_rc" -eq 99 ]]; then
        unset hubi_autologin_rc
        exit
    elif [[ "$hubi_autologin_rc" -eq 98 ]]; then
        export HUBI_NOAUTO=1
    fi
    unset hubi_autologin_rc
fi
# <<< HUBI AUTOLOGIN <<<
