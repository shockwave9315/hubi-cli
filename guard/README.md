# Strażnik limitu Codexa

Pilnuje limitów Codexa, żeby ten nie urwał się w połowie pracy. To skrypt w powłoce, bez AI. Nie uruchamia tur modelu i nie zużywa kredytów ani resetów.

Limity odczytuje `limits.py`. Uruchamia na chwilę `codex app-server` i zadaje jedno pytanie tylko do odczytu, `account/rateLimits/read`. Tekstu z ekranu do odczytu limitów nie używa. Ekran Codexa czyta tylko po to, żeby sprawdzić, czy Codex pracuje. Od v4.3 pewną pracą jest wyłącznie pasek Codexa z czasem i „to interrupt”, np. `Working (5s • esc to interrupt)` albo `Compacting context (54s • esc to interrupt)` (wzorzec `BAR_RE`). Sam ruch w panelu, np. przerysowanie przy podglądzie okna, odsuwa tylko koniec z bezczynności. Nie potwierdza pracy, pauzy ani wznowienia.

## Co robi

| Sytuacja | Reakcja |
|---|---|
| Limit 5 h ≤ 7% (`LOW`) | Jeden raz na okno wkleja Codexowi `LIMIT-5`. Codex kończy zadanie, robi commit, zgłasza pauzę i czeka. |
| Codex po `LIMIT-5` nie pokazuje paska pracy przez 2 obiegi | Pauza potwierdzona. Czas pauzy nie liczy się jako bezczynność. |
| Codex nadal pokazuje pasek pracy 30 min po `LIMIT-5` | Jedna informacja do Claude. Strażnik nie wysyła `LIMIT-5` drugi raz. |
| Codex ruszył w potwierdzonej pauzie (np. obudziła go wiadomość), a limit 5 h jest nadal poniżej 20% (bez odczytu: okno jeszcze się nie odnowiło) | Po dwóch obiegach z paskiem pracy z rzędu wysyła mu ponowną pauzę: stop, commit, czekaj na `<CP> wznów`, bez odpowiedzi. Claude dostaje jedną informację na okno. Najwyżej 3 ponowne pauzy na okno (`REPAUSE_MAX`, licznik w pliku stanu, przeżywa restart), potem tylko ostrzeżenie do Claude. |
| Limit 5 h odnowiony: świeży, pełny odczyt pokazuje 5 h ≥ 20%, tydzień > 1% (`WEEK_LOW`) i `ordinaryUsageAllowed = true`. Minęła godzina resetu (+2 min) albo okno już się zmieniło (np. ręczny reset) | Jeden raz wkleja Codexowi `<CP> wznów`, także gdy Codex akurat pracuje. Po `LIMIT-5` czeka on na tę wiadomość, więc bez niej stanąłby po bieżącej turze. Dotyczy to też sytuacji, w której pauza nie zdążyła się potwierdzić. |
| Po `wznów` | Wznowienie potwierdza dopiero pasek pracy. Bez niego po 10 min jedna informacja do Claude („nie ruszył”). |
| Po resecie limit nadal niski albo nieznany | Nie wznawia. Po 30 min jedna informacja do Claude. |
| Limit tygodniowy ≤ 1% (`WEEK_LOW`, od v4.5 osobny próg) | Codex dostaje `LIMIT-5`: zatrzymaj się, najpierw commit tego, co jest (także niedokończone), potem linia do Claude (v4.6). Claude dostaje informację, a w pliku stanu zapisuje się blokada `WEEKLY_STOP`. Strażnik kończy pracę i nie uruchomi się ponownie, dopóki człowiek nie usunie pliku stanu. |
| Codex stoi na błędzie „■ Selected model is at capacity. Please try a different model.” (ostatni wpis nad polem wpisywania, bez paska pracy), poza pauzą, przy pełnym odczycie i limitach w porządku (v4.7) | Po dwóch obiegach z tym błędem wysyła `<CP> wznów po błędzie`. Następna próba najwcześniej po 5 min. Po 3 próbach bez skutku jedno zgłoszenie do Claude, potem próba co 30 min. Epizod kończą dwa obiegi z paskiem pracy. Postój na błędzie nie liczy się jako bezczynność. W pauzie nic nie wysyła. Gdy wysyłka nie wychodzi, ponawia co obieg, a po 10 min wysyła jedno zgłoszenie do Claude (v4.8). |
| Nie udało się wysłać `LIMIT-5` | Jedna informacja do Claude, ponowna próba w każdym obiegu. Strażnik nie udaje, że wiadomość dotarła. |
| Limitów nie da się odczytać albo odczyt jest niepełny (błąd, brak okna 5 h lub tygodniowego, brak zalogowania) | Nie wznawia i nie zgaduje. Pauzuje tylko wtedy, gdy znany limit jest niski. Po 15 min jedna informacja do Claude. |
| Ekran Codexa nieczytelny | Nie liczy się jako bezczynność, nie potwierdza pauzy i blokuje wysłanie `wznów`. |
| Codex bezczynny 60 min poza pauzą | Strażnik kończy pracę. Codex nie jest zatrzymywany. |
| Sesja Codexa zniknęła (np. restart) | Czeka, aż wróci, i pracuje dalej z nowym panelem. Poza pauzą po 60 min nieobecności kończy pracę i informuje Claude. |
| Po `wznów` limit znowu spada do ≤ 7%, zanim strażnik zobaczył pasek pracy | Zwykły `LIMIT-5` dla nowego okna (v4.4). Ochrona nie zależy od rozpoznania paska. |
| `wznów` należny, ale nie wychodzi (błąd wysyłki, panel przewijany, ekran nieczytelny) | Ponawia w każdym obiegu. Po 10 min jedna informacja do Claude. |
| 24 h działania | Przy pracy (RUNNING) kończy. W pauzie zostaje do wznowienia, a po niepotwierdzonym `wznów` kończy z jedną informacją do Claude. |
| Plik `stop`, sygnał TERM/INT | Kończy pracę. |

