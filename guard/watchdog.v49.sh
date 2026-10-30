#!/usr/bin/env bash
# Strażnik limitu Codexa. Bash + tmux, bez tur modelu.
# v3.2: reset limitu (Weekly + 5 h) zużywa WYŁĄCZNIE za zgodą właściciela: plik reset.armed,
# najwyżej jeden reset na plik; po użyciu plik jest przenoszony do reset.used.<czas>.
# Limity czyta limits.py (codex app-server, tylko account/rateLimits/read).
# v4: sesje wskazuje się nazwą Hubi (codex/praca, claude/praca, claude/hubi-cli/2). Panel ustala
# `hubi6 pane` przy każdym obiegu i każdej wysyłce, więc restart sesji niczego nie psuje.
# v4.1: Codex obudzony w pauzie (np. wiadomością od innej sesji), gdy limit 5 h nadal niski,
# dostaje ponowną pauzę (najwyżej REPAUSE_MAX razy na okno). Po odnowieniu limitu praca trwa dalej.
# v4.2: po odnowieniu limitu "wznów" pomija tylko pewna praca (napis "Working ("), nie sam ruch w panelu.
# v4.3 (review claude/sh): praca = pasek Codexa "(<czas> … to interrupt)" (Working, Compacting context…),
# liczony raz na obieg; po odnowieniu limitu zawsze jeden "wznów", także gdy Codex akurat pracuje;
# pauzę i wznowienie potwierdza pasek, nie sam ruch; liczniki ponownych pauz w pliku stanu.
# v4.4 (re-review claude/sh): LIMIT-5 działa też w RESUME_REQUESTED (nie zależy od rozpoznania paska);
# MAX_HOURS nie kończy strażnika w pauzie; wznów, który nie wychodzi, daje jedno zgłoszenie do Claude.
# v4.5 (właściciel 22.10): osobne progi. Pauza 5 h przy LOW (7%), stop tygodniowy dopiero przy WEEK_LOW (1%).
# v4.6 (właściciel 22.10, opcja 3 z review v4.5): tygodniowy LIMIT-5 każe najpierw commit tego, co jest, bez kończenia zadania.
# v4.7 (właściciel 22.10): tura przerwana błędem „Selected model is at capacity” → sam wysyła wznów (poza pauzą),
# z odstępem między próbami; taki postój nie liczy się jako bezczynność.
# v4.8 (review claude/sh v4.7 + próba na Codexie 0.163): pole wpisywania musi być na dole ekranu (podgląd ^t);
# ostatni wpis sklejany z linii (zawinięcie bez wcięcia), ostrzeżenia «⚠» pomijane;
# wznów po błędzie, który nie wychodzi, daje jedno zgłoszenie do Claude.
# v4.9 (układ weekendowy 23.10): WD_REPORT_TO, czyli adres, który teksty „wznów” podają Codexowi
# dla zgłoszeń, STOP i linii gotowości (np. reviewer codex/praca/temp-rev). Domyślnie to <Claude>,
# a teksty są wtedy takie jak w v4.8. Linia pauzy z LIMIT-5 i zgłoszenia strażnika dalej idą do <Claude>.
# Instrukcja: README.md w tym katalogu.
#
#   watchdog.sh <CP> <Codex> <Claude>     np. watchdog.sh CP17 codex/praca claude/praca
#   (dawny sposób, stałe numery paneli: watchdog.sh CP17 %5 %8; po restarcie sesji przestaje działać)
set -uo pipefail

# --- ustawienia (można zmieniać) ---
LOW=${WD_LOW:-7}                     # LIMIT-5 (pauza 5 h), gdy limitu 5 h zostało <= tyle %
WEEK_LOW=${WD_WEEK_LOW:-1}           # v4.5: LIMIT-5 tygodniowy i WEEKLY_STOP, gdy tygodnia zostało <= tyle %
RESUME_MIN=${WD_RESUME_MIN:-20}      # wznów dopiero, gdy limit 5 h ma >= tyle %
IDLE_MIN=${WD_IDLE_MIN:-60}          # koniec po tylu minutach bezczynności (nie w pauzie)
MAX_HOURS=${WD_MAX_HOURS:-24}        # maksymalny czas działania
POLL=${WD_POLL:-120}                 # co ile sekund sprawdza limity i panel
RESUME_DELAY=${WD_RESUME_DELAY:-120} # sekundy po godzinie resetu, zanim spróbuje wznowić
UNKNOWN_ALERT_MIN=${WD_UNKNOWN_ALERT_MIN:-15}   # zgłoś Claude, gdy limity nieczytelne tak długo
PAUSE_CONFIRM_MIN=${WD_PAUSE_CONFIRM_MIN:-30}   # zgłoś, gdy Codex dalej pracuje po LIMIT-5
RESUME_CONFIRM_MIN=${WD_RESUME_CONFIRM_MIN:-10} # zgłoś, gdy Codex nie ruszył po "wznów"
REPAUSE_MAX=${WD_REPAUSE_MAX:-3}     # v4.1: najwyżej tyle ponownych pauz na jedno okno 5 h, potem tylko zgłoszenie
BAR_RE=${WD_BAR_RE:-'\([0-9][^()]* to interrupt\)'}  # v4.3: pasek pracy Codexa, np. "Working (5s • esc to interrupt)"
ACTIVE_SEC=${WD_ACTIVE_SEC:-30}      # wyjście w panelu w ciągu tylu sekund = Codex pracuje
CAP_RE=${WD_CAP_RE:-'^■[^[:alnum:]]{0,8}Selected model is at capacity'}  # v4.7: błąd Codexa, który przerywa turę
CAP_RETRY_MIN=${WD_CAP_RETRY_MIN:-5} # v4.7: po tym błędzie wznów najwyżej co tyle minut
CAP_MAX=${WD_CAP_MAX:-3}             # v4.7: po tylu wznowieniach bez skutku jedno zgłoszenie do Claude
REPORT_TO=${WD_REPORT_TO:-}           # v4.9: dokąd Codex ma wysyłać zgłoszenia, STOP i linie gotowości (puste = <Claude>)
CAP_SLOW_MIN=${WD_CAP_SLOW_MIN:-30}  # v4.7: potem wznawia już tylko co tyle minut
RESET_AT=${WD_RESET_AT:-1}           # z plikiem reset.armed: zużyj JEDEN reset, gdy tydzień <= tyle %
RESET_TRIES_MAX=${WD_RESET_TRIES_MAX:-1}  # domyślnie JEDNO wywołanie na zgodę; błąd = zgoda zdjęta, zwykła pauza tygodniowa
# Pełne polecenie lidera grupy procesów na pierwszym planie terminala panelu (ps -o args):
CODEX_ARGS_RE=${WD_CODEX_ARGS_RE:-'^(node [^ ]*/bin/codex|[^ ]*/codex)( |$)'}  # Codex (wrapper Node albo binarka)
CLAUDE_ARGS_RE=${WD_CLAUDE_ARGS_RE:-'^([^ ]*/)?claude( |$)'}                    # Claude Code
# ------------------------------------

