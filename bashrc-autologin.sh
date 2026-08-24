# shellcheck shell=bash
# >>> HUBI AUTOLOGIN >>>
# Recovery/bypass: ssh -t HOST 'HUBI_NOAUTO=1 bash -il'
if [[ $- == *i* ]] \
   && [[ -n "${SSH_TTY:-}" ]] \
   && [[ -z "${TMUX:-}" ]] \
   && [[ -z "${HUBI_ACTIVE:-}" ]] \
   && [[ -z "${HUBI_NOAUTO:-}" ]] \
   && [[ -x "$HOME/.local/bin/hubi" ]]; then

    "$HOME/.local/bin/hubi"
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