Sesje wskazuje się nazwą Hubi, nie numerem panelu: `codex/praca`, `claude/praca`, `claude/hubi-cli/2` (rodzaj/repo[/nazwa sesji]). Numer panelu zmienia się przy każdym restarcie sesji, więc strażnik pyta `hubi6 pane` o bieżący panel w każdym obiegu i tuż przed każdą wysyłką. Codexowi każe pisać do Claude poleceniem `hubi send claude praca -m TEKST`, a nie wklejać do numeru panelu. Tuż przed każdą wysyłką strażnik sprawdza też, że panel żyje, należy do sesji właściwego rodzaju (`-codex-` albo `-claude-`) i że na pierwszym planie jego terminala działa właśnie Codex (`node …/bin/codex`) albo Claude (`…/claude`), a nie np. dowolny skrypt `node`. Jeśli Codex się zamknął i w panelu została zwykła powłoka, nic nie zostanie wysłane. Wiadomości to pojedyncze linie bez backticków. Na jeden panel Codexa działa jeden strażnik (blokada `flock`). Stan zapisuje w pliku `state.<nazwa>` (np. `state.codex-praca`), więc po restarcie strażnika nie wysyła nic drugi raz.

## Instalacja

Strażnik jest częścią repozytorium hubi-cli (katalog `guard/`). Potrzebuje Hubi v6 (`hubi6 pane`, `hubi6 send`), `codex` w `PATH`, `tmux`, `flock`, `python3` i systemd użytkownika.

```bash
bash guard/install.sh            # do ~/.cache/praca/codex-watchdog (inny katalog: pierwszy argument)
```