DIR=${WD_DIR:-$HOME/.cache/praca/codex-watchdog}
S=${WD_TMUX_SOCK:-/run/user/$(id -u)/hubi6/tmux.sock}
LIMITS_CMD=${WD_LIMITS_CMD:-python3 -I $DIR/limits.py}
RESET_CMD=${WD_RESET_CMD:-python3 -I $DIR/reset_credit.py}
PANE_CMD=${WD_PANE_CMD:-$HOME/.local/bin/hubi6 pane}  # nazwa sesji → bieżący panel (pełna ścieżka: systemd-run nie ma ~/.local/bin w PATH)
SEND_CMD=${WD_SEND_CMD:-$HOME/.local/bin/hubi6 send}  # wysyłka po nazwie: blokada na sesję, czeka, gdy panel jest przewijany
RESET_ARM=$DIR/reset.armed           # zgoda właściciela na jeden reset; ważna tylko dla okna tygodniowego WEEK_RESET=<epoch> z pliku
DRYRUN=${WD_DRYRUN:-0}               # testy: 1 = nic nie wkleja, zapisuje do dryrun.out
FAIL_SEND=${WD_FAIL_SEND:-}          # testy: codex|claude = wysyłka do tego rodzaju panelu kończy się błędem
MAX_POLLS=${WD_MAX_POLLS:-0}         # testy: >0 = koniec po tylu obiegach
IDLE_SEC=${WD_IDLE_SEC:-$((IDLE_MIN * 60))}
UNKNOWN_ALERT_SEC=${WD_UNKNOWN_ALERT_SEC:-$((UNKNOWN_ALERT_MIN * 60))}
PAUSE_CONFIRM_SEC=${WD_PAUSE_CONFIRM_SEC:-$((PAUSE_CONFIRM_MIN * 60))}
RESUME_CONFIRM_SEC=${WD_RESUME_CONFIRM_SEC:-$((RESUME_CONFIRM_MIN * 60))}

