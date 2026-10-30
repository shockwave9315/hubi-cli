#!/usr/bin/env bash
# Testy strażnika na atrapach. Własny serwer tmux i własne katalogi;
# nic nie trafia do prawdziwych paneli Codexa ani Claude.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WD=${WD_UNDER_TEST:-$HERE/../watchdog.sh}
LIM=$HERE/../limits.py
T=$(mktemp -d "${TMPDIR:-/tmp}/wdtest.XXXXXX")
SOCK=$T/tmux.sock
PASS=0 FAIL=0
# Testy sprzed v4.5 zakładają dawny wspólny próg 5% (5 h i tydzień). Nowe testy T63+ ustawiają progi same (rundef = domyślne).
export WD_LOW=5 WD_WEEK_LOW=5
ok()  { PASS=$((PASS+1)); echo "PASS $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL $*"; }
check() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
checknot() { local name=$1; shift; if "$@"; then bad "$name"; else ok "$name"; fi; }

# --- testowy tmux: sesje z "-codex-" i "-claude-" w nazwie, panele zapisują wejście do plików
tmux -S "$SOCK" new-session -d -s "t-codex-1" "exec cat > $T/codex.in"
tmux -S "$SOCK" new-session -d -s "t-claude-1" "exec cat > $T/claude.in"
tmux -S "$SOCK" new-session -d -s "t-other-1" "exec cat > /dev/null"
CODEX=$(tmux -S "$SOCK" display-message -p -t t-codex-1 '#{pane_id}')
CLAUDE=$(tmux -S "$SOCK" display-message -p -t t-claude-1 '#{pane_id}')
OTHER=$(tmux -S "$SOCK" display-message -p -t t-other-1 '#{pane_id}')
trap 'pkill -f "$T" 2>/dev/null; tmux -S "$SOCK" kill-server 2>/dev/null; [ -n "${WD_TEST_KEEP:-}" ] || rm -rf "$T"' EXIT

# --- atrapa odczytu limitów: każdy obieg zdejmuje jedną linię "limity|work|idle"
cat > "$T/fake_limits.sh" <<'EOF'
#!/usr/bin/env bash
d=$1; seq=$d/seq; last=$d/last
if [[ -s $seq ]]; then line=$(head -1 "$seq"); sed -i 1d "$seq"; echo "$line" > "$last"; else line=$(cat "$last"); fi
lim=${line%%|*}; scr=${line##*|}
case $scr in work) echo "◦ Working (5s • esc to interrupt)" > "$d/screen";; err) rm -f "$d/screen";;
  comp) printf '%s\n' "• Compacting context (54s • esc to interrupt)" "  └ Making room to continue." "" "› Ask Codex to do anything" > "$d/screen";;
  quote) printf '%s\n' "• Strażnik sprawdza napis Working ( na dole ekranu." "" "› Ask Codex to do anything" > "$d/screen";;
  cap) printf '%s\n' "• Edited 2 files (+46 -15)" "" "■ Selected model is at capacity. Please try a different model." "" "› Ask Codex to do anything" "" "  GPT-6.1-Sol xhigh · ~/repos/praca" "  ? for shortcuts" > "$d/screen";;
  capwrap) printf '%s\n' "• Ran git status" "" "■ Selected model is at capacity. Please try a" "  different model." "" "─ Worked for 3m ─────────" "" "› CP17 szkic" > "$d/screen";;
  capold) printf '%s\n' "■ Selected model is at capacity. Please try a different model." "" "› CP17 wznów po błędzie (od Claude, automatycznie)" "" "• Gotowe." "" "› Ask Codex to do anything" > "$d/screen";;
  capquote) printf '%s\n' "• Strażnik szuka linii:" "  ■ Selected model is at capacity. Please try a different model." "" "› Ask Codex to do anything" > "$d/screen";;
  capbar) printf '%s\n' "■ Selected model is at capacity. Please try a different model." "" "• Working (3s • esc to interrupt)" "" "› Ask Codex to do anything" > "$d/screen";;
  capwrap0) printf '%s\n' "• Edited x" "" "■ Selected model is at capacity. Please try a" "different model." "" "› Ask Codex to do anything" "" "  gpt-x xhigh · ~/repos/praca" "  ← for agents · ? for shortcuts         ⚠ 2 warnings · f2 to view" > "$d/screen";;
  capnarrow) printf '%s\n' "› go" "" "■ Selected model is at" "capacity. Please try a" "different model." "" "› Ask Codex to do" "  anything" "" "  gpt-x xhigh" > "$d/screen";;
  capwarn) printf '%s\n' "■ Selected model is at capacity. Please try a different model." "" "⚠ Heads up, you have less than 25% of your" "  5h limit left." "" "› Ask Codex to do anything" "" "  gpt-x xhigh" > "$d/screen";;
  cap400) printf '%s\n' "› test" "" "■ {\"type\":\"error\",\"status\":400,\"error\":" "{\"type\":\"invalid_request_error\",\"message\":\"x\"}}" "" "› Ask Codex to do anything" "" "  gpt-x xhigh" > "$d/screen";;
  warnonly) printf '%s\n' "• Gotowe." "" "⚠ Heads up, you have less than 25% of your" "  5h limit left." "" "› Ask Codex to do anything" "" "  gpt-x xhigh" > "$d/screen";;
  capov) printf '%s\n' "■ Selected model is at capacity. Please try a different model." "" "› CP17 wznów po błędzie (od Claude, automatycznie): poprzednia tura przerwała" "" "• Ran flutter test" "  └ All tests passed!" "" "• Edited lib/x.dart (+3 -1)" > "$d/screen";;
  *) echo "› Ask Codex to do anything" > "$d/screen";; esac
echo "$lim"
EOF
chmod +x "$T/fake_limits.sh"

# scen <nazwa> <max obiegów> [zmienne...] — sekwencja w $D/seq (przygotowana wcześniej)
new() { D=$T/$1; mkdir -p "$D"; : > "$D/seq"; }
add() { printf '%s\n' "$1" >> "$D/seq"; }
run() { local polls=$1; shift
  env WD_DIR="$D" WD_TMUX_SOCK="$SOCK" WD_DRYRUN=1 WD_POLL=1 WD_RESUME_DELAY=1 WD_CODEX_ARGS_RE='^cat( |$)' WD_CLAUDE_ARGS_RE='^cat( |$)' \
      WD_MAX_POLLS="$polls" WD_SCREEN_FILE="$D/screen" WD_LIMITS_CMD="$T/fake_limits.sh $D" "$@" \
      bash "$WD" CP17 "$CODEX" "$CLAUDE"; }
tocodex() { awk -F'\t' -v p="$CODEX" '$2==p' "$D/dryrun.out" 2>/dev/null | grep -c -- "$1"; }
toclaude() { awk -F'\t' -v p="$CLAUDE" '$2==p' "$D/dryrun.out" 2>/dev/null | grep -c -- "$1"; }
N=$(date +%s)
okl() { echo "status=ok five_left=$1 five_reset=$2 week_left=$3 week_reset=$((N+500000)) allowed=${4:-true}"; }