Skrypt kopiuje tylko brakujące pliki. Gdy w katalogu jest już inny plik o tej samej nazwie (np. Twoja zmiana), nic nie kopiuje i to zgłasza. Działającego strażnika nie rusza. `watchdog.sh` przełączony na inną zachowaną wersję (z repozytorium albo z katalogu strażnika) nie jest konfliktem, a różny `README.md` zostaje bez zmian. Gdy kopiowanie się nie uda, skrypt kończy się błędem i mówi, przy którym pliku. Inny katalog strażnika ustawisz w Hubi w `[guard] dir`.

Pliki:

| Plik | Co to |
|---|---|
| `watchdog.sh` | strażnik, który się uruchamia (kopia jednej z wersji poniżej) |
| `watchdog.v49.sh`, `watchdog.v48.sh`, `watchdog.v4.sh` | zachowane wersje; przełączasz je poleceniem `hubi guard version` |
| `limits.py` | odczyt limitów Codexa, tylko do odczytu |
| `reset_credit.py` | zużycie jednego resetu limitu, tylko z plikiem zgody `reset.armed` |
| `tests/run.sh` | testy na atrapach |

W katalogu strażnik sam tworzy: `log` (czas UTC), `state.<codex>` (stan, np. `state.codex-praca`), `lock.<codex>`, a po użyciu zgody `reset.used.<czas>`.

## Uruchomienie i sterowanie (Hubi)

Najprościej z Hubi: w menu klawisz `s` (stan, log, klawisze `u` uruchom, `z` zatrzymaj, `p` CP, `w` Codex i kontakt, `g` progi, `v` wersja, `l` limity) albo w konsoli:

```bash
hubi guard start CP21 codex/praca claude/praca   # pierwszy start: CP, pilnowany Codex, kontakt
hubi guard start                                 # kolejne: ostatnie ustawienie
hubi guard status [--limits]                     # stan, progi, wersja, godzina wznowienia, log (czas polski)
hubi guard cp CP22                               # nowy CP (tylko w RUNNING)
hubi guard who codex/praca claude/praca/sh       # inny Codex albo kontakt
hubi guard limits 7 1 [--idle 720] [--max-hours 48]   # progi pauzy 5 h i stopu tygodniowego
hubi guard version v4.8                          # inna zachowana wersja
hubi guard stop
```

Nazwy sesji są jak w Hubi: `rodzaj/repo[/nazwa]`. Bez nazwy chodzi o sesję „1”, np. `claude/praca` to `claude/1` w repo praca. Kontaktem może być też inny, nazwany Codex (np. `codex/praca/temp-rev`), ale nie ten pilnowany. Numerów paneli (`%5`) Hubi nie przyjmuje.

Hubi uruchamia strażnika jako osobną jednostkę systemd (`praca-codex-watchdog`). Przeżywa ona restart sesji i podmianę Hubi, ale nie restart devboksa. Restart zachowuje `PATH`, wszystkie `WD_…` i opcje restartu. Zmiana CP albo Codexa w pauzie jest odrzucana, bo zgubiłaby stan pauzy. Gdy nowe ustawienie nie wystartuje, Hubi wraca do poprzedniego. Hubi nigdy nie tworzy `reset.armed`, nie uruchamia `reset_credit.py` i nie pisze do Codexa.

## Uruchomienie bez Hubi

```bash
cd ~/.cache/praca/codex-watchdog
# Restart=on-failure: po awarii systemd podnosi strażnika po 30 s; stan wraca z pliku.
# Bez restartu przy kodach 1 (już działa), 2 (złe wywołanie), 3 (blokada tygodniowa) i przy zwykłym końcu.
systemd-run --user --unit=praca-codex-watchdog --collect --same-dir --setenv=PATH="$PATH" \
  --setenv=WD_LOW=7 --setenv=WD_WEEK_LOW=1 --setenv=WD_IDLE_MIN=720 \
  -p Restart=on-failure -p RestartSec=30 -p RestartPreventExitStatus="1 2 3" \
  -- bash ./watchdog.sh CP21 codex/praca claude/praca

systemctl --user status praca-codex-watchdog   # czy działa
cat state.codex-praca                          # stan
tail log                                       # log, czas UTC
python3 -I limits.py                           # bieżące limity (tylko odczyt, uruchamia codex app-server na kilka sekund)
systemctl --user stop praca-codex-watchdog     # wyłączenie (albo: touch stop, koniec w ciągu jednego obiegu)
```

