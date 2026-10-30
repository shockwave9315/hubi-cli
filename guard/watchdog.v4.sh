#!/usr/bin/env bash
# Strażnik limitu Codexa. Bash + tmux, bez tur modelu.
# v3.2: reset limitu (Weekly + 5 h) zużywa WYŁĄCZNIE za zgodą właściciela: plik reset.armed,
# najwyżej jeden reset na plik; po użyciu plik jest przenoszony do reset.used.<czas>.
# Limity czyta limits.py (codex app-server, tylko account/rateLimits/read).
# v4: sesje wskazuje się nazwą Hubi (codex/praca, claude/praca, claude/hubi-cli/2). Panel ustala
# `hubi6 pane` przy każdym obiegu i każdej wysyłce, więc restart sesji niczego nie psuje.
# Instrukcja: README.md w tym katalogu.
#
#   watchdog.sh <CP> <Codex> <Claude>     np. watchdog.sh CP17 codex/praca claude/praca
#   (dawny sposób, stałe numery paneli: watchdog.sh CP17 %5 %8; po restarcie sesji przestaje działać)
set -uo pipefail

# --- ustawienia (można zmieniać) ---
LOW=${WD_LOW:-5}                     # LIMIT-5, gdy zostało <= tyle % (5 h lub tydzień)
RESUME_MIN=${WD_RESUME_MIN:-20}      # wznów dopiero, gdy limit 5 h ma >= tyle %
IDLE_MIN=${WD_IDLE_MIN:-60}          # koniec po tylu minutach bezczynności (nie w pauzie)
MAX_HOURS=${WD_MAX_HOURS:-24}        # maksymalny czas działania
POLL=${WD_POLL:-120}                 # co ile sekund sprawdza limity i panel
RESUME_DELAY=${WD_RESUME_DELAY:-120} # sekundy po godzinie resetu, zanim spróbuje wznowić
UNKNOWN_ALERT_MIN=${WD_UNKNOWN_ALERT_MIN:-15}   # zgłoś Claude, gdy limity nieczytelne tak długo
PAUSE_CONFIRM_MIN=${WD_PAUSE_CONFIRM_MIN:-30}   # zgłoś, gdy Codex dalej pracuje po LIMIT-5
RESUME_CONFIRM_MIN=${WD_RESUME_CONFIRM_MIN:-10} # zgłoś, gdy Codex nie ruszył po "wznów"
ACTIVE_SEC=${WD_ACTIVE_SEC:-30}      # wyjście w panelu w ciągu tylu sekund = Codex pracuje
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
[[ $CP =~ ^CP[0-9]+[a-z]?$ ]] && spec_ok "$CODEX" codex && spec_ok "$CLAUDE" claude || usage
key_of() { local k=${1#%}; echo "${k//\//-}"; }   # %5 → 5 (jak dawniej), codex/praca → codex-praca
# Jak Codex ma pisać do Claude: po nazwie przez hubi send, a dawniej wklejaniem do stałego panelu.
if [[ $CLAUDE == %* ]]; then
  TO_CLAUDE="wklej do ${CLAUDE}"; CLAUDE_ADDR="Mój panel to ${CLAUDE}: tam wklejaj"
else
  TO_CLAUDE="wyślij poleceniem «hubi send ${CLAUDE//\// } -m TEKST»"; CLAUDE_ADDR="Do mnie pisz poleceniem «hubi send ${CLAUDE//\// } -m TEKST»:"
fi
mkdir -p "$DIR" || exit 1
LOG=$DIR/log
STATE_FILE=$DIR/state.$(key_of "$CODEX")

now() { date +%s; }
log() { echo "$(date -u +%FT%TZ) $CP $CODEX $STATE: $*" >> "$LOG"; }
num() { [[ $1 =~ ^[0-9]+$ ]]; }

CODEX_PANE="" GONE_SINCE=0
STATE=RUNNING PAUSE_WINDOW="" PAUSE_SENT_AT=0 RESUMED_AT=0 WEEK_SENT="" NOTIFIED="" RESET_KEY="" RESET_TRIES=0 CREDITS=""

# Jedna instancja na panel Codexa.
exec 9>"$DIR/lock.$(key_of "$CODEX")"
flock -n 9 || { echo "strażnik dla $CODEX już działa" >&2; exit 1; }
cleanup() { rm -f "$DIR"/msg.$$.*; }
trap 'log "koniec: sygnał"; exit 0' INT TERM
trap cleanup EXIT

save() {
  local tmp="$STATE_FILE.$$"
  printf 'CP=%s\nSTATE=%s\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=%s\nWEEK_SENT=%s\nNOTIFIED=%s\nRESET_KEY=%s\nRESET_TRIES=%s\n' \
    "$CP" "$STATE" "$PAUSE_WINDOW" "$PAUSE_SENT_AT" "$RESUMED_AT" "$WEEK_SENT" "$NOTIFIED" "$RESET_KEY" "$RESET_TRIES" > "$tmp" && mv "$tmp" "$STATE_FILE"
}
load() { # tylko znane klucze i bezpieczne wartości; nic nie jest wykonywane
  [[ -f $STATE_FILE ]] || return 0
  local k v saved_cp="" st="" pw="" ps="" ra="" ws="" nt="" rk="" rt=""
  while IFS='=' read -r k v; do
    [[ $v =~ ^[A-Za-z0-9_,-]*$ ]] || continue
    case $k in CP) saved_cp=$v;; STATE) st=$v;; PAUSE_WINDOW) pw=$v;; PAUSE_SENT_AT) ps=$v;;
      RESUMED_AT) ra=$v;; WEEK_SENT) ws=$v;; NOTIFIED) nt=$v;; RESET_KEY) rk=$v;; RESET_TRIES) rt=$v;; esac
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
screen_state() { # work | idle | error. "idle" wymaga dwóch sygnałów: brak "Working (" i brak wyjścia
                 # przez ACTIVE_SEC; błąd odczytu nigdy nie znaczy "bezczynny"
  local out age
  if [[ -n ${WD_SCREEN_FILE:-} ]]; then out=$(cat "$WD_SCREEN_FILE" 2>/dev/null) || { echo error; return; }
  else out=$(tmux -S "$S" capture-pane -p -t "$CODEX_PANE" 2>/dev/null) || { echo error; return; }; fi
  [[ -n ${out//[[:space:]]/} ]] || { echo error; return; }
  if tail -15 <<< "$out" | grep -q 'Working ('; then echo work; return; fi
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
  if send_to "$CLAUDE" claude "STRAŻNIK $CP: $2"; then
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
week_ok() { (( W_LEFT > LOW )) || { armed && (( W_LEFT > RESET_AT )); }; }
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
  if [[ $prev == RUNNING && $SCR == work ]]; then
    STATE=RUNNING
  elif send_to "$CODEX" codex "$CP: limity Codexa odnowione resetem (od Claude, automatycznie; 5 h ${F_LEFT}%, tydzień ${W_LEFT}%). Kontynuuj pracę od miejsca, w którym przerwałeś. Zasada LIMIT-5 bez zmian."; then
    STATE=RESUME_REQUESTED RESUMED_AT=$(now) PAUSE_WINDOW=""
  else
    STATE=PAUSED_5H PAUSE_WINDOW=""   # zwykłe wznowienie wyśle "wznów" w kolejnym obiegu
  fi
  IDLE=0 IDLE_STREAK=0; save
  notify "reset-$(now)" "zużyłem JEDEN reset limitu Codexa (zgoda właściciela, plik reset.armed). Teraz 5 h ${F_LEFT}%, tydzień ${W_LEFT}%. Pozostałe resety: ${CREDITS:-?}; kolejny tylko po nowej zgodzie."
}

load; save
log "start (stan z pliku: $STATE)$([[ -f $RESET_ARM ]] && echo "; zgoda na reset dla tygodnia $(arm_week), próg <= ${RESET_AT}%")"
[[ -e $DIR/stop ]] && { rm -f "$DIR/stop"; log "usunięto stary plik stop"; }
END=$(( $(now) + MAX_HOURS * 3600 )); IDLE=0; UNKNOWN_SINCE=0; IDLE_STREAK=0; POLLS=0

while :; do
  [[ -e $DIR/stop ]] && { log "koniec: plik stop"; break; }
  (( $(now) >= END )) && { log "koniec: minęło ${MAX_HOURS} h"; break; }
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
  expire_arm

  # Limit tygodniowy ma pierwszeństwo.
  # Z plikiem reset.armed: bez pauzy tygodniowej; przy <= RESET_AT jeden reset i praca dalej.
  if num "$W_LEFT" && (( W_LEFT <= LOW )) && armed; then
    if (( W_LEFT <= RESET_AT )); then
      try_reset && after_reset
      sleep "$POLL"; continue
    fi
    notify "armed-$W_RESET" "limit tygodniowy Codexa ${W_LEFT}%; reset uzbrojony, więc bez pauzy tygodniowej. Zużyję jeden reset przy <= ${RESET_AT}%."
  elif num "$W_LEFT" && (( W_LEFT <= LOW )); then
    # Bez zgody na reset: pauza, trwała blokada, koniec. Wznowienie tylko ręczne.
    if [[ $WEEK_SENT == "${W_RESET}" ]] || send_to "$CODEX" codex "LIMIT-5 (od Claude, automatycznie): limit TYGODNIOWY Codexa ok. ${W_LEFT}%. Dokończ bieżące zadanie, zrób commit, ${TO_CLAUDE} linię «$CP pauza: stan» i czekaj. Wznowienie tylko ręcznie po odnowieniu limitu tygodniowego."; then
      WEEK_SENT=$W_RESET STATE=WEEKLY_STOP; save
      notify "week-$W_RESET" "limit tygodniowy Codexa ${W_LEFT}%, Codex dostał LIMIT-5. Strażnik kończy pracę; wznowienie ręczne, potem usuń $STATE_FILE."
      log "koniec: limit tygodniowy ${W_LEFT}%"; break
    fi
    notify "weekfail-$W_RESET" "limit tygodniowy Codexa ${W_LEFT}%, ale NIE udało się wysłać mu LIMIT-5; ponawiam co obieg."
    sleep "$POLL"; continue
  fi

  case $STATE in
    RUNNING)
      if num "$F_LEFT" && (( F_LEFT <= LOW )) && [[ $PAUSE_WINDOW != "${F_RESET}" ]]; then
        if send_to "$CODEX" codex "LIMIT-5 (od Claude, automatycznie): zostało ok. ${F_LEFT}% limitu 5 h. Dokończ bieżące zadanie, zrób commit (lokalnie wystarczy, push jeśli zdążysz), ${TO_CLAUDE} linię «$CP pauza: co zrobione, co dalej» i czekaj na «$CP wznów»."; then
          STATE=PAUSE_REQUESTED PAUSE_WINDOW=$F_RESET PAUSE_SENT_AT=$(now) IDLE_STREAK=0; save
          log "LIMIT-5 wysłany przy ${F_LEFT}%, reset $F_RESET"
        else
          notify "sendfail-$F_RESET" "limit 5 h Codexa ${F_LEFT}%, ale NIE udało się wysłać mu LIMIT-5; ponawiam co obieg."
        fi
      elif [[ $SCR == work ]]; then
        IDLE=0
      elif [[ $SCR == idle ]]; then
        IDLE=$((IDLE + POLL))
        (( IDLE >= IDLE_SEC )) && { log "koniec: Codex bezczynny ${IDLE} s (Codex nie jest zatrzymywany)"; break; }
      fi ;;   # error: bez zmian licznika
    PAUSE_REQUESTED)
      if [[ $SCR == work ]]; then
        IDLE_STREAK=0
        if num "$F_RESET" && [[ $F_RESET != "$PAUSE_WINDOW" ]] && num "$F_LEFT" && (( F_LEFT >= RESUME_MIN )); then
          STATE=RUNNING; save; log "okno 5 h odnowione, a Codex nadal pracuje; bez wznów"
        elif (( $(now) - PAUSE_SENT_AT >= PAUSE_CONFIRM_SEC )); then
          notify "pause-$PAUSE_WINDOW" "Codex dalej pracuje ${PAUSE_CONFIRM_MIN} min po LIMIT-5; nie ponawiam, sprawdź go."
        fi
      elif [[ $SCR == idle ]]; then
        IDLE_STREAK=$((IDLE_STREAK + 1))
        (( IDLE_STREAK >= 2 )) && { STATE=PAUSED_5H; save; log "pauza potwierdzona: Codex bezczynny"; }
      else
        IDLE_STREAK=0; log "ekran Codexa nieczytelny; pauza niepotwierdzona"
      fi ;;
    PAUSED_5H)
      due=0
      if num "$PAUSE_WINDOW"; then (( $(now) >= PAUSE_WINDOW + RESUME_DELAY )) && due=1
      else due=1; fi   # reset nieznany: decyduje wyłącznie świeży, pełny odczyt poniżej
      if (( due && COMPLETE )) && (( F_LEFT >= RESUME_MIN )) && week_ok && [[ $ALLOWED == true ]]; then
        if [[ $SCR == work ]]; then
          STATE=RUNNING IDLE=0; save; log "Codex już pracuje po resecie; bez wznów"
        elif [[ $SCR == idle ]]; then
          if send_to "$CODEX" codex "$CP wznów (od Claude, automatycznie po odnowieniu limitu: 5 h ${F_LEFT}%, tydzień ${W_LEFT}%). ${CLAUDE_ADDR} zgłoszenia, STOP i linie gotowości. Kontynuuj od ostatniej pauzy; zasada LIMIT-5 bez zmian."; then
            STATE=RESUME_REQUESTED RESUMED_AT=$(now); save; log "wznów wysłany (5 h ${F_LEFT}%, tydzień ${W_LEFT}%)"
          fi
        else
          log "ekran Codexa nieczytelny; wznowienie wstrzymane"
        fi
      elif (( due )) && num "$PAUSE_WINDOW" && (( $(now) >= PAUSE_WINDOW + RESUME_DELAY + 1800 )); then
        notify "resume-$PAUSE_WINDOW" "po resecie limit nadal za niski albo nieznany (5 h ${F_LEFT}, tydzień ${W_LEFT}, dozwolone ${ALLOWED}); nie wznawiam."
      fi ;;
    RESUME_REQUESTED)
      if [[ $SCR == work ]]; then
        STATE=RUNNING IDLE=0; save; log "wznowienie potwierdzone: Codex pracuje"
      elif (( $(now) - RESUMED_AT >= RESUME_CONFIRM_SEC )); then
        # Bez ponawiania i bez wyjścia z powodu bezczynności: strażnik zostaje na posterunku,
        # aż Codex ruszy (np. po ręcznym wznowieniu), albo do MAX_HOURS / pliku stop.
        notify "resumed-$PAUSE_WINDOW" "wysłałem $CP wznów, ale Codex nie ruszył od ${RESUME_CONFIRM_MIN} min; nie ponawiam, czekam aż ruszy."
      fi ;;
  esac
  sleep "$POLL"
done