echo "== T1 limits.py: formaty odpowiedzi app-servera"
p() { echo "$1" > "$T/r.json"; python3 -I "$LIM" --parse "$T/r.json"; }
W5='{"usedPercent":53,"windowDurationMins":300,"resetsAt":1792534299}'
WW='{"usedPercent":38,"windowDurationMins":10080,"resetsAt":1793084921}'
check "T1 komplet 5h+tydzień" [ "$(p "{\"rateLimitsByLimitId\":{\"codex\":{\"primary\":$W5,\"secondary\":$WW}},\"rateLimits\":{}}")" = "status=ok five_left=47 five_reset=1792534299 week_left=62 week_reset=1793084921 allowed=unknown" ]
check "T1 tylko tydzień → 5h unknown" grep -q 'five_left=unknown five_reset=unknown week_left=62' <(p "{\"rateLimits\":{\"limitId\":\"codex\",\"primary\":$WW,\"secondary\":null}}")
check "T1 odwrócona kolejność okien" grep -q 'five_left=47 .* week_left=62' <(p "{\"rateLimitsByLimitId\":{\"codex\":{\"primary\":$WW,\"secondary\":$W5}}}")
check "T1 primary=null" grep -q 'five_left=unknown' <(p "{\"rateLimitsByLimitId\":{\"codex\":{\"primary\":null,\"secondary\":$WW}}}")
check "T1 resetsAt=null" grep -q 'five_left=47 five_reset=unknown' <(p '{"rateLimitsByLimitId":{"codex":{"primary":{"usedPercent":53,"windowDurationMins":300,"resetsAt":null}}}}')
check "T1 usedPercent spoza zakresu" grep -q 'five_left=unknown' <(p '{"rateLimitsByLimitId":{"codex":{"primary":{"usedPercent":150,"windowDurationMins":300,"resetsAt":5}}}}')
check "T1 inny limitId → błąd" grep -q 'status=error reason=no-codex-limit' <(p "{\"rateLimitsByLimitId\":{\"other\":{\"primary\":$W5}},\"rateLimits\":{\"limitId\":\"other\",\"primary\":$W5}}")
check "T1 zły JSON" grep -q 'status=error reason=bad-json' <(p '{oops')
check "T1 0% i 100% used" grep -q 'five_left=100 .* week_left=0' <(p '{"rateLimitsByLimitId":{"codex":{"primary":{"usedPercent":0,"windowDurationMins":300,"resetsAt":9},"secondary":{"usedPercent":100,"windowDurationMins":10080,"resetsAt":9}}}}')
check "T1 dwa okna 300 → niejednoznaczne" grep -q 'five_left=unknown' <(p "{\"rateLimitsByLimitId\":{\"codex\":{\"primary\":$W5,\"secondary\":$W5}}}")
mkdir -p "$T/eof" "$T/hang"
printf '#!/bin/sh\nexit 0\n' > "$T/eof/codex"; printf '#!/bin/sh\nexec sleep 30\n' > "$T/hang/codex"; chmod +x "$T/eof/codex" "$T/hang/codex"
check "T1 EOF app-servera → błąd" grep -q 'status=error reason=init-eof' <(PATH=$T/eof:$PATH python3 -I "$LIM")
check "T1 timeout app-servera → błąd" grep -q 'status=error reason=init-timeout' <(PATH=$T/hang:$PATH WD_LIMITS_TIMEOUT=2 python3 -I "$LIM")
sleep 0.5; check "T1 brak osieroconego procesu" bash -c "! pgrep -x -f 'sleep 30' -u $(id -u) >/dev/null"

echo "== T2 5% → jeden LIMIT-5, bez wznów przed resetem"
new t2; R=$((N+100000)); add "$(okl 5 $R 60)|work"; for _ in 1 2 3 4 5; do add "$(okl 5 $R 60)|idle"; done
run 6
check "T2 jeden LIMIT-5" [ "$(tocodex 'LIMIT-5 (od')" = 1 ]
check "T2 zero wznów" [ "$(tocodex 'wznów (od')" = 0 ]

echo "== T3 reset: nie przed terminem, nie przy niskim limicie, raz po odnowieniu"
new t3; N=$(date +%s); R=$((N+4))
add "$(okl 5 $R 60)|work"; add "$(okl 5 $R 60)|idle"; add "$(okl 5 $R 60)|idle"
for _ in 1 2 3 4; do add "$(okl 3 $R 60)|idle"; done
add "$(okl 80 $((R+18000)) 60)|idle"; add "$(okl 80 $((R+18000)) 60)|work"; add "$(okl 79 $((R+18000)) 60)|work"
run 10
check "T3 jeden LIMIT-5" [ "$(tocodex 'LIMIT-5 (od')" = 1 ]
check "T3 jeden wznów" [ "$(tocodex 'wznów (od')" = 1 ]
WT=$(awk -F'\t' '/wznów \(od/ {print $1}' "$D/dryrun.out")
check "T3 wznów po resecie+opóźnieniu" [ "${WT:-0}" -ge $((R+1)) ]
checknot "T3 nie wznowił przy 3%" grep -q 'wznów wysłany (5 h 3%' "$D/log"
check "T3 wznowienie potwierdzone" grep -q 'wznowienie potwierdzone' "$D/log"

echo "== T4 tydzień 5% razem z 5 h 5% → tydzień wygrywa"
new t4; N=$(date +%s); add "$(okl 5 $((N+999)) 5)|work"
run 5
c() { [ "$(tocodex 'TYGODNIOWY')" = 1 ] && [ "$(tocodex 'LIMIT-5 (od')" = 1 ]; }; check "T4 jedna wiadomość do Codexa (tygodniowa)" c
check "T4 jedno powiadomienie Claude" [ "$(toclaude 'tygodniowy')" = 1 ]
check "T4 koniec bez wznów" grep -q 'koniec: limit tygodniowy' "$D/log"
run 3
c() { [ "$(tocodex 'TYGODNIOWY')" = 1 ] && [ "$(toclaude 'tygodniowy')" = 1 ]; }; check "T4 restart w tym samym tygodniu bez duplikatu" c

echo "== T5 brak danych → bez działań, jedno zgłoszenie"
new t5; for _ in $(seq 6); do add "status=error reason=read-timeout|work"; done
run 6 WD_UNKNOWN_ALERT_SEC=2
check "T5 zero wiadomości do Codexa" [ "$(tocodex '.')" = 0 ]
check "T5 jedno zgłoszenie do Claude" [ "$(toclaude 'nie mam pełnego')" = 1 ]
new t5b; N=$(date +%s)
printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' $((N-100)) $((N-200)) > "$D/state.${CODEX#%}"
for _ in 1 2 3 4; do add "status=ok five_left=unknown five_reset=unknown week_left=60 week_reset=1 allowed=true|idle"; done
add "status=ok five_left=90 five_reset=$((N+18000)) week_left=unknown week_reset=unknown allowed=true|idle"
add "status=ok five_left=90 five_reset=$((N+18000)) week_left=60 week_reset=1 allowed=false|idle"
run 6
check "T5 5 h/tydzień nieznany lub zablokowane → bez wznów" [ "$(tocodex 'wznów')" = 0 ]

echo "== T6 pauza niepotwierdzona → jedno zgłoszenie, bez powtórek"
new t6; N=$(date +%s); for _ in $(seq 7); do add "$(okl 4 $((N+99999)) 60)|work"; done
run 7 WD_PAUSE_CONFIRM_SEC=2
check "T6 jeden LIMIT-5" [ "$(tocodex 'LIMIT-5 (od')" = 1 ]
check "T6 jedno zgłoszenie" [ "$(toclaude 'dalej pracuje')" = 1 ]

echo "== T7 bezczynność"
new t7; for _ in $(seq 8); do add "$(okl 50 $((N+99999)) 60)|idle"; done
run 8 WD_IDLE_SEC=3
check "T7 koniec po bezczynności" grep -q 'koniec: Codex bezczynny' "$D/log"
new t7b; for _ in $(seq 6); do add "$(okl 50 $((N+99999)) 60)|work"; done
run 6 WD_IDLE_SEC=2
checknot "T7 praca nie kończy strażnika" grep -q 'bezczynny' "$D/log"
new t7c; N=$(date +%s)
printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' $((N+99999)) "$N" > "$D/state.${CODEX#%}"
for _ in $(seq 6); do add "$(okl 2 $((N+99999)) 60)|idle"; done
run 6 WD_IDLE_SEC=2
checknot "T7 pauza nie liczy się jako bezczynność" grep -q 'bezczynny' "$D/log"
new t7d; add "$(okl 50 $((N+99999)) 60)|work"; touch "$D/stop"
( sleep 3; touch "$D/stop" ) & run 30; wait
c() { grep -q 'usunięto stary plik stop' "$D/log" && grep -q 'koniec: plik stop' "$D/log"; }; check "T7 plik stop kończy (stary stop usuwany przy starcie)" c
new t7e; add "$(okl 50 $((N+99999)) 60)|work"
run 50 WD_MAX_HOURS=0
check "T7 MAX_HOURS kończy" grep -q 'koniec: minęło 0 h' "$D/log"

echo "== T8 dwie instancje"
new t8; add "$(okl 50 $((N+99999)) 60)|work"
run 6 & P1=$!; sleep 1.5
run 2 2> "$D/err2"; RC=$?
wait $P1
c() { [ $RC = 1 ] && grep -q 'już działa' "$D/err2"; }; check "T8 druga instancja odmawia" c

echo "== T9 restart w pauzie: bez duplikatów"
new t9; N=$(date +%s); R=$((N+2))
add "$(okl 5 $R 60)|work"; add "$(okl 5 $R 60)|idle"; add "$(okl 5 $R 60)|idle"
run 3
check "T9 pauza zapisana" grep -q 'STATE=PAUSED_5H' "$D/state.${CODEX#%}"
for _ in 1 2; do add "$(okl 5 $R 60)|idle"; done
run 2
check "T9 po restarcie bez drugiego LIMIT-5" [ "$(tocodex 'LIMIT-5 (od')" = 1 ]
sleep 2; for _ in 1 2 3; do add "$(okl 90 $((R+18000)) 60)|idle"; done
run 1
check "T9 jeden wznów" [ "$(tocodex 'wznów (od')" = 1 ]
run 2
check "T9 restart po wznów bez drugiego wznów" [ "$(tocodex 'wznów (od')" = 1 ]

echo "== T10 transport: znaki specjalne, bez wykonania poleceń, jeden Enter"
src=$T/send_only.sh
sed -n '/^now()/p;/^log()/p;/^num()/p;/^resolve()/,/^}/p;/^pane_ok()/,/^}/p;/^send_to()/,/^}/p' "$WD" > "$src"
MSG=$'Zażółć gęślą `touch '"$T"'/PWN1` $(touch '"$T"'/PWN2) <...> "cudzysłów" \\ koniec'
( export S=$SOCK DIR=$T DRYRUN=0 FAIL_SEND= LOG=$T/t10.log CP=CP17 STATE=T CODEX_ARGS_RE='^cat( |$)' CLAUDE_ARGS_RE='^cat( |$)'; source "$src"; send_to "$CODEX" codex "$MSG"; sleep 1; send_to "$CODEX" codex "druga" ); sleep 1
check "T10 tekst dotarł dosłownie" grep -qF 'Zażółć gęślą `touch' "$T/codex.in"
c() { [ ! -e "$T/PWN1" ] && [ ! -e "$T/PWN2" ]; }; check "T10 nic się nie wykonało" c
check "T10 dwie wiadomości = dwa Entery" [ "$(grep -c '' "$T/codex.in")" = 2 ]
c() { [ -z "$(tmux -S "$SOCK" list-buffers 2>/dev/null)" ] && [ -z "$(ls "$T"/msg.* 2>/dev/null)" ]; }; check "T10 brak resztek buforów i plików" c

echo "== T11 panele: zły rodzaj albo brak"
new t11; add "$(okl 50 $((N+99999)) 60)|work"
env WD_DIR="$D" WD_TMUX_SOCK="$SOCK" WD_DRYRUN=1 WD_POLL=1 WD_CODEX_ARGS_RE='^cat( |$)' WD_CLAUDE_ARGS_RE='^cat( |$)' WD_MAX_POLLS=3 WD_SCREEN_FILE="$D/screen" WD_LIMITS_CMD="$T/fake_limits.sh $D" bash "$WD" CP17 "$OTHER" "$CLAUDE"
check "T11 panel spoza sesji Codexa → koniec" grep -q 'nie jest sesją Codexa' "$D/log"
new t11b; add "$(okl 5 $((N+999)) 5)|work"
env WD_DIR="$D" WD_TMUX_SOCK="$SOCK" WD_DRYRUN=1 WD_POLL=1 WD_CODEX_ARGS_RE='^cat( |$)' WD_CLAUDE_ARGS_RE='^cat( |$)' WD_MAX_POLLS=3 WD_SCREEN_FILE="$D/screen" WD_LIMITS_CMD="$T/fake_limits.sh $D" bash "$WD" CP17 "$CODEX" "%999"
c() { grep -q 'nie powiadomiono Claude' "$D/log" && [ "$(toclaude '.')" = 0 ]; }; check "T11 brak panelu Claude → tylko log" c
check "T11 złe argumenty → kod 2" bash -c "bash '$WD' 'CP17;rm' %1 %2 2>/dev/null; [ \$? = 2 ]"

echo "== T12 SIGTERM: czyste wyjście, blokada zwolniona"
new t12; add "$(okl 50 $((N+99999)) 60)|work"
( exec env WD_DIR="$D" WD_TMUX_SOCK="$SOCK" WD_DRYRUN=1 WD_POLL=1 WD_CODEX_ARGS_RE='^cat( |$)' WD_CLAUDE_ARGS_RE='^cat( |$)' WD_MAX_POLLS=60 WD_SCREEN_FILE="$D/screen" WD_LIMITS_CMD="$T/fake_limits.sh $D" bash "$WD" CP17 "$CODEX" "$CLAUDE" ) & P=$!
sleep 2; kill -TERM $P; wait $P
check "T12 log sygnału" grep -q 'koniec: sygnał' "$D/log"
run 1; check "T12 blokada zwolniona" [ $? = 0 ]

echo "== T13 odbiorca: zwykła powłoka w panelu Codexa nie dostaje tekstu (review #1)"
tmux -S "$SOCK" new-session -d -s "t-codex-sh" "bash --norc -i"
SH=$(tmux -S "$SOCK" display-message -p -t t-codex-sh '#{pane_id}'); sleep 0.5
( export S=$SOCK DIR=$T DRYRUN=0 FAIL_SEND= LOG=$T/t13.log CP=CP17 STATE=T CODEX_ARGS_RE='^cat( |$)' CLAUDE_ARGS_RE='^cat( |$)'; source "$src"; send_to "$SH" codex "touch $T/PWN3" ); sleep 1
c() { [ ! -e "$T/PWN3" ] && grep -q 'NIE wysłano' "$T/t13.log"; }; check "T13 powłoka odrzucona, nic nie wykonano" c
check "T13 wiadomości bez backticków" bash -c "! grep -n 'send_to .*\\\`' '$WD'"

echo "== T14 ordinaryUsageAllowed nieznane/niepoprawne → bez wznów (review #2)"
check "T14 parser: napis \"false\" to unknown" grep -q 'allowed=unknown' <(p '{"ordinaryUsageAllowed":"false","rateLimitsByLimitId":{"codex":{"primary":{"usedPercent":1,"windowDurationMins":300,"resetsAt":9}}}}')
new t14; N=$(date +%s)
printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' $((N-100)) $((N-200)) > "$D/state.${CODEX#%}"
for _ in 1 2 3; do add "status=ok five_left=90 five_reset=$((N+18000)) week_left=60 week_reset=1 allowed=unknown|idle"; done
run 3
check "T14 allowed=unknown → bez wznów" [ "$(tocodex 'wznów (od')" = 0 ]

echo "== T15 blokada tygodniowa trwała po restarcie (review #3)"
new t15; N=$(date +%s)
printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' $((N+99999)) "$N" > "$D/state.${CODEX#%}"
add "$(okl 5 $((N+99999)) 4)|idle"
run 3
check "T15 stan WEEKLY_STOP zapisany" grep -q 'STATE=WEEKLY_STOP' "$D/state.${CODEX#%}"
add "$(okl 90 $((N+99999)) 80)|idle"
run 3 2> "$D/err15"; RC=$?
c() { [ $RC = 3 ] && grep -q 'blokada tygodniowa' "$D/err15" && [ "$(tocodex 'wznów (od')" = 0 ]; }; check "T15 restart odmawia, zero wznów" c
check "T15 inny CP też odmawia" bash -c "env WD_DIR='$D' WD_TMUX_SOCK='$SOCK' WD_DRYRUN=1 WD_POLL=1 WD_CODEX_ARGS_RE='^cat( |$)' WD_CLAUDE_ARGS_RE='^cat( |$)' WD_MAX_POLLS=1 WD_SCREEN_FILE='$D/screen' WD_LIMITS_CMD='$T/fake_limits.sh $D' bash '$WD' CP18 '$CODEX' '$CLAUDE' 2>/dev/null; [ \$? = 3 ]"

echo "== T16 nieudana wysyłka tygodniowego LIMIT-5 (review #4)"
new t16; N=$(date +%s); for _ in 1 2 3; do add "$(okl 50 $((N+99999)) 3)|work"; done
run 3 WD_FAIL_SEND=codex
c() { [ "$(toclaude 'NIE udało się')" = 1 ] && [ "$(toclaude 'dostał')" = 0 ] && ! grep -q 'koniec: limit tygodniowy' "$D/log" && ! grep -q 'WEEKLY_STOP' "$D/state.${CODEX#%}"; }
check "T16 brak fałszywego potwierdzenia, ponawia" c

echo "== T17 ekran nieczytelny ≠ bezczynny (review #5)"
new t17; N=$(date +%s); add "$(okl 5 $((N+2)) 60)|work"; for _ in 1 2 3 4; do add "$(okl 5 $((N+2)) 60)|err"; done
run 5
check "T17 pauza niepotwierdzona przy błędzie ekranu" grep -q 'STATE=PAUSE_REQUESTED' "$D/state.${CODEX#%}"
new t17b; N=$(date +%s)
printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' $((N-100)) $((N-200)) > "$D/state.${CODEX#%}"
for _ in 1 2 3; do add "$(okl 90 $((N+18000)) 60)|err"; done
run 3
check "T17 bez wznów przy błędzie ekranu" [ "$(tocodex 'wznów (od')" = 0 ]
new t17c; for _ in $(seq 5); do add "$(okl 50 $((N+99999)) 60)|err"; done
run 5 WD_IDLE_SEC=2
checknot "T17 błąd ekranu nie liczy się jako bezczynność" grep -q 'bezczynny' "$D/log"

echo "== T18 niepełne dane → alarm po czasie (review #6)"
new t18; for _ in $(seq 5); do add "status=ok five_left=unknown five_reset=unknown week_left=60 week_reset=1 allowed=true|work"; done
run 5 WD_UNKNOWN_ALERT_SEC=2
check "T18 jedno zgłoszenie o niepełnym odczycie" [ "$(toclaude 'nie mam pełnego')" = 1 ]

echo "== T19 grupa procesów app-servera (review #7)"
mkdir -p "$T/wrap"; printf '#!/bin/sh\ntrap "" TERM\nsleep 301 &\nwait\n' > "$T/wrap/codex"; chmod +x "$T/wrap/codex"
OUT=$(PATH=$T/wrap:$PATH WD_LIMITS_TIMEOUT=2 python3 -I "$LIM"); sleep 0.5
c() { [[ $OUT == *init-timeout* ]] && ! pgrep -x -f 'sleep 301' -u "$(id -u)" >/dev/null; }
check "T19 wrapper z potomkiem ignorującym TERM nie zostaje" c

echo "== T20 obcy node w panelu Codexa odrzucony, prawdziwy wrapper przyjęty (re-review #1)"
mkdir -p "$T/bin"; printf 'setInterval(()=>{},1000)\n' > "$T/bin/codex"; printf 'setInterval(()=>{},1000)\n' > "$T/repl.js"
tmux -S "$SOCK" new-session -d -s "t-codex-node" "exec node $T/repl.js"
tmux -S "$SOCK" new-session -d -s "t-codex-real" "exec node $T/bin/codex --no-daemon"
NODEP=$(tmux -S "$SOCK" display-message -p -t t-codex-node '#{pane_id}'); REALP=$(tmux -S "$SOCK" display-message -p -t t-codex-real '#{pane_id}'); sleep 0.5
chk() { ( S=$SOCK; CODEX_ARGS_RE='^(node [^ ]*/bin/codex|[^ ]*/codex)( |$)'; CLAUDE_ARGS_RE='^([^ ]*/)?claude( |$)'; source "$src"; pane_ok "$1" codex ); }
checknot "T20 obcy skrypt node odrzucony (domyślne ustawienia)" chk "$NODEP"
check "T20 node …/bin/codex przyjęty" chk "$REALP"
checknot "T20 panel Claude nie jest Codexem" chk "$CLAUDE"

echo "== T21 aktywność bez napisu Working = praca (uwaga ChatGPT #1)"
new t21; N=$(date +%s)
for _ in $(seq 6); do add "$(okl 50 $((N+99999)) 60)|idle"; done
( for _ in $(seq 12); do date +%s > "$D/screen.activity"; sleep 0.5; done ) &
run 6 WD_IDLE_SEC=2
wait
checknot "T21 świeże wyjście w panelu nie liczy się jako bezczynność" grep -q 'bezczynny' "$D/log"
new t21b; add "$(okl 5 $((N+99999)) 60)|work"; for _ in 1 2 3 4; do add "$(okl 5 $((N+99999)) 60)|idle"; done
( for _ in $(seq 12); do date +%s > "$D/screen.activity"; sleep 0.5; done ) &
run 5
wait
check "T21 v4.3: sam ruch w panelu bez paska pracy nie blokuje potwierdzenia pauzy" grep -q 'STATE=PAUSED_5H' "$D/state.${CODEX#%}"

echo "== T22 niepotwierdzone wznów: strażnik zostaje, bez ponawiania (uwaga ChatGPT #2)"
new t22; N=$(date +%s)
printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' $((N-100)) $((N-200)) > "$D/state.${CODEX#%}"
for _ in $(seq 8); do add "$(okl 90 $((N+18000)) 60)|idle"; done
run 8 WD_RESUME_CONFIRM_SEC=2 WD_IDLE_SEC=2
c() { [ "$(tocodex 'wznów (od')" = 1 ] && [ "$(toclaude 'nie ruszył')" = 1 ] && ! grep -q 'bezczynny' "$D/log" && grep -q 'STATE=RESUME_REQUESTED' "$D/state.${CODEX#%}"; }
check "T22 jedno wznów, jedno zgłoszenie, bez wyjścia" c

# --- v3.2: reset limitu tylko z plikiem reset.armed (zgoda właściciela)
cat > "$T/fake_reset.sh" <<'EOF'
#!/usr/bin/env bash
d=$1; key=$2; echo "$key" >> "$d/reset.calls"
if [[ -s $d/rseq ]]; then line=$(head -1 "$d/rseq"); sed -i 1d "$d/rseq"; else line="status=ok outcome=reset credits_before=2 five_left=100 week_left=100"; fi
echo "$line"
EOF
chmod +x "$T/fake_reset.sh"
st() { grep -q "STATE=$1" "$D/state.${CODEX#%}"; }
calls() { [ -s "$D/reset.calls" ] && wc -l < "$D/reset.calls" || echo 0; }
arm() { echo "WEEK_RESET=$((N+500000))" > "$D/reset.armed"; }   # zgoda na bieżące okno tygodniowe atrapy

echo "== T23 uzbrojony reset: tydzień 4% → bez pauzy tygodniowej, bez resetu"
new t23; N=$(date +%s); arm; for _ in 1 2 3; do add "$(okl 50 $((N+99999)) 4)|work"; done
run 3 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(tocodex 'LIMIT-5 (od')" = 0 ] && [ "$(toclaude 'reset uzbrojony')" = 1 ] && [ "$(calls)" = 0 ] && ! st WEEKLY_STOP && [ -e "$D/reset.armed" ]; }
check "T23 bez LIMIT-5, jedno info, zero resetów" c

echo "== T24 uzbrojony reset: tydzień 1% przy pracy → dokładnie jeden reset, Codex bez wiadomości"
new t24; N=$(date +%s); arm
add "$(okl 50 $((N+99999)) 1)|work"; add "$(okl 100 $((N+18000)) 100)|work"; add "$(okl 99 $((N+18000)) 100)|work"; add "$(okl 99 $((N+18000)) 100)|work"
run 3 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 1 ] && [ ! -e "$D/reset.armed" ] && ls "$D"/reset.used.* >/dev/null 2>&1 && [ "$(toclaude 'zużyłem JEDEN reset')" = 1 ] && [ "$(tocodex 'odnowione resetem')" = 0 ] && st RUNNING; }
check "T24 jeden reset, zgoda zużyta, stan RUNNING" c
check "T24 Claude dostaje liczbę pozostałych resetów" [ "$(toclaude 'Pozostałe resety: 1')" = 1 ]