Kody wyjścia: 1 strażnik dla tego Codexa już działa, 2 złe argumenty albo zły próg, 3 blokada tygodniowa.

**Restart Codexa albo Claude:** nic nie trzeba robić. Strażnik sam znajdzie nowy panel.

**Nowy CP:** zmieniaj tylko w RUNNING. W pauzie strażnik trzyma stan pod nazwą CP, więc nowa nazwa gubi pauzę i Codex nie dostanie `wznów`. Przy serii CP z review między nimi ustaw `WD_IDLE_MIN=720` (720 minut). Wtedy strażnik nie kończy pracy, gdy Codex godzinami czeka na review albo na właściciela.

**Ręczne wznowienie po limicie tygodniowym:** gdy limit wróci, wpisz Codexowi `<CP> wznów`, usuń plik `state.codex-praca` (to zdejmuje blokadę `WEEKLY_STOP`) i włącz strażnika ponownie. Z blokadą strażnik odmawia startu z kodem 3.

**Gdy coś pójdzie nie tak:** zatrzymaj strażnika i ręcznie wpisz Codexowi, co ma zrobić. Plik `state.codex-praca` możesz usunąć, jeśli chcesz zacząć od czystego stanu.

## Ustawienia

Każde ustawienie podajesz zmienną `WD_…` przy starcie (domyślne wartości są na górze `watchdog.sh`). Progi zmienisz też poleceniem `hubi guard limits`.

- `WD_LOW` — próg pauzy 5 h: `LIMIT-5`, gdy limitu 5 h zostało tyle % albo mniej, domyślnie 7;
- `WD_WEEK_LOW` — próg stopu tygodniowego, domyślnie 1. Próg spoza 0–100 (np. `1%`) kończy start kodem 2;
- `WD_RESUME_MIN` — wznowienie dopiero, gdy limit 5 h ma tyle % albo więcej, domyślnie 20. `WD_LOW` musi być niżej, inaczej Codex dostanie pauzę zaraz po wznowieniu;
- `WD_IDLE_MIN` — koniec po tylu minutach bezczynności Codexa poza pauzą, domyślnie 60;
- `WD_MAX_HOURS` — najdłuższy czas pracy, domyślnie 24 (w pauzie strażnik zostaje do wznowienia);
- `WD_POLL` — co ile sekund sprawdza limity i panel, domyślnie 120;
- `WD_RESUME_DELAY` — sekundy po godzinie resetu, zanim spróbuje wznowić, domyślnie 120;
- `WD_UNKNOWN_ALERT_MIN`, `WD_PAUSE_CONFIRM_MIN`, `WD_RESUME_CONFIRM_MIN` — po ilu minutach zgłasza problem;
- `WD_CAP_RE`, `WD_CAP_RETRY_MIN` (5), `WD_CAP_MAX` (3), `WD_CAP_SLOW_MIN` (30) — rozpoznanie błędu „model at capacity” i odstępy między wznowieniami;
- `WD_REPORT_TO` — nazwa Hubi, którą teksty „wznów” podają Codexowi dla zgłoszeń, STOP i linii gotowości (np. `codex/praca/temp-rev`). Puste oznacza kontakt. Linia pauzy z `LIMIT-5` i zgłoszenia strażnika zawsze idą do kontaktu;
- `WD_CODEX_ARGS_RE`, `WD_CLAUDE_ARGS_RE` — wzorzec polecenia procesu na pierwszym planie panelu (Codex: `node …/bin/codex`, Claude: `…/claude`). Podgląd: `ps -t <tty panelu> -o pid,stat,args`;
- `WD_DIR`, `WD_TMUX_SOCK`, `WD_PANE_CMD`, `WD_SEND_CMD`, `WD_LIMITS_CMD` — katalog, gniazdo tmux Hubi i polecenia (domyślnie `~/.local/bin/hubi6 pane`, `hubi6 send`, `limits.py`);
- rzadziej używane: `WD_REPAUSE_MAX` (3), `WD_BAR_RE`, `WD_ACTIVE_SEC` (30), `WD_RESET_AT` (1), `WD_RESET_CMD`, `WD_RESET_TRIES_MAX` (1), a do testów `WD_DRYRUN`, `WD_MAX_POLLS` i `WD_…_SEC`. Opis każdej jest na górze `watchdog.sh`.

