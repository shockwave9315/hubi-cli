# >>> HUBI AUTOLOGIN >>>
if [[ $- == *i* ]] \
   && [[ -n "${SSH_TTY:-}" ]] \
   && [[ -z "${TMUX:-}" ]] \
   && [[ -z "${HUBI_ACTIVE:-}" ]] \
   && [[ -z "${HUBI_NOAUTO:-}" ]] \
   && [[ -x "$HOME/.local/bin/hubi" ]]; then

    "$HOME/.local/bin/hubi"
    HUBI_RC=$?

    if [[ "$HUBI_RC" -eq 99 ]]; then
        exit
    elif [[ "$HUBI_RC" -eq 98 ]]; then
        export HUBI_NOAUTO=1
    fi
fi
# <<< HUBI AUTOLOGIN <<<