echo "== T25 uzbrojony reset w pauzie 5 h → reset i jedno «kontynuuj», bez «wznów»"
new t25; N=$(date +%s); arm
printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' $((N+99999)) "$N" > "$D/state.${CODEX#%}"
add "$(okl 0 $((N+99999)) 1)|idle"; add "$(okl 100 $((N+18000)) 100)|idle"; add "$(okl 99 $((N+18000)) 100)|work"; add "$(okl 99 $((N+18000)) 100)|work"
run 3 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 1 ] && [ "$(tocodex 'odnowione resetem')" = 1 ] && [ "$(tocodex 'wznów (od')" = 0 ] && st RUNNING; }
check "T25 reset w pauzie, jedno kontynuuj, potem RUNNING" c

echo "== T26 błędy, potem sukces: ten sam klucz idempotencji, jeden reset"
new t26; N=$(date +%s); arm
printf '%s\n' "status=error reason=consume-timeout" "status=error reason=consume-timeout" "status=ok outcome=alreadyRedeemed credits_before=2 five_left=100 week_left=100" > "$D/rseq"
for _ in 1 2 3; do add "$(okl 50 $((N+99999)) 1)|work"; done; add "$(okl 100 $((N+18000)) 100)|work"; add "$(okl 99 $((N+18000)) 100)|work"
run 4 WD_RESET_TRIES_MAX=3 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 3 ] && [ "$(sort -u "$D/reset.calls" | wc -l)" = 1 ] && grep -qE '^[0-9a-f-]{36}$' "$D/reset.calls" && [ "$(toclaude 'zużyłem JEDEN reset')" = 1 ] && [ ! -e "$D/reset.armed" ]; }
check "T26 trzy próby jednym kluczem, jedno potwierdzenie" c