Teksty wiadomości są w liniach z `send_to`.

## Testy

```bash
bash guard/tests/run.sh                                     # atrapy limitów i hubi6 pane, osobny serwer tmux
WD_UNDER_TEST=$PWD/guard/watchdog.v48.sh bash guard/tests/run.sh   # inna wersja
```

Testy nic nie wysyłają do prawdziwych paneli i kończą się wierszem `PASS=… FAIL=0`. Uruchamiaj je z krótkim `TMPDIR` (domyślne `/tmp` jest dobre): przy długiej ścieżce gniazdo tmux się nie tworzy i prawie wszystkie testy padają. W hubi-cli uruchamia je też `tests_v6/test_guard_sources.py` (w laboratorium).

## Wersje

- **v4.9** (23.10.2026, obecna): `WD_REPORT_TO`; kontaktem może być nazwany Codex (np. reviewer `codex/praca/temp-rev`).
- **v4.8** (22.10.2026): błąd „model at capacity” rozpoznawany na całym ostatnim wpisie nad polem wpisywania; wznów po błędzie, który nie wychodzi, daje jedno zgłoszenie. Kontaktem może być tylko Claude.
- **v4.7**: po błędzie „Selected model is at capacity” sam wysyła `<CP> wznów po błędzie`, z odstępami.
- **v4.6**: tygodniowy `LIMIT-5` każe najpierw zrobić commit tego, co jest, bez kończenia zadania.
- **v4.5**: osobne progi: pauza 5 h przy `WD_LOW`, stop tygodniowy przy `WD_WEEK_LOW`; zły próg kończy start kodem 2.
- **v4.4**: `LIMIT-5` działa też zaraz po `wznów`; 24 h nie kończy strażnika w pauzie.
- **v4.3**: pracę rozpoznaje po pasku Codexa; po odnowieniu limitu zawsze jedno `wznów`.
- **v4.1**: ponowna pauza, gdy Codex ruszył w pauzie przy niskim limicie.
- **v4** (21.10.2026, `watchdog.v4.sh`): sesje po nazwach Hubi zamiast numerów paneli. Starsze wywołanie z numerami (`CP17 %5 %8`) nadal działa, ale po restarcie sesji przestaje trafiać. Plik stanu nowszych wersji czyta bez straty pauzy.

## Reset limitu (tylko za zgodą właściciela)

Plik `reset.armed` w katalogu strażnika to zgoda na **jeden** reset „Weekly + 5 hr”. Strażnik użyje go przy limicie tygodniowym ≤ 1% (`WD_RESET_AT`) i zamiast pauzy tygodniowej pozwoli Codexowi pracować dalej. Wtedy wywołuje `reset_credit.py`, który zużywa jeden kredyt resetu (z kluczem idempotencji, więc ponowienie nie zużywa drugiego). Po użyciu plik przechodzi w `reset.used.<czas>`. Bez pliku obowiązuje zwykła pauza tygodniowa. Plik tworzy tylko człowiek; Hubi nigdy.
