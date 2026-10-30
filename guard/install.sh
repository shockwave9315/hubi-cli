#!/usr/bin/env bash
# Instaluje strażnika limitu Codexa z repozytorium hubi-cli do jego katalogu
# (domyślnie ~/.cache/praca/codex-watchdog; inny: pierwszy argument albo WD_DIR).
# Kopiuje tylko brakujące pliki. Gdy w katalogu jest już INNY plik o tej samej
# nazwie, nic nie kopiuje i to zgłasza: działający strażnik i jego wersje są
# ważniejsze niż kopia z repozytorium. Inny README.md zostaje bez zmian, tak samo
# watchdog.sh przełączony na jedną z zachowanych wersji (z repozytorium albo z katalogu).
# Nie uruchamia strażnika.
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
DEST=${1:-${WD_DIR:-$HOME/.cache/praca/codex-watchdog}}
FILES=(watchdog.v4.sh watchdog.v48.sh watchdog.v49.sh watchdog.sh limits.py reset_credit.py README.md)

conflicts=() own=()
same() {   # 0 = plik może zostać: taki sam, własny README albo watchdog.sh = jedna z zachowanych wersji
  local f=$1 v
  cmp -s "$SRC/$f" "$DEST/$f" && return 0
  if [[ $f == README.md ]]; then own+=("README.md (własny)"); return 0; fi   # aktualny opis jest w repozytorium
  [[ $f == watchdog.sh ]] || return 1
  # hubi guard version przełącza na każdą watchdog.v*.sh z katalogu strażnika, nie tylko na te z repozytorium
  for v in "$SRC"/watchdog.v*.sh "$DEST"/watchdog.v*.sh; do
    if [[ -f $v ]] && cmp -s "$v" "$DEST/$f"; then own+=("watchdog.sh (= $(basename "$v"))"); return 0; fi
  done
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
copied=0 tmp=""
trap 'rm -f -- "$tmp"' EXIT
stop() { echo "przerwane przy $1: $2; skopiowano wcześniej $copied plików, reszta bez zmian" >&2; exit 1; }
for f in "${FILES[@]}"; do
  [[ -e $DEST/$f || -L $DEST/$f ]] && continue
  tmp=$(mktemp "$DEST/.install-$f.XXXXXX")
  cp -p "$SRC/$f" "$tmp" || stop "$f" "nie udało się skopiować"
  # ln zamiast mv: plik pojawia się w całości (bash czyta skrypt w trakcie działania) i nigdy nie nadpisuje
  # pliku, który powstał w międzyczasie.
  ln "$tmp" "$DEST/$f" 2>/dev/null || stop "$f" "plik powstał w trakcie instalacji albo katalog nie przyjmuje dowiązań"
  rm -f -- "$tmp"; tmp=""
  copied=$((copied+1))
done
echo "strażnik w $DEST: skopiowano $copied z ${#FILES[@]} plików"
((${#own[@]})) && printf 'zostawiono: %s\n' "${own[@]}"
echo "start: hubi guard start CP1 codex/<repo> claude/<repo>   (opis: $DEST/README.md)"