usage() { echo "użycie: watchdog.sh <CP> <Codex> <Claude>  (np. CP17 codex/praca claude/praca; dawniej numery paneli: CP17 %5 %8)" >&2; exit 2; }
CP=${1:-}; CODEX=${2:-}; CLAUDE=${3:-}
spec_ok() { [[ $1 =~ ^%[0-9]+$ || $1 =~ ^$2/[A-Za-z0-9._-]+(/[A-Za-z0-9][A-Za-z0-9_-]*)?$ ]]; }  # spec_ok <wskazanie> <rodzaj>
# v4.9: kontaktem może być też inna sesja Codexa po nazwie (np. reviewer codex/praca/temp-rev), gdy Claude ma mało limitu.
[[ $CP =~ ^CP[0-9]+[a-z]?$ ]] && spec_ok "$CODEX" codex || usage
if [[ $CLAUDE == codex/*/* && $CLAUDE != "$CODEX" ]] && spec_ok "$CLAUDE" codex; then CLAUDE_KIND=codex
elif spec_ok "$CLAUDE" claude; then CLAUDE_KIND=claude
else usage; fi
# v4.9: tylko nazwa Hubi (bez numeru panelu), Codex albo Claude.
if [[ -n $REPORT_TO ]] && { [[ $REPORT_TO == %* ]] || ! { spec_ok "$REPORT_TO" codex || spec_ok "$REPORT_TO" claude; }; }; then
  echo "zły WD_REPORT_TO=$REPORT_TO (nazwa Hubi, np. codex/praca/temp-rev)" >&2; exit 2
fi
# v4.5: próg spoza 0–100 (np. "1%") w (( )) daje błąd albo 0, a wtedy pauza lub stop tygodniowy nigdy by nie zadziałały.
for v in LOW WEEK_LOW RESUME_MIN RESET_AT; do
  [[ ${!v} =~ ^[0-9]{1,3}$ ]] && (( 10#${!v} <= 100 )) || { echo "zły próg $v=${!v} (liczba 0–100)" >&2; exit 2; }
done
LOW=$((10#$LOW)) WEEK_LOW=$((10#$WEEK_LOW)) RESUME_MIN=$((10#$RESUME_MIN)) RESET_AT=$((10#$RESET_AT))
for v in CAP_RETRY_MIN CAP_MAX CAP_SLOW_MIN; do   # v4.7
  [[ ${!v} =~ ^[0-9]{1,4}$ ]] || { echo "zła wartość $v=${!v} (liczba 0–9999)" >&2; exit 2; }
done
CAP_RETRY_MIN=$((10#$CAP_RETRY_MIN)) CAP_MAX=$((10#$CAP_MAX)) CAP_SLOW_MIN=$((10#$CAP_SLOW_MIN))
CAP_RETRY_SEC=${WD_CAP_RETRY_SEC:-$((CAP_RETRY_MIN * 60))}
CAP_SLOW_SEC=${WD_CAP_SLOW_SEC:-$((CAP_SLOW_MIN * 60))}
key_of() { local k=${1#%}; echo "${k//\//-}"; }   # %5 → 5 (jak dawniej), codex/praca → codex-praca
# Jak Codex ma pisać do Claude: po nazwie przez hubi send, a dawniej wklejaniem do stałego panelu.
if [[ $CLAUDE == %* ]]; then
  TO_CLAUDE="wklej do ${CLAUDE}"; CLAUDE_ADDR="Mój panel to ${CLAUDE}: tam wklejaj"
else
  TO_CLAUDE="wyślij poleceniem «hubi send ${CLAUDE//\// } -m TEKST»"; CLAUDE_ADDR="Do mnie pisz poleceniem «hubi send ${CLAUDE//\// } -m TEKST»:"
fi
# v4.9: zgłoszenia, STOP i linie gotowości do innej sesji niż ta, która dostaje linię pauzy i zgłoszenia strażnika.
if [[ -n $REPORT_TO && $REPORT_TO != "$CLAUDE" ]] || [[ $CLAUDE_KIND == codex ]]; then
  CLAUDE_ADDR="Do reviewera pisz poleceniem «hubi send ${REPORT_TO:-$CLAUDE} -m TEKST»:"; CLAUDE_ADDR=${CLAUDE_ADDR//\// }
fi
mkdir -p "$DIR" || exit 1
LOG=$DIR/log
STATE_FILE=$DIR/state.$(key_of "$CODEX")

now() { date +%s; }
log() { echo "$(date -u +%FT%TZ) $CP $CODEX $STATE: $*" >> "$LOG"; }
num() { [[ $1 =~ ^[0-9]+$ ]]; }

CODEX_PANE="" GONE_SINCE=0
STATE=RUNNING PAUSE_WINDOW="" PAUSE_SENT_AT=0 RESUMED_AT=0 WEEK_SENT="" NOTIFIED="" RESET_KEY="" RESET_TRIES=0 CREDITS=""
REPAUSES=0 REPAUSE_WIN=""

# Jedna instancja na panel Codexa.
exec 9>"$DIR/lock.$(key_of "$CODEX")"
flock -n 9 || { echo "strażnik dla $CODEX już działa" >&2; exit 1; }
cleanup() { rm -f "$DIR"/msg.$$.*; }
trap 'log "koniec: sygnał"; exit 0' INT TERM
trap cleanup EXIT

save() {
  local tmp="$STATE_FILE.$$"
  printf 'CP=%s\nSTATE=%s\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=%s\nWEEK_SENT=%s\nNOTIFIED=%s\nRESET_KEY=%s\nRESET_TRIES=%s\nREPAUSES=%s\nREPAUSE_WIN=%s\n' \
    "$CP" "$STATE" "$PAUSE_WINDOW" "$PAUSE_SENT_AT" "$RESUMED_AT" "$WEEK_SENT" "$NOTIFIED" "$RESET_KEY" "$RESET_TRIES" "$REPAUSES" "$REPAUSE_WIN" > "$tmp" && mv "$tmp" "$STATE_FILE"
}
load() { # tylko znane klucze i bezpieczne wartości; nic nie jest wykonywane
  [[ -f $STATE_FILE ]] || return 0
  local k v saved_cp="" st="" pw="" ps="" ra="" ws="" nt="" rk="" rt="" rp="" rw=""
  while IFS='=' read -r k v; do
    [[ $v =~ ^[A-Za-z0-9_,-]*$ ]] || continue
    case $k in CP) saved_cp=$v;; STATE) st=$v;; PAUSE_WINDOW) pw=$v;; PAUSE_SENT_AT) ps=$v;;
      RESUMED_AT) ra=$v;; WEEK_SENT) ws=$v;; NOTIFIED) nt=$v;; RESET_KEY) rk=$v;; RESET_TRIES) rt=$v;;
      REPAUSES) rp=$v;; REPAUSE_WIN) rw=$v;; esac
  done < "$STATE_FILE"
  # Blokada tygodniowa dotyczy konta, nie CP: zdejmuje ją tylko człowiek (usunięcie pliku stanu).
  if [[ $st == WEEKLY_STOP ]]; then
    echo "blokada tygodniowa w $STATE_FILE: wznów Codexa ręcznie, usuń ten plik i uruchom strażnika ponownie" >&2
    STATE=WEEKLY_STOP; log "odmowa startu: blokada tygodniowa"; exit 3
  fi
  # Próba resetu dotyczy konta: ten sam klucz idempotencji po restarcie, niezależnie od CP.
  [[ $rk =~ ^[0-9a-f-]{36}$ ]] && RESET_KEY=$rk && num "$rt" && RESET_TRIES=$rt
  [[ $saved_cp == "$CP" ]] || return 0
  WEEK_SENT=$ws NOTIFIED=$nt
  num "$rp" && num "$rw" && REPAUSES=$rp REPAUSE_WIN=$rw   # v4.3: limit ponownych pauz przeżywa restart
  case $st in PAUSE_REQUESTED|PAUSED_5H|RESUME_REQUESTED)
    STATE=$st PAUSE_WINDOW=$pw; num "$ps" && PAUSE_SENT_AT=$ps; num "$ra" && RESUMED_AT=$ra;; esac
}

resolve() { # resolve <wskazanie>: bieżący panel %N. Nazwę sprawdza za każdym razem; stały %N zwraca bez zmian.
  local spec=$1 kind rest repo name out
  if [[ $spec =~ ^%[0-9]+$ ]]; then echo "$spec"; return 0; fi
  kind=${spec%%/*} rest=${spec#*/} repo=${rest%%/*} name=""
  [[ $rest == */* ]] && name=${rest#*/}
  # shellcheck disable=SC2086  # PANE_CMD to polecenie z argumentami
  out=$(timeout 20 ${PANE_CMD:-$HOME/.local/bin/hubi6 pane} "$kind" "$repo" ${name:+"$name"} 2>/dev/null) || return 1
  [[ $out =~ ^%[0-9]+$ ]] || return 1
  echo "$out"
}
pane_ok() { # pane_ok <panel> <codex|claude>: panel żyje, należy do sesji tego rodzaju,
            # a na pierwszym planie jego terminala działa właśnie Codex / Claude (nie dowolny node ani powłoka)
  local info name dead pid pg args re
  info=$(tmux -S "$S" display-message -p -t "$1" '#{session_name} #{pane_dead} #{pane_pid}' 2>/dev/null) || return 1
  read -r name dead pid <<< "$info"
  [[ $name == *-"$2"-* && $dead == 0 ]] && num "$pid" || return 1
  pg=$(ps -o tpgid= -p "$pid" 2>/dev/null | tr -d ' ') && num "$pg" && (( pg > 0 )) || return 1
  args=$(ps -o args= -p "$pg" 2>/dev/null) || return 1
  [[ $2 == codex ]] && re=$CODEX_ARGS_RE || re=$CLAUDE_ARGS_RE
  [[ $args =~ $re ]]
}
activity_age() { # sekundy od ostatniego wyjścia w oknie panelu Codexa (tmux window_activity)
  local a
  if [[ -n ${WD_SCREEN_FILE:-} ]]; then a=$(cat "$WD_SCREEN_FILE.activity" 2>/dev/null) || a=0
  else a=$(tmux -S "$S" display-message -p -t "$CODEX_PANE" '#{window_activity}' 2>/dev/null) || return 1; fi
  num "$a" || return 1
  echo $(( $(now) - a ))
}
cap_on_screen() { # v4.7/v4.8: 0 = ostatni wpis rozmowy nad polem wpisywania to błąd CAP_RE.
                  # Pole = ostatnia linia «› »; pod nią tylko linie puste albo wcięte (stopka), inaczej to wiadomość
                  # z historii (np. podgląd ^t). Idąc w górę pomija puste linie, separatory «─» i całe ostrzeżenia «⚠ …».
                  # Wpis = blok niepustych linii, sklejony spacjami: Codex nie wcina zawiniętego ciągu dalszego błędu
                  # (sprawdzone na Codexie 0.163 z błędem 400), więc linie łączymy zamiast sprawdzać pojedynczo.
  local -a l; local i j k c=-1 t blk=""
  mapfile -t l <<< "$1"
  for i in "${!l[@]}"; do [[ ${l[i]} == '› '* ]] && c=$i; done
  (( c >= 1 )) || return 1
  for (( i = c + 1; i < ${#l[@]}; i++ )); do [[ ${l[i]} =~ ^[^[:space:]] ]] && return 1; done
  i=$((c - 1)) j=-1
  while (( i >= 0 )); do
    if [[ ${l[i]} =~ ^[[:space:]]*$ || ${l[i]} == ─* ]]; then i=$((i - 1)); continue; fi
    for (( j = i; j >= 0; j-- )); do [[ ${l[j]} =~ ^[[:space:]]*$ ]] && break; done
    [[ ${l[j+1]} == ⚠* ]] || break
    i=$j
  done
  (( i >= 0 )) || return 1
  for (( k = j + 1; k <= i; k++ )); do
    t=${l[k]}; t=${t#"${t%%[![:space:]]*}"}; t=${t%"${t##*[![:space:]]}"}
    blk+="${blk:+ }$t"
  done
  [[ $blk =~ $CAP_RE ]]
}
screen_state() { # busy | cap | work | idle | error, z jednego odczytu ekranu.
                 # busy  = pasek pracy Codexa (BAR_RE) w ostatnich 15 liniach: pewna praca;
                 # cap   = v4.7: brak paska, tura przerwana błędem „model at capacity” (cap_on_screen);
                 # work  = tylko ruch w panelu w ciągu ACTIVE_SEC (też przerysowanie, podgląd okna);
                 # idle  = ani paska, ani ruchu; błąd odczytu nigdy nie znaczy "bezczynny".
  local out age
  if [[ -n ${WD_SCREEN_FILE:-} ]]; then out=$(cat "$WD_SCREEN_FILE" 2>/dev/null) || { echo error; return; }
  else out=$(tmux -S "$S" capture-pane -p -t "$CODEX_PANE" 2>/dev/null) || { echo error; return; }; fi
  [[ -n ${out//[[:space:]]/} ]] || { echo error; return; }
  if tail -15 <<< "$out" | grep -qE "$BAR_RE"; then echo busy; return; fi
  if cap_on_screen "$out"; then echo cap; return; fi
  age=$(activity_age) || { echo error; return; }
  if (( age < ACTIVE_SEC )); then echo work; else echo idle; fi
}

send_to() { # send_to <wskazanie> <codex|claude> <tekst>; tekst tworzy wyłącznie ten skrypt, jedna linia
  local spec=$1 kind=$2 text=$3 buf="wd-$$-$RANDOM" f rc=1 pane rest repo name
  # Panel ustalany tuż przed wysyłką: po restarcie odbiorcy ma inny numer.
  pane=$(resolve "$spec") || { log "NIE wysłano do $spec: sesji nie ma albo nie działa"; return 1; }
  # Kontrola odbiorcy tuż przed wysyłką: po zakończeniu Codexa/Claude panel może trzymać zwykłą powłokę.
  pane_ok "$pane" "$kind" || { log "NIE wysłano do $pane: panel nie jest żywym $kind"; return 1; }
  [[ $FAIL_SEND == "$kind" ]] && { log "NIE wysłano do $pane: test FAIL_SEND"; return 1; }
  if [[ $DRYRUN == 1 ]]; then printf '%s\t%s\t%s\n' "$(now)" "$pane" "$text" >> "$DIR/dryrun.out"; log "dry-run → $pane"; return 0; fi
  if [[ $spec != %* ]]; then
    # Po nazwie wysyła hubi send: sam ustala panel, nie skleja równoczesnych wiadomości (np. od Codexa)
    # i nic nie wkleja, gdy ktoś przewija panel (kod 4). Każdy błąd = ponowienie w następnym obiegu.
    rest=${spec#*/} repo=${rest%%/*} name=""
    [[ $rest == */* ]] && name=${rest#*/}
    # shellcheck disable=SC2086  # SEND_CMD to polecenie z argumentami
    timeout 30 ${SEND_CMD:-$HOME/.local/bin/hubi6 send} "$kind" "$repo" ${name:+"$name"} -m "${text//$'\n'/ }" 2>>"$LOG"; rc=$?
    log "wysłano do $spec ($pane) rc=$rc"; return $rc
  fi
  # Stały panel: w trybie kopiowania tmux wkleja bez znaczników, a Enter przepada.
  if [[ $(tmux -S "$S" display-message -p -t "$pane" '#{pane_in_mode}' 2>/dev/null) != 0 ]]; then
    log "NIE wysłano do $pane: panel przewijany (tryb kopiowania); ponowię"; return 1
  fi
  f=$(mktemp "$DIR/msg.$$.XXXXXX") || return 1
  printf '%s' "${text//$'\n'/ }" > "$f"
  if tmux -S "$S" load-buffer -b "$buf" "$f" && tmux -S "$S" paste-buffer -p -d -b "$buf" -t "$pane"; then
    sleep 1; tmux -S "$S" send-keys -t "$pane" Enter && rc=0
  else
    tmux -S "$S" delete-buffer -b "$buf" 2>/dev/null
  fi
  rm -f "$f"; log "wysłano do $pane rc=$rc"; return $rc
}
notify() { # notify <klucz> <tekst>: najwyżej raz na klucz, tylko do zweryfikowanego panelu Claude
  [[ ,$NOTIFIED, == *,"$1",* ]] && return 0
  if send_to "$CLAUDE" "$CLAUDE_KIND" "STRAŻNIK $CP: $2"; then
    NOTIFIED+="${NOTIFIED:+,}$1"; save
  else
    log "nie powiadomiono Claude ($CLAUDE): $1"
  fi
}

probe() { # ustawia PSTATUS F_LEFT F_RESET W_LEFT W_RESET ALLOWED PREASON
  local out kv
  PSTATUS=error F_LEFT=unknown F_RESET=unknown W_LEFT=unknown W_RESET=unknown ALLOWED=unknown PREASON=""
  # shellcheck disable=SC2086  # LIMITS_CMD to polecenie z argumentami, ustawiane przez operatora
  out=$(timeout 45 $LIMITS_CMD 2>/dev/null) || out="status=error reason=probe-failed"
  for kv in $out; do
    case $kv in
      status=ok) PSTATUS=ok;; reason=*) PREASON=${kv#*=};;
      five_left=*) num "${kv#*=}" && F_LEFT=${kv#*=};; five_reset=*) num "${kv#*=}" && F_RESET=${kv#*=};;
      week_left=*) num "${kv#*=}" && W_LEFT=${kv#*=};; week_reset=*) num "${kv#*=}" && W_RESET=${kv#*=};;
      allowed=true|allowed=false) ALLOWED=${kv#*=};;
    esac
  done
  num "$F_LEFT" && (( F_LEFT > 100 )) && F_LEFT=unknown
  num "$W_LEFT" && (( W_LEFT > 100 )) && W_LEFT=unknown
  [[ $PSTATUS == ok ]] || { F_LEFT=unknown W_LEFT=unknown ALLOWED=unknown; }
  # Pełny odczyt: oba okna znane. Niepełny = jak nieczytelny (alarm po czasie), choć znane części nadal chronią.
  COMPLETE=0
  [[ $PSTATUS == ok ]] && num "$F_LEFT" && num "$W_LEFT" && COMPLETE=1
  [[ $PSTATUS == ok && $COMPLETE == 0 ]] && PREASON=${PREASON:-incomplete}
}

arm_week() { sed -n 's/^WEEK_RESET=\([0-9][0-9]*\)$/\1/p' "$RESET_ARM" 2>/dev/null | head -1; }
armed() { [[ -f $RESET_ARM ]] && num "$W_RESET" && [[ $(arm_week) == "$W_RESET" ]]; }
expire_arm() { # zgoda dotyczy jednego okna tygodniowego: ręczny reset albo odnowienie tygodnia ją unieważnia
  local w
  [[ -f $RESET_ARM ]] && num "$W_RESET" || return 0
  w=$(arm_week)
  [[ $w == "$W_RESET" ]] && return 0
  mv -f "$RESET_ARM" "$DIR/reset.expired.$(date -u +%Y%m%dT%H%M%SZ)" 2>/dev/null
  log "zgoda na reset wygasła (okno zgody ${w:-brak}, obecne $W_RESET)"
  notify "armexp-$W_RESET" "zgoda na reset wygasła: okno tygodniowe jest inne niż w zgodzie (ręczny reset albo odnowienie tygodnia). Nie zużyję żadnego resetu."
}
week_ok() { (( W_LEFT > WEEK_LOW )) || { armed && (( W_LEFT > RESET_AT )); }; }
try_reset() { # zużywa JEDEN reset (zgoda: reset.armed); 0 = limity odnowione
  local out kv outcome="" credits="" stamp
  if [[ -z $RESET_KEY ]]; then
    RESET_KEY=$(cat /proc/sys/kernel/random/uuid 2>/dev/null)
    if ! [[ $RESET_KEY =~ ^[0-9a-f-]{36}$ ]]; then RESET_KEY=""; return 1; fi
    RESET_TRIES=0
  fi
  RESET_TRIES=$((RESET_TRIES + 1)); save   # klucz zapisany PRZED wywołaniem: ponowienie nie zużyje drugiego resetu
  # shellcheck disable=SC2086  # RESET_CMD to polecenie z argumentami, ustawiane przez operatora
  out=$(timeout 90 $RESET_CMD "$RESET_KEY" 2>/dev/null) || out="status=error reason=reset-failed"
  for kv in $out; do case $kv in outcome=*) outcome=${kv#*=};; credits_before=*) credits=${kv#*=};; esac; done
  [[ $out == status=ok* ]] || outcome=error
  log "reset: próba $RESET_TRIES, wynik ${outcome:-?} ($out)"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  case $outcome in
    reset|alreadyRedeemed)
      mv -f "$RESET_ARM" "$DIR/reset.used.$stamp" 2>/dev/null
      RESET_KEY="" RESET_TRIES=0 CREDITS="?"
      if num "$credits"; then CREDITS=$((credits - 1)); fi
      save; return 0;;
    noCredit|nothingToReset)
      mv -f "$RESET_ARM" "$DIR/reset.failed.$stamp" 2>/dev/null
      RESET_KEY="" RESET_TRIES=0; save
      notify "resetfail-$W_RESET" "nie zużyłem resetu (wynik: $outcome); limit tygodniowy ${W_LEFT}%, wracam do zwykłej pauzy tygodniowej."
      return 1;;
    *)
      if (( RESET_TRIES >= RESET_TRIES_MAX )); then
        mv -f "$RESET_ARM" "$DIR/reset.failed.$stamp" 2>/dev/null
        RESET_KEY="" RESET_TRIES=0; save
        notify "resetfail-$W_RESET" "reset nie przeszedł po $RESET_TRIES_MAX próbach (wynik: ${outcome:-?}); wracam do zwykłej pauzy tygodniowej. Sprawdź w Codexie /status, czy reset jednak nie przeszedł."
      fi
      return 1;;
  esac
}
after_reset() { # po udanym resecie: świeży odczyt, Codex pracuje dalej bez pauzy
  local prev=$STATE
  probe; SCR=$(screen_state)
  log "reset zużyty: 5 h ${F_LEFT}%, tydzień ${W_LEFT}% (stan przed: $prev, ekran: $SCR)"
  if [[ $prev == RUNNING && ( $SCR == busy || $SCR == work ) ]]; then
    STATE=RUNNING
  elif send_to "$CODEX" codex "$CP: limity Codexa odnowione resetem (od Claude, automatycznie; 5 h ${F_LEFT}%, tydzień ${W_LEFT}%). Kontynuuj pracę od miejsca, w którym przerwałeś. Zasada LIMIT-5 bez zmian."; then
    STATE=RESUME_REQUESTED RESUMED_AT=$(now) PAUSE_WINDOW=""
  else
    STATE=PAUSED_5H PAUSE_WINDOW=""   # zwykłe wznowienie wyśle "wznów" w kolejnym obiegu
  fi
  IDLE=0 IDLE_STREAK=0; save
  notify "reset-$(now)" "zużyłem JEDEN reset limitu Codexa (zgoda właściciela, plik reset.armed). Teraz 5 h ${F_LEFT}%, tydzień ${W_LEFT}%. Pozostałe resety: ${CREDITS:-?}; kolejny tylko po nowej zgodzie."
}
resume_ok() { # pewny odczyt i odnowiony limit: wolno (i trzeba) wysłać wznów
  (( COMPLETE )) && (( F_LEFT >= RESUME_MIN )) && week_ok && [[ $ALLOWED == true ]] || return 1
  num "$PAUSE_WINDOW" || return 0                            # okno pauzy nieznane: decyduje świeży odczyt
  (( $(now) >= PAUSE_WINDOW + RESUME_DELAY )) && return 0    # minęła godzina odnowienia
  num "$F_RESET" && [[ $F_RESET != "$PAUSE_WINDOW" ]]        # nowe okno wcześniej (np. ręczny reset)
}
resume_blocked() { # v4.4: wznów należny, ale nie wyszedł; po RESUME_CONFIRM_SEC jedno zgłoszenie na okno
  (( RESUME_FAIL_SINCE )) || RESUME_FAIL_SINCE=$(now)
  log "wznów należny, ale nie wysłany: $1; ponowię"
  (( $(now) - RESUME_FAIL_SINCE >= RESUME_CONFIRM_SEC )) && notify "resumefail-${PAUSE_WINDOW:-x}" "limit Codexa odnowiony, ale od $(( ($(now) - RESUME_FAIL_SINCE) / 60 )) min nie mogę wysłać mu «$CP wznów» ($1); ponawiam co obieg."
}
send_resume() { # v4.3: jeden wznów po odnowieniu, także gdy Codex akurat pracuje: wcześniej dostał
                # "czekaj na wznów", więc bez tej wiadomości po bieżącej turze stanąłby na dobre.
  [[ $SCR == error ]] && { resume_blocked "ekran Codexa nieczytelny"; return; }
  if send_to "$CODEX" codex "$CP wznów (od Claude, automatycznie po odnowieniu limitu: 5 h ${F_LEFT}%, tydzień ${W_LEFT}%). ${CLAUDE_ADDR} zgłoszenia, STOP i linie gotowości. Jeśli właśnie coś robisz, najpierw to dokończ; potem kontynuuj od ostatniej pauzy. Zasada LIMIT-5 bez zmian."; then
    RESUME_FAIL_SINCE=0
    STATE=RESUME_REQUESTED RESUMED_AT=$(now) IDLE_STREAK=0 WAKE_STREAK=0; save
    log "wznów wysłany (5 h ${F_LEFT}%, tydzień ${W_LEFT}%, ekran: $SCR)"
  else
    resume_blocked "wysyłka nieudana"
  fi
}
pause_if_low() { # LIMIT-5 przy niskim limicie 5 h, raz na okno. 0 = limit niski (wysłane albo ponowienie w kolejnym obiegu).
  num "$F_LEFT" && (( F_LEFT <= LOW )) && [[ $PAUSE_WINDOW != "${F_RESET}" ]] || return 1
  if send_to "$CODEX" codex "LIMIT-5 (od Claude, automatycznie): zostało ok. ${F_LEFT}% limitu 5 h. Dokończ bieżące zadanie, zrób commit (lokalnie wystarczy, push jeśli zdążysz), ${TO_CLAUDE} linię «$CP pauza: co zrobione, co dalej» i czekaj na «$CP wznów»."; then
    STATE=PAUSE_REQUESTED PAUSE_WINDOW=$F_RESET PAUSE_SENT_AT=$(now) IDLE_STREAK=0; save
    log "LIMIT-5 wysłany przy ${F_LEFT}%, reset $F_RESET"
  else
    notify "sendfail-$F_RESET" "limit 5 h Codexa ${F_LEFT}%, ale NIE udało się wysłać mu LIMIT-5; ponawiam co obieg."
  fi
  return 0
}
cap_resume() { # v4.7: w RUNNING / RESUME_REQUESTED tura przerwana błędem „model at capacity”: sam wysyła wznów.
               # Dwa obiegi z błędem z rzędu, odstęp CAP_RETRY_SEC, po CAP_MAX próbach jedno zgłoszenie i odstęp CAP_SLOW_SEC.
  local gap=$CAP_RETRY_SEC
  CAP_STREAK=$((CAP_STREAK + 1)) IDLE=0
  (( CAP_STREAK >= 2 )) || { log "błąd capacity na ekranie Codexa; sprawdzę w następnym obiegu"; return; }
  if (( CAP_TRIES >= CAP_MAX )); then
    gap=$CAP_SLOW_SEC
    notify "cap-$CAP_SINCE" "Codex nadal stoi na błędzie «model at capacity» po ${CAP_MAX} automatycznych wznowieniach; ponawiam co ${CAP_SLOW_MIN} min."
  fi
  (( CAP_LAST == 0 || $(now) - CAP_LAST >= gap )) || return
  # Tylko gdy wiadomo, że wolno pracować: inaczej decyduje LIMIT-5 albo zwykłe wznowienie po odnowieniu limitu.
  if ! { (( COMPLETE )) && (( F_LEFT > LOW )) && week_ok && [[ $ALLOWED == true ]]; }; then
    log "błąd capacity, ale limity niepełne albo niskie (5 h ${F_LEFT}, tydzień ${W_LEFT}, dozwolone ${ALLOWED}); nie wznawiam"; return
  fi
  if send_to "$CODEX" codex "$CP wznów po błędzie (od Claude, automatycznie): poprzednia tura przerwała się błędem «model at capacity». Kontynuuj od miejsca, w którym przerwałeś. ${CLAUDE_ADDR} zgłoszenia, STOP i linie gotowości. Zasada LIMIT-5 bez zmian."; then
    (( CAP_TRIES )) || CAP_SINCE=$(now)
    CAP_TRIES=$((CAP_TRIES + 1)) CAP_LAST=$(now) CAP_STREAK=0 CAP_FAIL_SINCE=0
    log "błąd capacity: wznów wysłany (próba ${CAP_TRIES})"
  else   # v4.8: jak resume_blocked: ponawia co obieg, po RESUME_CONFIRM_SEC jedno zgłoszenie na epizod
    (( CAP_FAIL_SINCE )) || CAP_FAIL_SINCE=$(now)
    (( $(now) - CAP_FAIL_SINCE >= RESUME_CONFIRM_SEC )) && notify "capfail-$CAP_FAIL_SINCE" "Codex stoi na błędzie «model at capacity», a od $(( ($(now) - CAP_FAIL_SINCE) / 60 )) min nie mogę wysłać mu wznowienia; ponawiam co obieg."
  fi
}
woke_in_pause() { # v4.1: w PAUSED_5H pasek pracy, a warunki wznowienia nie są spełnione
  local low=0 until
  [[ $SCR == busy ]] || { WAKE_STREAK=0; return 0; }   # tylko pewna praca; sam ruch w panelu nic nie zmienia
  # Limit wystarczający, ale odczyt niepełny albo okno jeszcze nieodnowione: nie zatrzymujemy i nie
  # przechodzimy do RUNNING bez wznów (v4.3); wznów wyśle resume_ok przy pełnym odczycie.
  if num "$F_LEFT" && (( F_LEFT >= RESUME_MIN )); then
    WAKE_STREAK=0; log "Codex pracuje w pauzie, limit 5 h ${F_LEFT}%; nie zatrzymuję, czekam na pełny odczyt do wznów"
    return
  fi
  # Niski limit: znany odczyt poniżej progu wznowienia albo, bez odczytu, okno 5 h jeszcze się nie odnowiło.
  if num "$F_LEFT"; then low=1
  elif num "$PAUSE_WINDOW" && (( $(now) < PAUSE_WINDOW )); then low=1; fi
  (( low )) || { WAKE_STREAK=0; log "Codex pracuje w pauzie, limit 5 h nieznany; nie zatrzymuję"; return; }
  # Dwa obiegi z paskiem pracy z rzędu, zanim pójdzie wiadomość.
  WAKE_STREAK=$((WAKE_STREAK + 1))
  (( WAKE_STREAK >= 2 )) || { log "pasek pracy Codexa w pauzie; sprawdzę w następnym obiegu"; return; }
  [[ $REPAUSE_WIN == "$PAUSE_WINDOW" ]] || REPAUSE_WIN=$PAUSE_WINDOW REPAUSES=0
  if (( REPAUSES >= REPAUSE_MAX )); then
    notify "wokemax-$PAUSE_WINDOW" "Codex znowu pracuje w pauzie (5 h ${F_LEFT}%) mimo ${REPAUSE_MAX} ponownych pauz; więcej nie wysyłam, sprawdź go."
    return
  fi
  until=""; num "$PAUSE_WINDOW" && until=" Limit 5 h odnowi się ok. $(date -u -d "@$PAUSE_WINDOW" +%H:%M) UTC."
  if send_to "$CODEX" codex "LIMIT-5 nadal obowiązuje (od Claude, automatycznie): limit 5 h ok. ${F_LEFT}%.${until} Zatrzymaj się teraz: nie zaczynaj nic nowego, zrób commit tego, co już zmieniłeś (lokalnie), i czekaj na «$CP wznów». Wiadomości otrzymane w pauzie wykonaj dopiero po wznowieniu. Nie odpisuj."; then
    REPAUSES=$((REPAUSES + 1))
    STATE=PAUSE_REQUESTED PAUSE_SENT_AT=$(now) IDLE_STREAK=0 WAKE_STREAK=0; save
    log "Codex ruszył w pauzie (5 h ${F_LEFT}%); ponowna pauza ${REPAUSES}/${REPAUSE_MAX}"
    notify "woke-$PAUSE_WINDOW" "Codex ruszył w pauzie przy limicie 5 h ${F_LEFT}%; wysłałem mu ponowną pauzę. Nie pisz do Codexa przed «$CP wznów»."
  fi
}

load; save
log "start (stan z pliku: $STATE; progi: 5 h <= ${LOW}%, tydzień <= ${WEEK_LOW}%)$([[ -n $REPORT_TO ]] && echo "; zgłoszenia Codexa: $REPORT_TO")$([[ -f $RESET_ARM ]] && echo "; zgoda na reset dla tygodnia $(arm_week), próg <= ${RESET_AT}%")"
[[ -e $DIR/stop ]] && { rm -f "$DIR/stop"; log "usunięto stary plik stop"; }
END=$(( $(now) + MAX_HOURS * 3600 )); IDLE=0; UNKNOWN_SINCE=0; IDLE_STREAK=0; POLLS=0
WAKE_STREAK=0 RESUME_FAIL_SINCE=0 MAXH_LOGGED=0
CAP_STREAK=0 CAP_TRIES=0 CAP_LAST=0 CAP_SINCE=0 BUSY_STREAK=0 CAP_FAIL_SINCE=0   # v4.7/v4.8: tylko w pamięci, plik stanu bez zmian

while :; do
  [[ -e $DIR/stop ]] && { log "koniec: plik stop"; break; }
  if (( $(now) >= END )); then
    # v4.4: koniec po MAX_HOURS tylko przy pracy. W pauzie czekamy na wznów (pauza trwa najwyżej ok. 5 h),
    # a po niepotwierdzonym wznów kończymy z jednym zgłoszeniem.
    if [[ $STATE == RUNNING ]]; then log "koniec: minęło ${MAX_HOURS} h"; break; fi
    if [[ $STATE == RESUME_REQUESTED ]] && (( $(now) - RESUMED_AT >= RESUME_CONFIRM_SEC )); then
      notify "maxh-$RESUMED_AT" "minęło ${MAX_HOURS} h działania strażnika, a Codex nie potwierdził «$CP wznów»; strażnik kończy pracę."
      log "koniec: minęło ${MAX_HOURS} h, wznów niepotwierdzone"; break
    fi
    (( MAXH_LOGGED )) || { MAXH_LOGGED=1; log "minęło ${MAX_HOURS} h w stanie $STATE; zostaję do wznowienia"; }
  fi
  (( MAX_POLLS > 0 && POLLS >= MAX_POLLS )) && { log "koniec: limit obiegów testu"; break; }
  POLLS=$((POLLS + 1))
  CODEX_PANE=$(resolve "$CODEX") || CODEX_PANE=""
  if [[ -z $CODEX_PANE ]] || ! pane_ok "$CODEX_PANE" codex; then
    if [[ $CODEX == %* ]]; then   # stały numer panelu nigdy nie wraca
      log "koniec: panel Codexa $CODEX nie istnieje, nie jest sesją Codexa albo Codex w nim nie działa"
      notify codex-pane "panel Codexa $CODEX zniknął albo Codex w nim nie działa; strażnik kończy pracę."
      break
    fi
    # Nazwa: sesja wraca po restarcie pod innym panelem. Czekamy; poza pauzą to bezczynność.
    (( GONE_SINCE == 0 )) && { GONE_SINCE=$(now); log "Codex ($CODEX) nie działa; czekam, aż wróci"; }
    if [[ $STATE == RUNNING ]]; then
      IDLE=$((IDLE + POLL))
      if (( IDLE >= IDLE_SEC )); then
        log "koniec: Codex ($CODEX) nie działa od ${IDLE} s"
        notify "codex-gone-$GONE_SINCE" "Codex ($CODEX) nie działa od $((IDLE / 60)) min; strażnik kończy pracę."
        break
      fi
    fi
    sleep "$POLL"; continue
  fi
  (( GONE_SINCE )) && { log "Codex ($CODEX) znowu działa, panel $CODEX_PANE"; GONE_SINCE=0; }

  probe
  if (( COMPLETE )); then
    UNKNOWN_SINCE=0
  else
    (( UNKNOWN_SINCE == 0 )) && UNKNOWN_SINCE=$(now)
    log "limity nieczytelne lub niepełne (${PREASON:-?}): 5 h ${F_LEFT}, tydzień ${W_LEFT}"
    (( $(now) - UNKNOWN_SINCE >= UNKNOWN_ALERT_SEC )) && notify "unknown" "nie mam pełnego odczytu limitów Codexa (${PREASON:-?}); nie wznawiam, pauzuję tylko przy znanym niskim limicie."
  fi
  SCR=$(screen_state)
  # v4.7: koniec epizodu capacity dopiero po dwóch obiegach z paskiem pracy (krótki start tury, który znów pada, się nie liczy).
  if [[ $SCR == busy ]]; then BUSY_STREAK=$((BUSY_STREAK + 1)); else BUSY_STREAK=0; fi
  [[ $SCR == cap ]] || CAP_STREAK=0
  if (( CAP_TRIES && BUSY_STREAK >= 2 )); then log "Codex pracuje po błędzie capacity (prób: ${CAP_TRIES})"; CAP_TRIES=0 CAP_LAST=0 CAP_SINCE=0; fi
  [[ $SCR == cap ]] || CAP_FAIL_SINCE=0   # v4.8: zgłoszenie o nieudanej wysyłce dotyczy jednego ciągłego postoju
  expire_arm

  # Limit tygodniowy ma pierwszeństwo.
  # Z plikiem reset.armed: bez pauzy tygodniowej; przy <= RESET_AT jeden reset i praca dalej.
  if num "$W_LEFT" && (( W_LEFT <= WEEK_LOW )) && armed; then
    if (( W_LEFT <= RESET_AT )); then
      try_reset && after_reset
      sleep "$POLL"; continue
    fi
    notify "armed-$W_RESET" "limit tygodniowy Codexa ${W_LEFT}%; reset uzbrojony, więc bez pauzy tygodniowej. Zużyję jeden reset przy <= ${RESET_AT}%."
  elif num "$W_LEFT" && (( W_LEFT <= WEEK_LOW )); then
    # Bez zgody na reset: pauza, trwała blokada, koniec. Wznowienie tylko ręczne.
    if [[ $WEEK_SENT == "${W_RESET}" ]] || send_to "$CODEX" codex "LIMIT-5 (od Claude, automatycznie): limit TYGODNIOWY Codexa ok. ${W_LEFT}%. Zatrzymaj się teraz i nie zaczynaj nic nowego. Najpierw zrób commit tego, co już zmieniłeś (lokalnie wystarczy, także niedokończone), potem ${TO_CLAUDE} linię «$CP pauza: co zrobione, co zostało» i czekaj. Wznowienie tylko ręcznie po odnowieniu limitu tygodniowego."; then
      WEEK_SENT=$W_RESET STATE=WEEKLY_STOP; save
      notify "week-$W_RESET" "limit tygodniowy Codexa ${W_LEFT}%, Codex dostał LIMIT-5. Strażnik kończy pracę; wznowienie ręczne, potem usuń $STATE_FILE."
      log "koniec: limit tygodniowy ${W_LEFT}%"; break
    fi
    notify "weekfail-$W_RESET" "limit tygodniowy Codexa ${W_LEFT}%, ale NIE udało się wysłać mu LIMIT-5; ponawiam co obieg."
    sleep "$POLL"; continue
  fi

  case $STATE in
    RUNNING)
      if pause_if_low; then
        :
      elif [[ $SCR == cap ]]; then   # v4.7: postój na błędzie to nie bezczynność
        cap_resume
      elif [[ $SCR == busy || $SCR == work ]]; then   # ruch w panelu też odsuwa koniec z bezczynności
        IDLE=0
      elif [[ $SCR == idle ]]; then
        IDLE=$((IDLE + POLL))
        (( IDLE >= IDLE_SEC )) && { log "koniec: Codex bezczynny ${IDLE} s (Codex nie jest zatrzymywany)"; break; }
      fi ;;   # error: bez zmian licznika
    PAUSE_REQUESTED)
      if resume_ok; then   # v4.3: okno odnowione, zanim pauza się potwierdziła → zawsze wznów
        send_resume
      elif [[ $SCR == busy ]]; then
        IDLE_STREAK=0
        (( $(now) - PAUSE_SENT_AT >= PAUSE_CONFIRM_SEC )) && notify "pause-$PAUSE_WINDOW" "Codex dalej pracuje ${PAUSE_CONFIRM_MIN} min po LIMIT-5; nie ponawiam, sprawdź go."
      elif [[ $SCR == idle || $SCR == work || $SCR == cap ]]; then   # v4.3: sam ruch w panelu (bez paska pracy) to nie praca; v4.7: błąd też
        IDLE_STREAK=$((IDLE_STREAK + 1))
        (( IDLE_STREAK >= 2 )) && { STATE=PAUSED_5H; save; log "pauza potwierdzona: Codex bezczynny"; }
      else
        IDLE_STREAK=0; log "ekran Codexa nieczytelny; pauza niepotwierdzona"
      fi ;;
    PAUSED_5H)
      due=0
      if num "$PAUSE_WINDOW"; then (( $(now) >= PAUSE_WINDOW + RESUME_DELAY )) && due=1
      else due=1; fi   # reset nieznany: decyduje wyłącznie świeży, pełny odczyt
      if resume_ok; then   # v4.3: wznów także przy pasku pracy (Codex obudzony wiadomością stanąłby po turze)
        send_resume
      elif [[ $SCR == busy ]]; then
        woke_in_pause
      elif (( due )) && num "$PAUSE_WINDOW" && (( $(now) >= PAUSE_WINDOW + RESUME_DELAY + 1800 )); then
        notify "resume-$PAUSE_WINDOW" "po resecie limit nadal za niski albo nieznany (5 h ${F_LEFT}, tydzień ${W_LEFT}, dozwolone ${ALLOWED}); nie wznawiam."
      fi
      [[ $SCR == busy ]] || WAKE_STREAK=0 ;;
    RESUME_REQUESTED)
      if pause_if_low; then   # v4.4: ochrona LIMIT-5 nie czeka na potwierdzenie wznowienia paskiem
        :
      elif [[ $SCR == busy ]]; then   # v4.3: potwierdza pasek pracy, nie sam ruch w panelu
        STATE=RUNNING IDLE=0; save; log "wznowienie potwierdzone: Codex pracuje"
      elif [[ $SCR == cap ]]; then   # v4.7: po wznów od razu błąd capacity
        cap_resume
      elif (( $(now) - RESUMED_AT >= RESUME_CONFIRM_SEC )); then
        # Bez ponawiania i bez wyjścia z powodu bezczynności: strażnik zostaje na posterunku,
        # aż Codex ruszy (np. po ręcznym wznowieniu), albo do MAX_HOURS / pliku stop.
        notify "resumed-$PAUSE_WINDOW" "wysłałem $CP wznów, ale Codex nie ruszył od ${RESUME_CONFIRM_MIN} min; nie ponawiam, czekam aż ruszy."
      fi ;;
  esac
  sleep "$POLL"
done