echo "== T27 noCredit → zgoda zdjęta, zwykła pauza tygodniowa"
new t27; N=$(date +%s); arm; echo "status=ok outcome=noCredit credits_before=0 five_left=50 week_left=1" > "$D/rseq"
add "$(okl 50 $((N+99999)) 1)|work"; add "$(okl 50 $((N+99999)) 1)|work"
run 3 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { ls "$D"/reset.failed.* >/dev/null 2>&1 && [ "$(toclaude 'nie zużyłem resetu')" = 1 ] && [ "$(tocodex 'limit TYGODNIOWY')" = 1 ] && st WEEKLY_STOP; }
check "T27 brak resetu → stara ścieżka WEEKLY_STOP" c

echo "== T28 trzy błędy → zgoda zdjęta, zgłoszenie, pauza tygodniowa"
new t28; N=$(date +%s); arm; printf 'status=error reason=x\nstatus=error reason=x\nstatus=error reason=x\n' > "$D/rseq"
for _ in 1 2 3 4; do add "$(okl 50 $((N+99999)) 1)|work"; done
run 5 WD_RESET_TRIES_MAX=3 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 3 ] && ls "$D"/reset.failed.* >/dev/null 2>&1 && [ "$(toclaude 'nie przeszedł po 3')" = 1 ] && st WEEKLY_STOP; }
check "T28 limit prób, potem stara ścieżka" c

echo "== T29 bez zgody: tydzień 1% → zero resetów (zachowanie v3.1)"
new t29; N=$(date +%s); add "$(okl 50 $((N+99999)) 1)|work"
run 2 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 0 ] && st WEEKLY_STOP; }
check "T29 bez pliku zgody nigdy nie resetuje" c

echo "== T30 po zużyciu: drugi niski tydzień → bez drugiego resetu"
new t30; N=$(date +%s); arm
add "$(okl 50 $((N+99999)) 1)|work"; add "$(okl 100 $((N+18000)) 100)|work"; add "$(okl 90 $((N+18000)) 1)|work"
run 4 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 1 ] && st WEEKLY_STOP; }
check "T30 najwyżej jeden reset na zgodę" c

echo "== T31 restart w trakcie próby: ten sam klucz z pliku stanu"
new t31; N=$(date +%s); arm; K=0f0e0d0c-0b0a-4908-8706-050403020100
printf 'CP=CP17\nSTATE=RUNNING\nPAUSE_WINDOW=\nPAUSE_SENT_AT=0\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\nRESET_KEY=%s\nRESET_TRIES=1\n' "$K" > "$D/state.${CODEX#%}"
add "$(okl 50 $((N+99999)) 1)|work"; add "$(okl 100 $((N+18000)) 100)|work"
run 2 WD_RESET_CMD="$T/fake_reset.sh $D"
check "T31 ponowienie po restarcie używa zapisanego klucza" [ "$(head -1 "$D/reset.calls" 2>/dev/null)" = "$K" ]

echo "== T32 uzbrojony reset pozwala wznowić po 5 h przy tygodniu 3%"
new t32; N=$(date +%s); arm
printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' $((N-100)) $((N-200)) > "$D/state.${CODEX#%}"
add "$(okl 90 $((N+18000)) 3)|idle"; add "$(okl 90 $((N+18000)) 3)|idle"
run 2 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(tocodex 'wznów (od')" = 1 ] && [ "$(calls)" = 0 ]; }
check "T32 wznów przy uzbrojonym resecie, bez zużycia" c

echo "== T33 reset_credit.py: najwcześniej wygasający kredyt, klucz, wynik; zły klucz bez połączenia"
RC=$HERE/../reset_credit.py
mkdir -p "$T/fakecodex"
cat > "$T/fakecodex/codex" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_RESET_LOG"]; reads = 0
open(log, "a").write("spawn\n")
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    meth, i = m["method"], m["id"]
    if meth == "initialize": r = {}
    elif meth == "account/rateLimits/read":
        reads += 1
        used = 99 if reads == 1 else 0
        r = {"ordinaryUsageAllowed": True,
             "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": 5},
                                               "secondary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": 9}}},
             "rateLimitResetCredits": {"availableCount": 2, "credits": [
                 {"id": "late", "status": "available", "expiresAt": 2000},
                 {"id": "early", "status": "available", "expiresAt": 1000},
                 {"id": "gone", "status": "redeemed", "expiresAt": 500}]}}
    elif meth == "account/rateLimitResetCredit/consume":
        open(log, "a").write(json.dumps(m["params"], sort_keys=True) + "\n"); r = {"outcome": "reset"}
    else:
        open(log, "a").write("FORBIDDEN " + meth + "\n"); r = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": i, "result": r}) + "\n"); sys.stdout.flush()
EOF
chmod +x "$T/fakecodex/codex"
K=11111111-2222-4333-8444-555555555555
OUT=$(PATH=$T/fakecodex:$PATH FAKE_RESET_LOG=$T/fr.log python3 -I "$RC" "$K")
check "T33 wynik i limity po resecie" [ "$OUT" = "status=ok outcome=reset credits_before=2 five_left=100 week_left=100" ]
check "T33 kredyt wygasający najwcześniej i podany klucz" grep -qF "{\"creditId\": \"early\", \"idempotencyKey\": \"$K\"}" "$T/fr.log"
checknot "T33 żadnej innej metody" grep -q FORBIDDEN "$T/fr.log"
rm -f "$T/fr.log"
OUT=$(PATH=$T/fakecodex:$PATH FAKE_RESET_LOG=$T/fr.log python3 -I "$RC" "nie-uuid")
c() { [ "$OUT" = "status=error reason=bad-key" ] && [ ! -e "$T/fr.log" ]; }
check "T33 zły klucz: błąd, bez uruchamiania Codexa" c
checknot "T33 limits.py dalej tylko do odczytu" grep -q 'rateLimitResetCredit/consume' "$LIM"

echo "== T34 domyślnie jedno wywołanie: błąd → bez ponowienia, zgoda zdjęta, pauza tygodniowa"
new t34; N=$(date +%s); arm; echo "status=error reason=consume-timeout" > "$D/rseq"
for _ in 1 2 3; do add "$(okl 50 $((N+99999)) 1)|work"; done
run 3 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 1 ] && ls "$D"/reset.failed.* >/dev/null 2>&1 && [ "$(toclaude 'nie przeszedł po 1')" = 1 ] && st WEEKLY_STOP; }
check "T34 jedna próba i koniec" c

echo "== T35 zgoda z innego okna tygodniowego → wygasa, zero resetów"
new t35; N=$(date +%s); echo "WEEK_RESET=12345" > "$D/reset.armed"
add "$(okl 50 $((N+99999)) 1)|work"; add "$(okl 50 $((N+99999)) 1)|work"
run 2 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 0 ] && ls "$D"/reset.expired.* >/dev/null 2>&1 && [ "$(toclaude 'zgoda na reset wygasła')" = 1 ] && st WEEKLY_STOP; }
check "T35 obca zgoda nigdy nie resetuje" c

echo "== T36 ręczny reset właściciela (nowe okno tygodnia) → drugi reset niemożliwy"
new t36; N=$(date +%s); arm
add "$(okl 50 $((N+99999)) 30)|work"
add "status=ok five_left=100 five_reset=$((N+18000)) week_left=100 week_reset=$((N+900000)) allowed=true|work"
add "status=ok five_left=80 five_reset=$((N+18000)) week_left=1 week_reset=$((N+900000)) allowed=true|work"
run 3 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 0 ] && [ ! -e "$D/reset.armed" ] && ls "$D"/reset.expired.* >/dev/null 2>&1 && st WEEKLY_STOP; }
check "T36 po ręcznym resecie zgoda wygasa" c

echo "== T37 limit 5 h nigdy nie zużywa resetu (tylko pauza)"
new t37; N=$(date +%s); arm
add "$(okl 2 $((N+99999)) 40)|work"; for _ in 1 2 3; do add "$(okl 0 $((N+99999)) 40)|idle"; done
run 4 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 0 ] && [ "$(tocodex 'LIMIT-5 (od')" = 1 ] && [ -e "$D/reset.armed" ]; }
check "T37 5 h: LIMIT-5 i pauza, zgoda nietknięta" c

echo "== T38 nazwy sesji: panel z hubi6 pane, wiadomość mówi jak pisać do Claude, stan pod nazwą"
tmux -S "$SOCK" new-session -d -s "t-codex-2" "exec cat > $T/codex2.in"
tmux -S "$SOCK" new-session -d -s "t-claude-2" "exec cat > $T/claude2.in"
CODEX2=$(tmux -S "$SOCK" display-message -p -t t-codex-2 '#{pane_id}')
CLAUDE2=$(tmux -S "$SOCK" display-message -p -t t-claude-2 '#{pane_id}')
# atrapa hubi6 pane: kolejne linie $d/map.<rodzaj> (ostatnia zostaje), "-" = sesji nie ma (kod 4)
cat > "$T/fake_pane.sh" <<'EOF2'
#!/usr/bin/env bash
d=$1 kind=$2; f=$d/map.$kind
echo "$2 $3 ${4:-primary}" >> "$d/pane.calls"
[[ -s $f ]] || exit 4
line=$(head -1 "$f"); (( $(wc -l < "$f") > 1 )) && sed -i 1d "$f"
[[ $line == - ]] && exit 4
echo "$line"
EOF2
chmod +x "$T/fake_pane.sh"
runn() { local polls=$1 cx=$2 cl=$3; shift 3
  env WD_DIR="$D" WD_TMUX_SOCK="$SOCK" WD_DRYRUN=1 WD_POLL=1 WD_RESUME_DELAY=1 WD_CODEX_ARGS_RE='^cat( |$)' WD_CLAUDE_ARGS_RE='^cat( |$)' \
      WD_MAX_POLLS="$polls" WD_SCREEN_FILE="$D/screen" WD_LIMITS_CMD="$T/fake_limits.sh $D" WD_PANE_CMD="$T/fake_pane.sh $D" "$@" \
      bash "$WD" CP17 "$cx" "$cl"; }
