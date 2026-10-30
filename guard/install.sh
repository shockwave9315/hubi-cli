#!/usr/bin/env bash
# Instaluje strażnika limitu Codexa z repozytorium hubi-cli do jego katalogu
# (domyślnie ~/.cache/praca/codex-watchdog; inny: pierwszy argument albo WD_DIR).
# Kopiuje tylko brakujące pliki. Gdy w katalogu jest już INNY plik o tej samej
# nazwie, nic nie kopiuje i to zgłasza: działający strażnik i jego wersje są
# ważniejsze niż kopia z repozytorium. Inny README.md zostaje bez zmian.
# Nie uruchamia strażnika.
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
DEST=${1:-${WD_DIR:-$HOME/.cache/praca/codex-watchdog}}
FILES=(watchdog.v4.sh watchdog.v48.sh watchdog.v49.sh watchdog.sh limits.py reset_credit.py README.md)

conflicts=()
same() {   # watchdog.sh przełączony na inną zachowaną wersję (hubi guard version) to nie konflikt
  local f=$1 v
  cmp -s "$SRC/$f" "$DEST/$f" && return 0
  [[ $f == README.md ]] && return 0          # opis z własnymi notatkami zostaje; aktualny jest w repozytorium
  [[ $f == watchdog.sh ]] || return 1
  for v in "$SRC"/watchdog.v*.sh; do cmp -s "$v" "$DEST/$f" && return 0; done
  return 1
}
for f in "${FILES[@]}"; do
  if [[ -e $DEST/$f || -L $DEST/$f ]] && ! same "$f"; then conflicts+=("$DEST/$f"); fi
done
if ((${#conflicts[@]})); then
  printf 'inny plik już jest: %s\n' "${conflicts[@]}" >&2
  echo "nic nie skopiowano; porównaj pliki i zdecyduj ręcznie (wersję zmienisz poleceniem hubi guard version)" >&2
  exit 1
fi
mkdir -p "$DEST"
copied=0
for f in "${FILES[@]}"; do
  [[ -e $DEST/$f ]] && continue
  tmp=$(mktemp "$DEST/.install-$f.XXXXXX")
  cp -p "$SRC/$f" "$tmp" && mv -f "$tmp" "$DEST/$f"   # nowy plik przez rename: bash czyta skrypt w trakcie działania
  copied=$((copied+1))
done
echo "strażnik w $DEST: skopiowano $copied z ${#FILES[@]} plików (reszta już była taka sama)"
echo "start: hubi guard start CP1 codex/<repo> claude/<repo>   (opis: $DEST/README.md)"