to() { awk -F'\t' -v p="$1" '$2==p' "$D/dryrun.out" 2>/dev/null | grep -c -- "$2"; }
new t38; N=$(date +%s); echo "$CODEX" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
add "$(okl 5 $((N+99999)) 60)|work"; add "$(okl 5 $((N+99999)) 60)|idle"
runn 2 codex/praca claude/praca/rev
check "T38 LIMIT-5 do panelu z nazwy" [ "$(to "$CODEX" 'LIMIT-5 (od')" = 1 ]
check "T38 Codex dostaje polecenie hubi send" [ "$(to "$CODEX" '«hubi send claude praca rev -m TEKST» linię «CP17 pauza')" = 1 ]
check "T38 bez numeru panelu w treści" bash -c "! grep -q 'wklej do' '$D/dryrun.out'"
check "T38 stan i blokada pod nazwą" [ -s "$D/state.codex-praca" ] && [ -e "$D/lock.codex-praca" ]
check "T38 nazwa przekazana do hubi6 pane" grep -qx 'codex praca primary' "$D/pane.calls"

echo "== T39 restart Codexa w trakcie: następna wiadomość idzie do nowego panelu"
new t39; N=$(date +%s); printf '%s\n%s\n%s\n' "$CODEX" "$CODEX" "$CODEX2" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
add "$(okl 50 $((N+99999)) 60)|work"; add "$(okl 50 $((N+99999)) 60)|work"; add "$(okl 5 $((N+99999)) 60)|work"
runn 3 codex/praca claude/praca
c() { [ "$(to "$CODEX2" 'LIMIT-5 (od')" = 1 ] && [ "$(to "$CODEX" 'LIMIT-5 (od')" = 0 ]; }; check "T39 LIMIT-5 trafił do nowego panelu" c

echo "== T40 Codex chwilowo nie działa: strażnik czeka, potem pracuje dalej"
new t40; N=$(date +%s); printf -- '-\n-\n%s\n' "$CODEX" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
add "$(okl 5 $((N+99999)) 60)|work"; add "$(okl 5 $((N+99999)) 60)|idle"
runn 4 codex/praca claude/praca
check "T40 czekał zamiast kończyć" grep -q 'nie działa; czekam' "$D/log"
checknot "T40 nie zakończył pracy" grep -q 'koniec: panel\|koniec: Codex' "$D/log"
check "T40 zauważył powrót" grep -q 'znowu działa' "$D/log"
check "T40 potem LIMIT-5" [ "$(to "$CODEX" 'LIMIT-5 (od')" = 1 ]

echo "== T41 Codex nie wraca: koniec po czasie bezczynności, informacja do Claude (też po nazwie)"
new t41; echo - > "$D/map.codex"; echo "$CLAUDE2" > "$D/map.claude"
runn 10 codex/praca claude/praca WD_IDLE_SEC=2
check "T41 koniec po bezczynności" grep -q 'koniec: Codex (codex/praca) nie działa' "$D/log"
check "T41 Claude powiadomiony w swoim bieżącym panelu" [ "$(to "$CLAUDE2" 'nie działa od')" = 1 ]

echo "== T43 wysyłka po nazwie idzie przez hubi send (blokada, przewijanie); błąd = ponowienie"
cat > "$T/fake_send.sh" <<'EOF2'
#!/usr/bin/env bash
d=$1; shift; printf '%s\n' "$*" >> "$d/send.calls"; exit "${FAKE_SEND_RC:-0}"
EOF2
chmod +x "$T/fake_send.sh"
new t43; N=$(date +%s); echo "$CODEX" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
add "$(okl 5 $((N+99999)) 60)|work"; add "$(okl 5 $((N+99999)) 60)|idle"
before=$(wc -c < "$T/codex.in")
runn 1 codex/praca claude/praca/rev WD_DRYRUN=0 WD_SEND_CMD="$T/fake_send.sh $D"
check "T43 hubi send dostał nazwę i treść" grep -qF 'codex praca -m LIMIT-5 (od Claude' "$D/send.calls"
check "T43 nic nie wklejono samodzielnie" [ "$(wc -c < "$T/codex.in")" = "$before" ]
check "T43 pauza zapisana po udanej wysyłce" grep -q 'STATE=PAUSE_REQUESTED' "$D/state.codex-praca"
new t43b; N=$(date +%s); echo "$CODEX" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
add "$(okl 5 $((N+99999)) 60)|work"; add "$(okl 5 $((N+99999)) 60)|work"
runn 2 codex/praca claude/praca WD_DRYRUN=0 WD_SEND_CMD="$T/fake_send.sh $D" FAKE_SEND_RC=4
c() { grep -q 'rc=4' "$D/log" && ! grep -q 'STATE=PAUSE_REQUESTED' "$D/state.codex-praca" && [ "$(grep -c 'LIMIT-5' "$D/send.calls")" -ge 2 ]; }
check "T43 kod 4 (przewijany) → bez pauzy, ponowienie w następnym obiegu" c

echo "== T44 stały panel w trybie kopiowania: nic nie wklejone"
tmux -S "$SOCK" copy-mode -t "$CODEX"; before=$(wc -c < "$T/codex.in")
( export S=$SOCK DIR=$T DRYRUN=0 FAIL_SEND= LOG=$T/t44.log CP=CP17 STATE=T CODEX_ARGS_RE='^cat( |$)' CLAUDE_ARGS_RE='^cat( |$)'; source "$src"; send_to "$CODEX" codex "przewijany-test" ); sleep 1
c() { [ "$(wc -c < "$T/codex.in")" = "$before" ] && grep -q 'panel przewijany' "$T/t44.log" && [ -z "$(tmux -S "$SOCK" list-buffers 2>/dev/null)" ]; }
check "T44 przewijany panel nie dostał tekstu" c
tmux -S "$SOCK" send-keys -t "$CODEX" -X cancel

# --- v4.1: Codex obudzony w pauzie (incydent 22.10: wiadomość od Claude obudziła Codexa przy 4%)
paused() { printf 'CP=CP17\nSTATE=PAUSED_5H\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' "$1" "$N" > "$D/state.${CODEX#%}"; }
echo "== T45 praca w pauzie przy niskim limicie → jedna ponowna pauza, jedno info do Claude, potem pauza potwierdzona"
new t45; N=$(date +%s); R=$((N+99999)); paused $R
add "$(okl 4 $R 60)|work"; add "$(okl 3 $R 60)|work"; for _ in 1 2 3; do add "$(okl 3 $R 60)|idle"; done
run 5
c() { [ "$(tocodex 'nadal obowiązuje')" = 1 ] && [ "$(toclaude 'ruszył w pauzie')" = 1 ] && [ "$(tocodex 'wznów (od')" = 0 ] && grep -q 'STATE=PAUSED_5H' "$D/state.${CODEX#%}" && grep -q "PAUSE_WINDOW=$R" "$D/state.${CODEX#%}"; }
check "T45 ponowna pauza, okno bez zmian, znowu PAUSED_5H" c
check "T45 wiadomość podaje godzinę odnowienia" [ "$(tocodex "$(date -u -d "@$R" +%H:%M) UTC")" = 1 ]
echo "== T46 pojedyncze obiegi pracy przeplatane bezczynnością w pauzie → bez wiadomości"
new t46; N=$(date +%s); R=$((N+99999)); paused $R
add "$(okl 4 $R 60)|idle"; add "$(okl 4 $R 60)|work"; add "$(okl 4 $R 60)|idle"; add "$(okl 4 $R 60)|work"; add "$(okl 4 $R 60)|idle"
run 5
check "T46 zero wiadomości do Codexa" [ "$(tocodex '.')" = 0 ]
echo "== T47 kolejne pobudki: najwyżej REPAUSE_MAX ponownych pauz na okno, potem jedno info"
new t47; N=$(date +%s); R=$((N+99999)); paused $R
for _ in 1 2 3 4; do add "$(okl 4 $R 60)|work"; add "$(okl 4 $R 60)|work"; add "$(okl 4 $R 60)|idle"; add "$(okl 4 $R 60)|idle"; done
run 16
c() { [ "$(tocodex 'nadal obowiązuje')" = 3 ] && [ "$(toclaude 'mimo 3 ponownych')" = 1 ] && [ "$(toclaude 'ruszył w pauzie')" = 1 ]; }
check "T47 trzy ponowne pauzy, jedno ostrzeżenie" c
echo "== T48 ręczny reset w pauzie (limit wysoki, Codex pracuje) → jedno wznów, RUNNING; ochrona LIMIT-5 wraca"
new t48; N=$(date +%s); R=$((N+99999)); R2=$((N+18000)); paused $R
add "$(okl 100 $R2 60)|work"; add "$(okl 60 $R2 60)|work"; add "$(okl 5 $R2 60)|work"
run 3
c() { [ "$(tocodex 'nadal obowiązuje')" = 0 ] && [ "$(tocodex 'wznów (od')" = 1 ] && [ "$(tocodex 'LIMIT-5 (od')" = 1 ] && grep -q 'STATE=PAUSE_REQUESTED' "$D/state.${CODEX#%}"; }
check "T48 bez ponownej pauzy, jedno wznów, potem zwykły LIMIT-5" c
echo "== T49 praca w pauzie, limit nieznany"
new t49; N=$(date +%s); paused $((N-100))
for _ in 1 2 3; do add "status=error reason=read-timeout|work"; done
run 3
check "T49 okno minęło, limit nieznany → bez wiadomości" [ "$(tocodex '.')" = 0 ]
new t49b; N=$(date +%s); R=$((N+99999)); paused $R
for _ in 1 2; do add "status=error reason=read-timeout|work"; done
run 2
check "T49 okno jeszcze trwa, limit nieznany → ponowna pauza" [ "$(tocodex 'nadal obowiązuje')" = 1 ]

# --- v4.2: sam ruch w panelu to nie praca (incydent 22.10 10:49: limit 100%, ruch na ekranie, brak wznów)
echo "== T50 limit odnowiony, ruch w panelu bez «Working (» → wznów wysłany"
new t50; N=$(date +%s); paused $((N-100))
for _ in 1 2 3; do add "$(okl 100 $((N+18000)) 75)|idle"; done
( for _ in $(seq 12); do date +%s > "$D/screen.activity"; sleep 0.5; done ) &
run 2
wait
check "T50 jedno wznów mimo ruchu w panelu" [ "$(tocodex 'wznów (od')" = 1 ]
checknot "T50 bez fałszywego «już pracuje»" grep -q 'już pracuje' "$D/log"
new t50b; N=$(date +%s); paused $((N-100))
for _ in 1 2; do add "$(okl 100 $((N+18000)) 75)|work"; done
run 2
c() { [ "$(tocodex 'wznów (od')" = 1 ] && grep -q 'wznowienie potwierdzone' "$D/log" && grep -q 'STATE=RUNNING' "$D/state.${CODEX#%}"; }
check "T50 v4.3: pasek pracy po odnowieniu → mimo to jedno wznów, potem RUNNING" c
echo "== T51 pauza przy niskim limicie, ruch w panelu bez «Working (» → bez ponownej pauzy"
new t51; N=$(date +%s); R=$((N+99999)); paused $R
for _ in 1 2 3 4; do add "$(okl 3 $R 60)|idle"; done
( for _ in $(seq 14); do date +%s > "$D/screen.activity"; sleep 0.5; done ) &
run 4
wait
c() { [ "$(tocodex '.')" = 0 ] && grep -q 'STATE=PAUSED_5H' "$D/state.${CODEX#%}"; }
check "T51 zero wiadomości, pauza trwa" c

# --- v4.3: przypadki R1–R7 z review claude/sh (review-v42-wynik.md)
st() { grep -q "STATE=$1" "$D/state.${CODEX#%}"; }
setst() { printf 'CP=CP17\nSTATE=%s\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=0\nWEEK_SENT=\nNOTIFIED=\n' "$1" "$2" "$3" > "$D/state.${CODEX#%}"; }
echo "== T52 (R1) pauza przy 3%, pasek «Compacting context (…)» → ponowna pauza"
new t52; N=$(date +%s); R=$((N+99999)); setst PAUSED_5H $R "$N"
for _ in 1 2 3 4; do add "$(okl 3 $R 60)|comp"; done
run 4
check "T52 ponowna pauza w czasie kompaktowania" [ "$(tocodex 'nadal obowiązuje')" = 1 ]
echo "== T53 (R2) LIMIT-5 przed końcem okna, okno odnowione, sam ruch w panelu → wznów"
new t53; N=$(date +%s); setst PAUSE_REQUESTED $((N-100)) $((N-600))
for _ in $(seq 4); do add "$(okl 100 $((N+18000)) 60)|idle"; done
( for _ in $(seq 10); do date +%s > "$D/screen.activity"; sleep 0.5; done ) &
run 4 WD_IDLE_SEC=3; wait
c() { [ "$(tocodex 'wznów (od')" = 1 ] && ! grep -q 'bez wznów' "$D/log"; }
check "T53 jedno wznów, żadnego przejścia bez wznów" c
echo "== T54 (R3) PAUSE_REQUESTED, okno odnowione, gdy Codex kończy zadanie, potem stoi → wznów i zgłoszenie"
new t54; N=$(date +%s); setst PAUSE_REQUESTED $((N-100)) $((N-600))
add "$(okl 100 $((N+18000)) 60)|work"; for _ in $(seq 5); do add "$(okl 100 $((N+18000)) 60)|idle"; done
run 6 WD_IDLE_SEC=3 WD_RESUME_CONFIRM_SEC=2
c() { [ "$(tocodex 'wznów (od')" = 1 ] && [ "$(toclaude 'nie ruszył')" = 1 ] && st RESUME_REQUESTED && ! grep -q 'koniec: Codex bezczynny' "$D/log"; }
check "T54 wznów mimo pracy; brak ruszenia → jedno zgłoszenie, bez cichego wyjścia" c
echo "== T55 (R4) PAUSED_5H, okno odnowione, Codex obudzony (pracuje), potem stoi → wznów"
new t55; N=$(date +%s); setst PAUSED_5H $((N-100)) $((N-600))
add "$(okl 100 $((N+18000)) 60)|work"; for _ in $(seq 5); do add "$(okl 100 $((N+18000)) 60)|idle"; done
run 6 WD_IDLE_SEC=3
c() { [ "$(tocodex 'wznów (od')" = 1 ] && ! grep -q 'bez wznów' "$D/log" && ! grep -q 'koniec: Codex bezczynny' "$D/log"; }
check "T55 jedno wznów, bez cichego wyjścia" c
echo "== T56 (R5) restart w tym samym oknie → najwyżej REPAUSE_MAX ponownych pauz łącznie"
new t56; N=$(date +%s); R=$((N+99999)); setst PAUSED_5H $R "$N"
for _ in 1 2 3; do add "$(okl 4 $R 60)|work"; add "$(okl 4 $R 60)|work"; add "$(okl 4 $R 60)|idle"; add "$(okl 4 $R 60)|idle"; done
run 12
for _ in 1 2 3; do add "$(okl 4 $R 60)|work"; add "$(okl 4 $R 60)|work"; add "$(okl 4 $R 60)|idle"; add "$(okl 4 $R 60)|idle"; done
run 12
c() { [ "$(tocodex 'nadal obowiązuje')" = 3 ] && grep -q 'REPAUSES=3' "$D/state.${CODEX#%}" && [ "$(toclaude 'mimo 3 ponownych')" = 1 ]; }
check "T56 trzy ponowne pauzy mimo restartu, jedno ostrzeżenie" c
echo "== T57 (R6) po wznów sam ruch w panelu → bez potwierdzenia, zgłoszenie «nie ruszył»"
new t57; N=$(date +%s); setst PAUSED_5H $((N-100)) $((N-600))
for _ in $(seq 6); do add "$(okl 100 $((N+18000)) 60)|idle"; done
( for _ in $(seq 20); do date +%s > "$D/screen.activity"; sleep 0.5; done ) &
run 6 WD_RESUME_CONFIRM_SEC=2; wait
c() { [ "$(toclaude 'nie ruszył')" = 1 ] && ! grep -q 'wznowienie potwierdzone' "$D/log"; }
check "T57 ruch bez paska nie potwierdza wznowienia" c
echo "== T58 (R7) bezczynny Codex cytuje «Working (» w pauzie przy 4% → bez wiadomości"
new t58; N=$(date +%s); R=$((N+99999)); setst PAUSED_5H $R "$N"
for _ in 1 2 3 4; do add "$(okl 4 $R 60)|quote"; done
run 4
check "T58 zero wiadomości do Codexa" [ "$(tocodex '.')" = 0 ]
echo "== T59 plik stanu sprzed v4.3 czytany przez nową wersję, a plik nowej wersji przez v4 (wycofanie bez utraty pauzy)"
new t59; N=$(date +%s); R=$((N+99999)); setst PAUSED_5H $R "$N"; add "$(okl 4 $R 60)|idle"
run 1
c() { grep -q 'REPAUSES=0' "$D/state.${CODEX#%}" && grep -q "PAUSE_WINDOW=$R" "$D/state.${CODEX#%}" && st PAUSED_5H; }
check "T59 stary plik → pauza zachowana, nowe pola dopisane" c
add "$(okl 4 $R 60)|idle"
( WD=$HERE/../watchdog.v4.sh; run 1 )
c() { grep -q "PAUSE_WINDOW=$R" "$D/state.${CODEX#%}" && st PAUSED_5H && grep -q 'start (stan z pliku: PAUSED_5H)' "$D/log" && [ "$(tocodex '.')" = 0 ]; }
check "T59 v4 czyta plik nowej wersji → pauza zachowana, zero wiadomości" c

# --- v4.4: przypadki N1–N3 z re-review claude/sh (review-v43-wynik.md)
echo "== T60 (N1) po wznów pasek nierozpoznany, limit spada do 5% → LIMIT-5 mimo braku potwierdzenia"
new t60; N=$(date +%s); setst PAUSED_5H $((N-100)) $((N-600))
add "$(okl 100 $((N+18000)) 60)|idle"; add "$(okl 50 $((N+18000)) 60)|idle"; for _ in 1 2 3; do add "$(okl 5 $((N+18000)) 60)|idle"; done
( for _ in $(seq 20); do date +%s > "$D/screen.activity"; sleep 0.5; done ) &
run 5; wait
c() { [ "$(tocodex 'wznów (od')" = 1 ] && [ "$(tocodex 'LIMIT-5 (od')" = 1 ] && grep -qE 'STATE=(PAUSE_REQUESTED|PAUSED_5H)' "$D/state.${CODEX#%}" && grep -q "PAUSE_WINDOW=$((N+18000))" "$D/state.${CODEX#%}"; }
check "T60 jedno wznów, potem jeden LIMIT-5 dla nowego okna" c
echo "== T61 (N2) MAX_HOURS w pauzie → strażnik zostaje, po odnowieniu wznów, koniec dopiero po potwierdzeniu"
new t61; N=$(date +%s); R=$((N+3)); setst PAUSED_5H $R "$N"
for _ in 1 2 3; do add "$(okl 3 $R 60)|idle"; done; add "$(okl 100 $((N+18000)) 60)|idle"; add "$(okl 100 $((N+18000)) 60)|work"; add "$(okl 99 $((N+18000)) 60)|work"
run 10 WD_MAX_HOURS=0
c() { [ "$(tocodex 'wznów (od')" = 1 ] && grep -q 'zostaję do wznowienia' "$D/log" && grep -q 'wznowienie potwierdzone' "$D/log" && grep -q 'koniec: minęło 0 h' "$D/log"; }
check "T61 pauza przeżywa MAX_HOURS, wznów wysłany, koniec po potwierdzeniu" c
new t61b; N=$(date +%s); setst PAUSED_5H $((N-100)) $((N-600))
for _ in $(seq 6); do add "$(okl 100 $((N+18000)) 60)|idle"; done
run 6 WD_MAX_HOURS=0 WD_RESUME_CONFIRM_SEC=2
c() { [ "$(tocodex 'wznów (od')" = 1 ] && [ "$(toclaude 'nie potwierdził')" = 1 ] && grep -q 'koniec: minęło 0 h, wznów niepotwierdzone' "$D/log"; }
check "T61 niepotwierdzone wznów po MAX_HOURS → jedno zgłoszenie i koniec" c
echo "== T62 (N3) wznów nie wychodzi → jedno zgłoszenie do Claude, ponawianie"
new t62; N=$(date +%s); setst PAUSED_5H $((N-100)) $((N-600))
for _ in $(seq 6); do add "$(okl 100 $((N+18000)) 60)|idle"; done
run 6 WD_FAIL_SEND=codex WD_RESUME_CONFIRM_SEC=2
c() { [ "$(toclaude 'nie mogę wysłać')" = 1 ] && [ "$(grep -c 'wznów należny, ale nie wysłany' "$D/log")" -ge 3 ] && st PAUSED_5H; }
check "T62 jedno zgłoszenie, ponawianie, pauza trwa" c
new t62b; N=$(date +%s); setst PAUSED_5H $((N-100)) $((N-600))
for _ in $(seq 5); do add "$(okl 100 $((N+18000)) 60)|err"; done
run 5 WD_RESUME_CONFIRM_SEC=2
c() { [ "$(tocodex '.')" = 0 ] && [ "$(toclaude 'ekran Codexa nieczytelny')" = 1 ]; }
check "T62 nieczytelny ekran blokuje wznów, jedno zgłoszenie" c

# --- v4.5: osobne progi (właściciel 22.10): pauza 5 h przy 7%, stop tygodniowy dopiero przy 1%
rundef() { ( unset WD_LOW WD_WEEK_LOW; run "$@" ); }   # domyślne progi wersji pod testem
echo "== T63 domyślne progi: 5 h <= 7% → LIMIT-5; tydzień 4–2% → nic; tydzień 1% → stop tygodniowy"
new t63; N=$(date +%s); R=$((N+99999))
add "$(okl 8 $R 60)|work"; add "$(okl 7 $R 60)|work"; add "$(okl 7 $R 60)|idle"
rundef 3
c() { [ "$(tocodex 'LIMIT-5 (od')" = 1 ] && [ "$(tocodex 'ok. 7% limitu 5 h')" = 1 ] && [ "$(tocodex 'TYGODNIOWY')" = 0 ]; }
check "T63 jeden LIMIT-5 przy 7%, nie przy 8%" c
check "T63 log startu podaje progi" grep -q 'progi: 5 h <= 7%, tydzień <= 1%' "$D/log"
new t63b; N=$(date +%s); R=$((N+99999))
for w in 4 3 2; do add "$(okl 50 $R $w)|work"; done
rundef 3
c() { [ "$(tocodex '.')" = 0 ] && [ "$(toclaude '.')" = 0 ] && st RUNNING; }
check "T63 tydzień 4%, 3%, 2% → zero wiadomości, praca trwa" c
add "$(okl 50 $R 1)|work"
rundef 2
c() { [ "$(tocodex 'limit TYGODNIOWY Codexa ok. 1%')" = 1 ] && [ "$(toclaude 'Codex dostał LIMIT-5')" = 1 ] && st WEEKLY_STOP && grep -q 'koniec: limit tygodniowy 1%' "$D/log"; }
check "T63 tydzień 1% → jeden LIMIT-5 tygodniowy, WEEKLY_STOP, koniec" c
echo "== T64 domyślne progi: odnowienie 5 h przy tygodniu 3% → wznów (dawniej stop tygodniowy)"
new t64; N=$(date +%s); setst PAUSED_5H $((N-100)) $((N-600))
for _ in 1 2 3; do add "$(okl 90 $((N+18000)) 3)|idle"; done
rundef 3
c() { [ "$(tocodex 'wznów (od')" = 1 ] && [ "$(tocodex 'tydzień 3%')" = 1 ] && [ "$(tocodex 'TYGODNIOWY')" = 0 ] && ! st WEEKLY_STOP; }
check "T64 jedno wznów, bez pauzy tygodniowej" c
echo "== T65 jawne progi z polecenia startu (WD_LOW=7 WD_WEEK_LOW=1): 5 h 7% i tydzień 7% → tylko pauza 5 h"
new t65; N=$(date +%s); R=$((N+99999))
add "$(okl 7 $R 7)|work"; add "$(okl 7 $R 7)|idle"; add "$(okl 7 $R 7)|idle"
run 3 WD_LOW=7 WD_WEEK_LOW=1
c() { [ "$(tocodex 'LIMIT-5 (od')" = 1 ] && [ "$(tocodex 'TYGODNIOWY')" = 0 ] && st PAUSED_5H && grep -q "PAUSE_WINDOW=$R" "$D/state.${CODEX#%}"; }
check "T65 pauza 5 h, bez stopu tygodniowego" c
echo "== T66 tydzień 0% (poniżej progu) → stop tygodniowy"
new t66; N=$(date +%s); add "$(okl 60 $((N+99999)) 0)|work"
rundef 2
c() { [ "$(tocodex 'limit TYGODNIOWY Codexa ok. 0%')" = 1 ] && st WEEKLY_STOP; }
check "T66 jeden LIMIT-5 tygodniowy, WEEKLY_STOP" c
echo "== T67 zły próg → kod 2, nic nie wysłane; 0 na początku liczby dozwolone"
t67() { new "t67-$1"; add "$(okl 50 $((N+99999)) 60)|work"; run 1 "$2" 2> "$D/err"; local rc=$?; [ $rc = 2 ] && grep -q 'zły próg' "$D/err" && [ ! -e "$D/dryrun.out" ]; }
N=$(date +%s)
check "T67 WD_WEEK_LOW=1% odrzucone" t67 a 'WD_WEEK_LOW=1%'
check "T67 WD_LOW=abc odrzucone" t67 b 'WD_LOW=abc'
check "T67 WD_WEEK_LOW=101 odrzucone" t67 c 'WD_WEEK_LOW=101'
check "T67 WD_LOW=-1 odrzucone" t67 d 'WD_LOW=-1'
new t67e; add "$(okl 50 $((N+99999)) 60)|work"; run 1 WD_LOW=08 WD_WEEK_LOW=01
check "T67 WD_LOW=08 WD_WEEK_LOW=01 → progi 8% i 1%" grep -q 'progi: 5 h <= 8%, tydzień <= 1%' "$D/log"
echo "== T68 uzbrojony reset przy domyślnych progach: tydzień 4% → nic, tydzień 1% → jeden reset"
new t68; N=$(date +%s); arm
add "$(okl 50 $((N+99999)) 4)|work"; add "$(okl 50 $((N+99999)) 4)|work"; add "$(okl 50 $((N+99999)) 1)|work"
add "$(okl 100 $((N+18000)) 100)|work"; add "$(okl 99 $((N+18000)) 100)|work"
rundef 4 WD_RESET_CMD="$T/fake_reset.sh $D"
c() { [ "$(calls)" = 1 ] && [ "$(toclaude 'reset uzbrojony')" = 0 ] && [ "$(tocodex 'LIMIT-5 (od')" = 0 ] && ls "$D"/reset.used.* >/dev/null 2>&1 && st RUNNING; }
check "T68 jeden reset przy 1%, wcześniej bez wiadomości" c

# --- v4.6: tygodniowy LIMIT-5 najpierw commit tego, co jest (właściciel 22.10, opcja 3)
echo "== T69 tekst tygodniowego LIMIT-5: commit od razu, bez kończenia zadania; tekst 5 h bez zmian"
new t69; N=$(date +%s); add "$(okl 50 $((N+99999)) 1)|work"
rundef 2
c() { [ "$(tocodex 'TYGODNIOWY.*Zatrzymaj się teraz i nie zaczynaj nic nowego. Najpierw zrób commit tego, co już zmieniłeś')" = 1 ] && [ "$(tocodex 'TYGODNIOWY.*Dokończ')" = 0 ] && [ "$(tocodex 'TYGODNIOWY.*pauza: co zrobione, co zostało')" = 1 ] && st WEEKLY_STOP; }
check "T69 tygodniowy: zatrzymaj, najpierw commit, potem linia do Claude" c
new t69b; N=$(date +%s); add "$(okl 7 $((N+99999)) 60)|work"
rundef 1
check "T69 5 h: nadal «Dokończ bieżące zadanie»" [ "$(tocodex 'limitu 5 h. Dokończ bieżące zadanie, zrób commit')" = 1 ]

# --- v4.7: tura przerwana błędem „Selected model is at capacity” (właściciel 22.10)
capw() { tocodex 'wznów po błędzie (od'; }
echo "== T70 RUNNING: błąd capacity w dwóch obiegach → jedno wznów, bez zgłoszenia; potem praca"
new t70; N=$(date +%s); R=$((N+99999))
add "$(okl 50 $R 60)|cap"; add "$(okl 50 $R 60)|cap"; add "$(okl 49 $R 60)|work"; add "$(okl 48 $R 60)|work"
rundef 4
c() { [ "$(capw)" = 1 ] && [ "$(tocodex 'model at capacity.*Kontynuuj od miejsca')" = 1 ] && [ "$(toclaude '.')" = 0 ] && st RUNNING && grep -q 'wznów wysłany (próba 1)' "$D/log" && grep -q 'Codex pracuje po błędzie capacity' "$D/log"; }
check "T70 jedno wznów po błędzie, Claude bez wiadomości, epizod zamknięty" c
check "T70 tekst bez «wznów (od» (nie myli się z wznowieniem po limicie)" [ "$(tocodex 'wznów (od')" = 0 ]
echo "== T71 jeden obieg z błędem, potem praca → bez wiadomości"
new t71; N=$(date +%s); R=$((N+99999)); add "$(okl 50 $R 60)|cap"; add "$(okl 50 $R 60)|work"; add "$(okl 50 $R 60)|work"
rundef 3
check "T71 zero wiadomości do Codexa" [ "$(tocodex '.')" = 0 ]
echo "== T72 błąd się powtarza → 3 wznów, jedno zgłoszenie, potem rzadziej; postój to nie bezczynność"
new t72; N=$(date +%s); R=$((N+99999)); for _ in $(seq 10); do add "$(okl 50 $R 60)|cap"; done
rundef 10 WD_CAP_RETRY_SEC=0 WD_CAP_SLOW_SEC=9999 WD_IDLE_SEC=2
c() { [ "$(capw)" = 3 ] && [ "$(toclaude 'nadal stoi na błędzie')" = 1 ] && ! grep -q 'bezczynny' "$D/log" && st RUNNING; }
check "T72 trzy wznów, jedno zgłoszenie, bez końca z bezczynności" c
new t72b; N=$(date +%s); R=$((N+99999)); for _ in $(seq 6); do add "$(okl 50 $R 60)|cap"; done
rundef 6
check "T72 domyślny odstęp 5 min: w 6 szybkich obiegach jedno wznów" [ "$(capw)" = 1 ]
echo "== T73 pauza: błąd capacity nigdy nie budzi Codexa, a w PAUSE_REQUESTED potwierdza pauzę"
new t73; N=$(date +%s); R=$((N+99999)); setst PAUSED_5H $R "$N"; for _ in 1 2 3 4; do add "$(okl 3 $R 60)|cap"; done
rundef 4
c() { [ "$(tocodex '.')" = 0 ] && st PAUSED_5H; }; check "T73 PAUSED_5H: zero wiadomości" c
new t73b; N=$(date +%s); R=$((N+99999)); setst PAUSE_REQUESTED $R "$N"; for _ in 1 2 3; do add "$(okl 5 $R 60)|cap"; done
rundef 3
c() { [ "$(tocodex '.')" = 0 ] && st PAUSED_5H; }; check "T73 PAUSE_REQUESTED + błąd → pauza potwierdzona, zero wiadomości" c
echo "== T74 niski albo nieznany limit → bez wznów po błędzie"
new t74; N=$(date +%s); R=$((N+99999)); for _ in 1 2 3; do add "$(okl 7 $R 60)|cap"; done
rundef 3
c() { [ "$(tocodex 'LIMIT-5 (od')" = 1 ] && [ "$(capw)" = 0 ]; }; check "T74 5 h 7%: LIMIT-5, bez wznów po błędzie" c
new t74b; for _ in 1 2 3; do add "status=error reason=read-timeout|cap"; done
rundef 3
check "T74 limity nieczytelne: zero wiadomości" [ "$(tocodex '.')" = 0 ]
new t74c; N=$(date +%s); for _ in 1 2 3; do add "$(okl 50 $((N+99999)) 60 false)|cap"; done
rundef 3
check "T74 allowed=false: zero wiadomości" [ "$(tocodex '.')" = 0 ]
echo "== T75 stary błąd nad nowszą rozmową, cytat w odpowiedzi, błąd z paskiem pracy → nic"
for k in capold capquote capbar; do
  new "t75-$k"; N=$(date +%s); for _ in 1 2 3; do add "$(okl 50 $((N+99999)) 60)|$k"; done
  rundef 3
  check "T75 $k: zero wiadomości" [ "$(tocodex '.')" = 0 ]
done
echo "== T76 błąd zawinięty na wąskim panelu, separator i szkic w polu wpisywania → wznów"
new t76; N=$(date +%s); add "$(okl 50 $((N+99999)) 60)|capwrap"; add "$(okl 50 $((N+99999)) 60)|capwrap"
rundef 2
check "T76 jedno wznów" [ "$(capw)" = 1 ]
echo "== T77 RESUME_REQUESTED: po wznów od razu błąd → wznów po błędzie, bez «nie ruszył», potem potwierdzenie"
new t77; N=$(date +%s)
printf 'CP=CP17\nSTATE=RESUME_REQUESTED\nPAUSE_WINDOW=%s\nPAUSE_SENT_AT=%s\nRESUMED_AT=%s\nWEEK_SENT=\nNOTIFIED=\n' $((N-200)) $((N-9000)) "$N" > "$D/state.${CODEX#%}"
add "$(okl 95 $((N+18000)) 60)|cap"; add "$(okl 95 $((N+18000)) 60)|cap"; add "$(okl 94 $((N+18000)) 60)|work"; add "$(okl 93 $((N+18000)) 60)|work"
rundef 4 WD_RESUME_CONFIRM_SEC=1
c() { [ "$(capw)" = 1 ] && [ "$(toclaude 'nie ruszył')" = 0 ] && grep -q 'wznowienie potwierdzone' "$D/log" && st RUNNING; }
check "T77 wznów po błędzie, bez fałszywego zgłoszenia, RUNNING" c
echo "== T78 drugi epizod po prawdziwej pracy liczy się od nowa (bez czekania 5 min)"
new t78; N=$(date +%s); R=$((N+99999))
for k in cap cap work work cap cap; do add "$(okl 50 $R 60)|$k"; done
rundef 6
check "T78 dwa wznów w dwóch epizodach" [ "$(capw)" = 2 ]
echo "== T79 zła wartość ustawienia capacity → kod 2"
new t79; add "$(okl 50 $((N+99999)) 60)|work"; run 1 WD_CAP_RETRY_MIN=5m 2> "$D/err"; RC=$?
c() { [ $RC = 2 ] && grep -q 'zła wartość CAP_RETRY_MIN' "$D/err"; }; check "T79 WD_CAP_RETRY_MIN=5m odrzucone" c

# --- v4.8: poprawki z review claude/sh v4.7 (Y1, Y2)
echo "== T80 (Y1) podgląd rozmowy bez pola wpisywania: stary błąd nad wiadomością, praca pod nią → nic"
new t80; N=$(date +%s); for _ in 1 2 3; do add "$(okl 50 $((N+99999)) 60)|capov"; done
rundef 3
check "T80 zero wiadomości do Codexa" [ "$(tocodex '.')" = 0 ]
echo "== T81 (Y2) wznów po błędzie nie wychodzi → ponawianie i jedno zgłoszenie do Claude"
new t81; N=$(date +%s); for _ in $(seq 8); do add "$(okl 50 $((N+99999)) 60)|cap"; done
rundef 8 WD_FAIL_SEND=codex WD_CAP_RETRY_SEC=0 WD_RESUME_CONFIRM_SEC=2
c() { [ "$(toclaude 'stoi na błędzie.*nie mogę wysłać mu wznowienia')" = 1 ] && [ "$(grep -c 'NIE wysłano.*FAIL_SEND' "$D/log")" -ge 5 ]; }
check "T81 jedno zgłoszenie, ponawianie co obieg" c
echo "== T82 układ jak w prawdziwym Codexie 0.163 (próba z błędem 400): zawinięcie bez wcięcia, wąski panel, ⚠ pod błędem"
for k in capwrap0 capnarrow capwarn; do
  new "t82-$k"; N=$(date +%s); for _ in 1 2; do add "$(okl 50 $((N+99999)) 60)|$k"; done
  rundef 2
  check "T82 $k: jedno wznów" [ "$(capw)" = 1 ]
done
for k in cap400 warnonly; do
  new "t82-$k"; N=$(date +%s); for _ in 1 2 3; do add "$(okl 50 $((N+99999)) 60)|$k"; done
  rundef 3
  check "T82 $k: zero wiadomości" [ "$(tocodex '.')" = 0 ]
done

# --- v4.9: WD_REPORT_TO (układ weekendowy 23.10)
echo "== T83 WD_REPORT_TO: wznów wskazuje reviewera, linia pauzy i zgłoszenia zostają przy <Claude>"
new t83; N=$(date +%s); echo "$CODEX" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
add "$(okl 5 $((N+99999)) 60)|work"; add "$(okl 5 $((N+99999)) 60)|idle"; add "$(okl 5 $((N+99999)) 60)|idle"; add "$(okl 100 $((N+120000)) 60)|idle"
runn 4 codex/praca claude/praca/wykonawca-sh WD_REPORT_TO=codex/praca/temp-rev
check "T83 linia pauzy do strażnika" [ "$(to "$CODEX" '«hubi send claude praca wykonawca-sh -m TEKST» linię «CP17 pauza')" = 1 ]
check "T83 wznów: zgłoszenia do reviewera" [ "$(to "$CODEX" 'CP17 wznów (od.*Do reviewera pisz poleceniem «hubi send codex praca temp-rev -m TEKST»: zgłoszenia, STOP i linie gotowości')" = 1 ]
check "T83 wznów bez «Do mnie pisz»" [ "$(to "$CODEX" 'Do mnie pisz')" = 0 ]
check "T83 log startu podaje adres" grep -q 'start (.*; zgłoszenia Codexa: codex/praca/temp-rev' "$D/log"
echo "== T84 WD_REPORT_TO: wznów po błędzie capacity też wskazuje reviewera"
new t84; N=$(date +%s); echo "$CODEX" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
add "$(okl 50 $((N+99999)) 60)|cap"; add "$(okl 50 $((N+99999)) 60)|cap"
runn 2 codex/praca claude/praca/wykonawca-sh WD_REPORT_TO=codex/praca/temp-rev
check "T84 wznów po błędzie do reviewera" [ "$(to "$CODEX" 'wznów po błędzie (od.*Do reviewera pisz poleceniem «hubi send codex praca temp-rev -m TEKST»')" = 1 ]
echo "== T85 bez WD_REPORT_TO (albo równe <Claude>) teksty jak w v4.8"
for rt in "" claude/praca; do
  new "t85-${rt//\//-}"; N=$(date +%s); echo "$CODEX" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
  add "$(okl 5 $((N+99999)) 60)|work"; add "$(okl 5 $((N+99999)) 60)|idle"; add "$(okl 5 $((N+99999)) 60)|idle"; add "$(okl 100 $((N+120000)) 60)|idle"
  runn 4 codex/praca claude/praca ${rt:+WD_REPORT_TO=$rt}
  check "T85 «${rt:-puste}»: wznów z «Do mnie pisz»" [ "$(to "$CODEX" 'CP17 wznów (od.*Do mnie pisz poleceniem «hubi send claude praca -m TEKST»: zgłoszenia')" = 1 ]
  checknot "T85 «${rt:-puste}»: bez «Do reviewera»" grep -q 'Do reviewera' "$D/dryrun.out"
done
echo "== T86 zły WD_REPORT_TO → kod 2"
t86() { env WD_DIR="$T/t86" WD_MAX_POLLS=1 WD_POLL=0 WD_PANE_CMD=false WD_DRYRUN=1 WD_REPORT_TO="$1" bash "$WD" CP17 codex/praca claude/praca 2>/dev/null; [ $? = 2 ]; }
for bad in %5 temp-rev 'codex/pra;ca' codex/praca/-x 'codex/pra ca' other/praca; do
  check "T86 odrzucone: «$bad»" t86 "$bad"
done

echo "== T87 kontakt = inna sesja Codexa (temp-rev): pauza, wznów i zgłoszenia tam, bez Claude"
new t87; N=$(date +%s); echo "$CODEX" > "$D/map.codex"; echo "$CLAUDE" > "$D/map.claude"
add "$(okl 5 $((N+99999)) 60)|work"; add "$(okl 5 $((N+99999)) 60)|idle"; add "$(okl 5 $((N+99999)) 60)|idle"; add "$(okl 100 $((N+120000)) 60)|idle"
runn 4 codex/praca codex/praca/temp-rev
check "T87 linia pauzy do temp-rev" [ "$(to "$CODEX" '«hubi send codex praca temp-rev -m TEKST» linię «CP17 pauza')" = 1 ]
check "T87 wznów: do reviewera temp-rev" [ "$(to "$CODEX" 'CP17 wznów (od.*Do reviewera pisz poleceniem «hubi send codex praca temp-rev -m TEKST»')" = 1 ]
checknot "T87 nic do Claude" grep -q 'claude praca' "$D/dryrun.out"
check "T87 brak zapytań o panel Claude" bash -c "! grep -q '^claude ' '$D/pane.calls'"

echo "== T42 złe nazwy → kod 2"
t42() { env WD_DIR="$T/t42" WD_MAX_POLLS=1 WD_POLL=0 WD_PANE_CMD=false WD_DRYRUN=1 bash "$WD" CP17 "$1" "$2" 2>/dev/null; [ $? = 2 ]; }
check "T42 odrzucone: Claude w miejscu Codexa" t42 claude/praca claude/praca
check "T42 odrzucone: Codex w miejscu Claude" t42 codex/praca codex/praca
check "T42 odrzucone: średnik w nazwie repo" t42 'codex/pra;ca' claude/praca
check "T42 odrzucone: pusta nazwa repo" t42 codex/ claude/praca
check "T42 odrzucone: zła nazwa sesji" t42 codex/praca claude/praca/-x
check "T42 odrzucone: spacja w nazwie" t42 'codex/pra ca' claude/praca

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
