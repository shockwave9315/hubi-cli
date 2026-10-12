# Hubi v5 — audit, profil wydajności, testy adversarial i plan v6

Data: 2026-10-10 · Host: `ai-devbox` · Audytowany commit: `def0c84` · Kod nie został zmieniony.

Wszystkie liczby w tym raporcie pochodzą z pomiarów wykonanych w trakcie audytu. Tam, gdzie coś jest wnioskiem, a nie pomiarem, jest to zaznaczone słowem „wniosek".

---

## 1. Executive verdict

**V5 NEEDS SMALL HARDENING**

- Nie znalazłem żadnego P1: w domyślnym, wspieranym przepływie v5 nie zabija cudzych procesów i nie gubi pracy. Rdzeń runtime jest solidny (136/136 testów repo w izolacji, 25/25 odzyskań po losowym SIGKILL w trakcie startu, wszystkie wyścigi stop/start/takeover kończą się fail-closed).
- Są dwa P2, oba z poprawką na kilka linii: wykonanie polecenia przez nazwę katalogu repo (F1) oraz możliwość położenia wspólnego serwera tmux z wnętrza dowolnego workloadu bez samoczynnego powrotu usługi (F2).
- Problem płynności UI jest realny i w pełni zdiagnozowany (sekcje 6–7). Jest architektoniczny (N+1 subprocessów), nie językowy.

**PYTHON V6 RECOMMENDED** — z zastrzeżeniem, które jest częścią werdyktu:

- Sam lag **nie** uzasadnia rewrite'u. Ten sam ekran główny z batchowaniem w Bashu renderuje się w 35 ms zamiast 682 ms (pomiar, sekcja 6.6).
- Rewrite uzasadnia kierunek produktu: model snapshotu jako fundament unified workload view, GitHub browser (JSON, cache, praca w tle), testowalność logiki bez żywego systemd/tmux oraz usunięcie klas błędów znalezionych w tym audycie (F1, F5, F6, F7, F10).
- Jeśli zakres miałby się ograniczyć do „napraw lag", właściwą decyzją byłoby v5.1 w Bashu, bez przepisywania.

---

## 2. Exact baseline

| | |
|---|---|
| Repo root | `/home/dev/repos/hubi-cli` |
| Branch / HEAD | `main` / `def0c84d4e593755371acb09860fbfbbdb9116c2` (merge PR #7, 2026-09-12) |
| Stan | czysty przed i po audycie; brak tagów; 38 commitów |
| Wersja | `HUBI_VERSION=5`; brak `--version`, brak identyfikatora commitu w binarce |
| Kod | jeden plik Bash `hubi`: 1967 linii, 98 funkcji, 190 command substitutions, 89 wywołań `tmux_cmd`, 75× `\|\| true`, 85× `2>/dev/null` |
| Testy | 4857 linii, 136 testów zachowania w 11 harnessach |
| Instalacja | `~/.local/bin/hubi`, `~/.config/hubi/tmux-server.conf`, `~/.config/systemd/user/hubi-tmux.service` — **bajt w bajt identyczne z HEAD** |
| Dryf instalacji | `~/.tmux.conf` różni się od `tmux.conf` z repo (starszy wariant `remain-on-exit`); blok autologin w `~/.bashrc` to starsza wersja (zmienna `HUBI_RC` nie jest czyszczona, brak komentarza recovery) |
| Środowisko | Debian 13 (trixie) w LXC, kernel 7.0.14-12-pve, 4 vCPU, 10 GB RAM; bash 5.2.37, tmux 3.6b, systemd 257, Python 3.13.5, git 2.47.3, gh 2.46.0 |
| Polityki | `Linger=yes`, `KillUserProcesses=no`, `sshd AcceptEnv LANG LC_*` |

---

## 3. Architecture as implemented

To jest model wynikający z kodu i z obserwacji runtime, nie z README.

**Topologia procesów**

```
user@1000.service (linger)
└─ app.slice
   ├─ hubi-tmux.service          tmux -D -S $XDG_RUNTIME_DIR/tmux-$UID/hubi   (Restart=on-failure)
   ├─ hubi-codex-<h12>.scope     agent primary      (systemd-run --scope z wnętrza panelu)
   ├─ hubi-codex-<h12>-<inst>.scope   instancja dodatkowa
   ├─ hubi-claude-<h12>[-<inst>].scope
   └─ hubi-terminal-<h12>-<inst>.scope   persistent terminal (bash)
session-N.scope (logind, SSH)    launcher hubi + klienci tmux — warstwa jednorazowa
```

**Tożsamość.** `h12 = sha256(repo_key)[:12]`, gdzie `repo_key` to ścieżka *względem* `$HUBI_REPOS`. Nazwa sesji tmux: `hubi-<agent>-<slug>-<h12>`. Nazwa scope zależy wyłącznie od (typ, `repo_key`, instancja) — nie od socketu, usługi ani ścieżki bezwzględnej.

**Stan.** Hubi nie ma własnego trwałego stanu. Prawda jest rozłożona na dwa źródła, odpytywane osobnymi procesami:

- opcje sesji tmux: `@hubi-managed=v5`, `@hubi-agent`, `@hubi-kind`, `@hubi-repo`, `@hubi-instance`, `@hubi-scope`, `@hubi-pane`,
- nazwy i `ActiveState` jednostek w `systemd --user`.

Jedyny zapis na dysk to pliki blokad `flock` w `$XDG_RUNTIME_DIR/hubi-locks-$UID` (nigdy nie sprzątane; 15 na live).

**Start workloadu** (`ensure_agent_session_locked`, `start_terminal_locked`): preflight (bus, Linger, `--kill-whom`, własność serwera: `MainPID` = PID z socketu = cgroup z `/proc`, dwa snapshoty) → blokada `flock` per (typ, repo, instancja) → pod blokadą nowy proces `bash hubi __start-locked` → `tmux new-session -d -c <repo> -- sleep 30` → metadane → `respawn-pane -k -- systemd-run --user --scope --collect --unit=<scope> -- <agent>`.

**Stop**: Ctrl+C do panelu (tylko agent) → do 3 s → `systemctl --user kill --kill-whom=all --signal=TERM <scope>` → do 2 s → `KILL` → do 2 s → `tmux kill-session -t =<nazwa>`. Przed TERM i przed KILL sprawdzany jest `InvocationID` scope'a.

**Sprzątanie sierot**: jeśli przy starcie scope jest aktywny, a sesji o wyliczonej nazwie nie ma *na tym sockecie*, Hubi zabija scope bez pytania („bezpieczne czyszczenie przed restartem").

**Selektory zabijania** (zweryfikowane statycznie, linia po linii): Hubi nigdy nie sygnalizuje po PID. Istnieje dokładnie jedno miejsce wysyłające sygnał (`hubi:1144`, cały cgroup po dokładnej nazwie scope) i `tmux kill-session` zawsze z dopasowaniem dokładnym `=nazwa`. Nie ma `eval`, `pkill`, globów w `systemctl`.

**UI**: pętla `clear → zbierz dane → printf` z wejściem liniowym (`read -r` + Enter) i kwarantanną wklejania. Każdy wiersz menu jest liczony synchronicznie tuż przed wydrukowaniem.

**Rozbieżności dokumentacja ↔ kod ↔ runtime**

| Dokumentacja | Rzeczywistość |
|---|---|
| Lista stanów: STOPPED, RUNNING, ATTACHED, EXITED, ORPHANED | Kod ma jeszcze `⚠ STALE SESSION`, `⚠ EXITED / STALE SCOPE`, `⚠ SYSTEMD STATE UNKNOWN` |
| „Harnessy są izolowane, nigdy nie adresują istniejących scope'ów" | Działają w prawdziwym `systemd --user` wywołującego i w jego katalogu blokad; izolacja opiera się wyłącznie na unikalności nazw (F4, F13) |
| „Sesje" pokazują sesje | Pokazują surowe nazwy sesji tmux posortowane alfabetycznie; terminale są na liście, ale zawsze na końcu (po wszystkich `hubi-claude-*` i `hubi-codex-*`), bez typu, repo i akcji innych niż attach |
| „Nie jest wymagana zmiana `.bashrc`" | Zainstalowany blok autologin jest starszy niż szablon w repo |
| `hubi doctor` jako gotowość | Doctor sprawdza `exit-empty`, preflight tworzenia już nie (F3) |

---

## 4. Live-runtime safety

**Wykryty runtime ownera** (tylko odczyt): `hubi-tmux.service` aktywna od 2026-10-05 15:48 UTC, MainPID 172, 0 restartów; 6 sesji i 6 scope'ów — Pompa-next (codex primary, claude primary, codex `c`), Inpost-Paczkomaty (codex, claude), hubi-cli (claude); 1 klient; 0 terminali; 7 repozytoriów.

**Istotny fakt:** sesja, w której wykonywałem audyt, sama działa w live runtime — cgroup `hubi-claude-83de655a3d27.scope`, `TMUX=/run/user/1000/tmux-1000/hubi,172,32`. Gołe `tmux kill-server` z mojej powłoki zabiłoby wszystkie 6 sesji ownera. To jest dokładnie mechanizm z F2.

**Jak chroniłem live**

1. Żadne polecenie zmieniające stan nie zostało wykonane jako `dev` wobec tmux ani `systemctl --user`. Wobec live wykonałem wyłącznie zapytania: `systemctl --user show/list-units`, `tmux list-sessions/list-panes/list-clients/show-options` (zawsze z `env -u TMUX -u TMUX_PANE` i jawnym `-N -S`), `hubi doctor`, 7 renderów menu głównego bez wysłania jakiegokolwiek klawisza (po renderze SIGHUP do launchera) oraz odczyty `git` z `GIT_OPTIONAL_LOCKS=0`.
2. Przed pierwszym testem zapisałem odcisk runtime: `InvocationID` i `MainPID` usługi, `InvocationID` każdego scope'a, id i czas utworzenia każdej sesji, PID każdego panelu, inode socketu. Porównywałem go dwukrotnie w trakcie i raz na końcu.

**Izolacja testowa**: osobny użytkownik Linux `hubiaudit` (uid 1001) z własnym `user@1001.service` (linger), własnym busem, własnym serwerem tmux na `/run/user/1001/tmux-1001/hubi`, usługą pod inną nazwą (`hubiaudit-tmux.service`), własnym `$HOME` z kopią HEAD (`git archive`), syntetycznymi repo i fałszywymi agentami (bez tokenów). Procesy testowe uruchamiałem jako transient service w managerze 1001, więc nie żyły w cgroupach ownera. Slice testowy miał limity (TasksMax 4096, MemoryMax 2G, CPUWeight 40).

Docker odrzuciłem: nie daje prawdziwego `systemd --user` z delegacją cgroup, więc nie testowałby kluczowych właściwości v5.

**Dlaczego testy nie mogły dotknąć live** — granicę egzekwuje kernel, nie konwencja nazw. Dowód (sondy niedestrukcyjne wykonane z uid 1001):

| Sonda | Wynik |
|---|---|
| `kill -0` na PID 172 i każdy z 20 wylistowanych procesów uid 1000 | EPERM |
| `tmux -S /run/user/1000/tmux-1000/hubi list-sessions` | Permission denied (katalog `0700`) |
| połączenie z busem `/run/user/1000/bus` | odmowa |
| `cgroup.kill`, `cgroup.procs` live scope'ów i usługi | brak prawa zapisu |
| `/home/dev`, katalog blokad live | brak dostępu |
| nazwy live unitów w managerze 1001 | `LoadState=not-found` |

**Ograniczenia izolacji (uczciwie):**

- Launcher testowy działał w transient service, nie w sesji logind po SSH. Scenariuszy „zerwanie SSH/sieci" nie powtarzałem — przeszły u Ciebie live.
- Agenci byli fałszywi (skrypty bash). Zachowanie prawdziwego Codexa/Claude na SIGHUP i Ctrl+C jest wnioskiem, nie pomiarem.
- Prawdziwy zimny start (zimny cache binarek) nie był mierzony, bo wymagałby globalnego `drop_caches` i zaburzyłby live.

---

## 5. Findings

Skala: **P1** utrata pracy lub trafienie cudzego procesu w domyślnym przepływie · **P2** realna wada osiągalna zwykłym użyciem, istotny skutek · **P3** osiągalna przy węższym warunku lub ograniczony skutek · **P4** UX, hardening, dryf.

Klasy: SRB = supported-runtime bug · UX · PERF · HARD = hardening opportunity · OOS = unsupported/out-of-scope.

P1: brak.

### F1 — P2 · SRB (bezpieczeństwo) · Nazwa katalogu repo jest wykonywana jako polecenie przez tmux

- **Reproducer** (wykonany w izolacji): `mkdir ~/repos/'a#(touch MARK2)b' && git -C ~/repos/'a#(touch MARK2)b' init`, potem start Codexa albo terminala w tym repo z Hubi.
- **Expected**: nazwa katalogu jest danymi; nic z niej nie jest wykonywane.
- **Actual**: w katalogu roboczym serwera tmux powstał plik `MARK2` (i `INJECTED_BY_TMUX_FORMAT` w wariancie terminala). Sam start kończy się błędem „tmux nie zachował katalogu repozytorium", ale polecenie już się wykonało. Druga połowa tej samej wady: repo o nazwie `C#Sharp` albo `notes#Home` nie da się nigdy uruchomić (aliasy formatów `#S`, `#H`); `issue#12` działa.
- **Root cause**: argument `-c` w `tmux new-session` jest łańcuchem formatu tmux; `#(...)` uruchamia `/bin/sh` w serwerze.
- **Affected**: `hubi:967` (`ensure_agent_session_locked`), `hubi:1393` (`start_terminal_locked`).
- **Remediation**: escapować `#` → `##` w wartości `-c` (istniejący `wait_for_pane_cwd` zostaje jako weryfikacja) albo odrzucać nazwy z `#`. W v6: jedna funkcja escapująca dla wszystkiego, co trafia do tmux, plus polityka nazw repo.
- **Uwaga o ryzyku**: wymaga katalogu o takiej nazwie bezpośrednio pod `~/repos`. Nazwy z GitHuba mają alfabet `[A-Za-z0-9._-]`, więc nie tą drogą; realny wektor to `git clone` z dziwnego URL-a, rozpakowane archiwum albo katalog utworzony przez agenta.

### F2 — P2 · SRB · Jeden workload może położyć wspólny serwer tmux, a usługa sama nie wraca

- **Reproducer**: w persistent terminalu (albo jako agent) wykonać gołe `tmux kill-server`.
- **Expected**: workload nie ma wpływu na serwer, który utrzymuje pozostałe workloady; a jeśli serwer padnie, usługa wraca.
- **Actual**: `TMUX` jest eksportowane do każdego panelu, więc gołe `tmux` celuje w serwer Hubi. Po `kill-server`: usługa `inactive (dead)`, `Result=success`, brak restartu; fałszywy Codex i bash terminala nie żyją; agent ignorujący HUP zostaje jako `⚠ ORPHANED`; `hubi doctor` FAIL; każdy start odmawia do ręcznego `systemctl --user start hubi-tmux.service`. Menu główne w tym stanie pokazuje „Sesje: 0" i wszystko `○ STOPPED`, bez informacji, że usługa nie działa.
- **Root cause**: (a) wspólny serwer + dziedziczone `TMUX`; (b) `Restart=on-failure` nie reaguje na czyste wyjście (kod 0).
- **Affected**: `systemd/user/hubi-tmux.service:9`; polecenia panelu w `hubi:1012–1014` i `hubi:1429–1431`.
- **Remediation** (obie części sprawdzone w izolacji): uruchamiać workload przez `env -u TMUX -u TMUX_PANE …` — wtedy gołe `tmux kill-server` trafia w domyślny socket i serwer Hubi przeżywa; `Restart=always` — po `kill-server` usługa wstała sama z nowym PID i `exit-empty off`. Dodatkowo: menu powinno pokazywać stan „runtime DOWN" zamiast „STOPPED".
- **Zakres wg REVIEW_POLICY**: zmianę stanu wywołuje nieuprzywilejowany proces Hubi (punkt 2), bez udziału administratora.

### F3 — P3 · SRB · Przeładowanie własnego `~/.tmux.conf` wyłącza niezmiennik `exit-empty off`

- **Reproducer**: `tmux source-file ~/.tmux.conf` z dowolnego klienta sesji Hubi (Twój `~/.tmux.conf` ma `set -g exit-empty on`), potem zatrzymać ostatni workload.
- **Expected**: czas życia serwera nie zależy od liczby sesji.
- **Actual**: `exit-empty=on`; doctor to wykrywa (FAIL), ale preflight tworzenia nie — start nadal działa. Po zatrzymaniu ostatniej sesji serwer wychodzi czysto, usługa `inactive (dead)`, kolejny start odmawia.
- **Root cause**: niezmiennik ustawiany raz przy starcie serwera; `source-file -q ~/.tmux.conf` w `config/tmux-server.conf:4` importuje ustawienie, które go łamie przy każdym przeładowaniu.
- **Remediation**: `Restart=always` (jak w F2) i ponowne `set -g exit-empty off` w preflight (idempotentne, 1 wywołanie). Praca nie ginie — wyzwala się tylko przy zerze sesji.

### F4 — P3 · HARD · Tożsamość scope'a nie jest związana z runtime — drugi runtime tego samego UID zabija cudzy workload

- **Reproducer** (izolacja): runtime A (domyślny) z repo `samename` i działającym Codexem; runtime B = drugi, poprawnie zakotwiczony serwer (`HUBI_TMUX_SOCKET_PATH`, `HUBI_TMUX_SERVICE`, `HUBI_REPOS` na inny katalog z repo o tej samej nazwie); w B start Codexa dla `samename`.
- **Expected**: B nie sygnalizuje workloadu, którego nie utworzył.
- **Actual**: B widzi scope A jako `⚠ ORPHANED`, wypisuje „znaleziono osierocony scope hubi-codex-7bde9026b706.scope; bezpieczne czyszczenie przed restartem", agent A ginie, `InvocationID` scope'a się zmienia, A pokazuje `⚠ EXITED / STALE SCOPE`.
- **Root cause**: nazwa scope = f(typ, hash ścieżki względnej, instancja); sierota = „aktywny scope, którego sesji nie ma na *moim* sockecie"; sprzątanie bez potwierdzenia (`hubi:956–959`, `hubi:1382–1386`).
- **Znaczenie**: w produkcji nieosiągalne bez nadpisania zmiennych. Ale to jest dokładnie kształt harnessów testowych repo (ten sam manager, inny socket; `tests/multi_instance.sh` używa stałych nazw `foo`, `foo:bar`) i będzie to kształt rozwijania v6 obok live v5, jeśli v6 odziedziczy prefiks `hubi-`.
- **Remediation**: v6 — identyfikator runtime w nazwie jednostki i inny prefiks niż v5; sierot nie zabijać bez jawnego potwierdzenia. Testy uruchamiać pod osobnym UID.

### F5 — P3 · SRB · Workload staje się nieosiągalny, gdy jego repo zniknie z listy

- **Reproducer**: uruchomić agenta w repo, potem `mv` repo (albo `rm -rf`, albo uszkodzić `.git/HEAD`, albo `git` znika z PATH / repo ma „dubious ownership").
- **Expected**: działający workload pozostaje widoczny i da się go zatrzymać z Hubi.
- **Actual**: agent działa dalej (cwd podąża za inode), ale menu główne go nie pokazuje (nowa nazwa: STOPPED/STOPPED), `hubi codex <stara-nazwa>` → „repozytorium nie istnieje", a `[s] Sesje` oferuje wyłącznie attach. Nie istnieje żadna ścieżka stop. Po wyjściu agenta sesja EXITED zostaje na zawsze (`remain-on-exit`). Przy braku `git` lista jest pusta z mylącym „Nie znaleziono repozytoriów Git".
- **Root cause**: każda akcja lifecycle jest kluczowana repozytorium, które musi się aktualnie rozwiązywać; widok sesji nie ma akcji.
- **Affected**: `repo_list` (`hubi:497`), `resolve_repo` (`hubi:407`), `sessions_menu` (`hubi:1663`).
- **Remediation**: widok workloadów budowany z inwentarza runtime, nie z listy repo, z akcjami stop/inspect (sekcja 9).

### F6 — P3 · SRB · Nieaktualna tożsamość po usunięciu i odtworzeniu repo pod tą samą nazwą

- **Reproducer**: agent działa w `repo`; `rm -rf repo && git clone … repo`.
- **Expected**: Hubi odróżnia agenta starego checkoutu od nowego repo.
- **Actual**: status dla nowego repo to `● RUNNING`, a cwd agenta to `…/repo (deleted)`. Attach podłącza do agenta pracującego w usuniętym katalogu.
- **Root cause**: metadane zawierają tylko klucz względny; tożsamość `dev:inode` jest sprawdzana wyłącznie przy tworzeniu (`revalidate_repo_for_start`).
- **Remediation**: zapisywać ścieżkę bezwzględną i `dev:inode` przy starcie; przy niezgodności stan `STALE REPO`.

### F7 — P3 · HARD (bezpieczeństwo) · Klucze API trafiają do argv

- **Reproducer**: launcher z `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` w środowisku, start agenta.
- **Actual**: 2 procesy (po jednym na zmienną) z sekretem w argv — najpierw jako `env -u HUBI_ACTIVE tmux … set-environment -t =sesja OPENAI_API_KEY <wartość>`, po exec jako `tmux …`. `/proc/PID/cmdline` ma tryb 444, brak `hidepid` — każdy lokalny użytkownik może to odczytać w oknie kilku milisekund.
- **Affected**: `seed_agent_session_environment`, `hubi:301`.
- **Stan u Ciebie**: uśpione — w `~/.bashrc`/`~/.profile` nie ma eksportów kluczy API.
- **Remediation** (sprawdzone): `tmux source-file -` czyta polecenia ze stdin, więc wartości nie pojawiają się w argv.

### F8 — P3 · HARD · `git status` z menu bierze `index.lock` i może wywrócić zapis agenta

- **Reproducer** (izolacja, stres): pętla `git status --porcelain` w stylu menu projektu równolegle z pętlą `git add && git commit`.
- **Actual**: 18 z 202 commitów nie powiodło się („Unable to create '.git/index.lock': File exists"). Z `GIT_OPTIONAL_LOCKS=0` po stronie statusu: 0 z 108.
- **Affected**: `git_summary` (`hubi:733`), `show_git_status` (`hubi:1657`).
- **Uwaga**: test był stresowy (4000 plików, `touch` przed każdym statusem). W zwykłym użyciu prawdopodobieństwo jest małe, ale mechanizm jest realny i uderza w agenta, nie w Hubi.
- **Remediation**: `GIT_OPTIONAL_LOCKS=0` dla każdego gita uruchamianego przez menu.

### P4

| # | Klasa | Wada | Dowód / miejsce | Kierunek |
|---|---|---|---|---|
| F9 | HARD | Bez `XDG_RUNTIME_DIR` socket spada na `/run/user/$UID`, a katalog blokad na `/tmp/hubi-locks-$UID` (przewidywalna ścieżka, brak sprawdzenia właściciela; dwa launchery mogą nie widzieć swoich blokad) | `hubi:803` | Ten sam fallback co socket; weryfikacja właściciela i trybu |
| F10 | UX | Numeracja `[1]..[9]` zależy od locale klienta (sshd przyjmuje `LANG LC_*`): `C.UTF-8` → Inpost, Pompa, hubi-cli…; `en_US.UTF-8` → hubi-cli, hubinet-ops, Inpost… Nazwa instancji `é1` jest akceptowana w `en_US.UTF-8` i odrzucana w `C.UTF-8`, więc taka instancja jest niewidoczna z drugiego klienta | `sort` w `hubi:507`, regex w `hubi:519` | `LC_ALL=C` dla sortowania i walidacji |
| F11 | SRB | Ślepe zaułki po uszkodzeniu metadanych: `@hubi-managed` ≠ `v5` → start odmawia bezterminowo, stop sprząta tylko scope; terminal z błędnym `@hubi-scope` i martwym scope'em → nie działa ani start („duplicate session"), ani stop. Fail-closed zgodnie z projektem, ale bez ścieżki naprawy w UI i bez wykrycia w doctorze | T8 | Akcja „napraw / usuń niespójną sesję" po potwierdzeniu |
| F12 | UX | Stop persistent terminala zawsze trwa ≥ 2,4 s (interaktywny bash ignoruje TERM → czekanie 2 s → KILL). Start agenta ≈ 1 s (pomiar), z czego ~0,7 s to stała pętla gotowości 10 × (sprawdzenie + `sleep 0.05`) (wyliczenie z kodu) | T11 | Dla terminala najpierw HUP; gotowość zdarzeniowo |
| F13 | HARD | Harnessy działają w prawdziwym managerze i katalogu blokad wywołującego: po jednym przebiegu zostało 8 plików blokad w katalogu runtime; sprzątanie w `tests/run.sh:35` szuka plików o nazwach, których kod nigdy nie tworzy | przebieg zestawu | Osobny UID testowy jako wymóg |
| F14 | UX | Brak nieinteraktywnego API: nie ma `stop`, `list`, `status`, `--version`; `hubi codex X` bez TTY wisi na „Enter, aby kontynuować" | zaobserwowane przy budowie fixture | Podkomendy + `--json` w v6 |
| F15 | UX | Znak nowej linii w nazwie katalogu daje dwa fałszywe wpisy; sekwencje ESC z nazwy są drukowane surowo do terminala; te same klawisze znaczą co innego w sąsiednich menu (`c`/`a` = zakończ w projekcie, = utwórz w instancjach; `n` = następna / nowy; paginacja raz `n/p`, raz `>/<`); drugi wpis wpisany „na zapas" w trakcie wolnego renderu jest odrzucany przez kwarantannę wklejania | T15, przebieg `screens.py` | Sanityzacja wyświetlania; spójna mapa klawiszy |

**Co się nie złamało** (to też jest wynik):

- 2 równoległe stopy: drugi dostaje rc=3 „agent zmienił się" albo czytelny timeout blokady; stan końcowy czysty.
- Start ścigający się z wolnym stopem: timeout blokady po 3 s, brak podwójnego workloadu.
- 8 różnych workloadów równolegle: start 1,23 s, stop 0,29 s, 8/8 poprawnie.
- Stop przy 3 podłączonych klientach + 5 równoczesnych takeover w trakcie: sesja i scope znikają, 0 klientów-duchów.
- 25× SIGKILL grupy procesów launchera w losowym momencie startu: 5 klas stanu pośredniego, 0 anomalii przy kolejnym starcie i stopie.
- 15 szybkich cykli start/stop: 0 błędów, 0 przyrostu jednostek, 0 jednostek `failed`, 0 `tmux-spawn-*`.
- Crash serwera (SIGKILL): `Restart=on-failure` podnosi nowy serwer, sierota jest widoczna i sprzątalna.
- Nazwy repo ze spacją, wiodącym `-`, `%s`, `$(id)`, backtickami, cudzysłowami, globami, Unicode, 200 znaków: start/stop poprawne. Quoting w skrypcie jest szczelny; jedyną dziurą jest format tmux (F1).

---

## 6. Performance profile

Metoda: sterownik na prawdziwym pty ze znacznikiem czasu każdego fragmentu wyjścia; liczba exec przez shimy na PATH; liczba forków z licznika `processes` w `/proc/stat` (minimum z 5 przebiegów — szum jest wyłącznie dodatni). Replika w izolacji odwzorowuje kształt live (7 repo, 5 działających primary, 1 instancja dodatkowa, 1 klient); render repliki (708 ms) różni się od live (677 ms) o 5%.

### 6.1 Ekran główny na Twoim rzeczywistym zestawie (live, tylko odczyt, n=7)

| Zdarzenie | ms od uruchomienia `hubi` |
|---|---|
| pierwszy bajt (czyszczenie ekranu) | 6 |
| baner i nagłówek | 36 |
| wiersz 1 (hubi-cli) | 150 |
| wiersz 2 / 3 | 188 / 226 |
| wiersz 4 (Inpost, 2 agenty) | 422 |
| wiersz 5 | 458 |
| wiersz 6 (Pompa-next, 2 agenty) | 647 |
| wiersz 7 + prompt | 683 |

Mediana do promptu: **677 ms** (672–700). Rozrzut między pierwszym a ostatnim wierszem: **528 ms**.

### 6.2 Koszt jednego renderu (replika)

| Ekran | klawisz → prompt (mediana) | forki | exec | tmux | systemctl | git |
|---|---|---|---|---|---|---|
| MAIN przy starcie | 708 ms | 645 | 347 | 120 | 14 | 7 |
| MAIN po powrocie / po złym klawiszu | 787–790 ms | 644 | 345 | 120 | 14 | 7 |
| PROJECT | 292 ms | 181 | 107 | 44 | 2 | 3 |
| INSTANCES | 595 ms | 434 | 263 | 116 | 5 | 0 |
| TERMINALS (zero terminali) | 220 ms | 139 | 81 | 31 | 1 | 0 |
| SESSIONS | 277 ms | 171 | 101 | 49 | 0 | 0 |

Pełny rozkład exec dla MAIN: `env` 120, `tmux show-option` 65, `tmux display-message` 30, `sha256sum` 28, `cut` 28, `tmux has-session` 19, `systemctl show` 14, `tr` 14, `realpath` 9, `git rev-parse` 7, `tmux list-clients` 5, pozostałe 8.

### 6.3 Ceny prymitywów i funkcji

| Operacja | Czas |
|---|---|
| sam fork `$(:)` | 0,31 ms |
| `tmux_cmd …` (surowy tmux 2,3 ms + `env` + podpowłoka) | 3,0 ms |
| `systemctl --user show <scope>` | 6,3 ms |
| `name_hash` (`printf \| sha256sum \| cut`) | 1,45 ms |
| `git rev-parse --show-toplevel` | 0,96 ms |
| start basha + parsowanie 1967 linii | 6 ms łącznie |
| **`agent_status` dla działającego agenta** | **97 ms** (22 wywołania tmux, 1 systemctl, 3 potoki hashujące) |
| `agent_status` dla zatrzymanego | 18 ms |
| `repo_list` (7 repo) | 25 ms |
| `instance_list` / `terminal_list` (6 sesji) | 210 ms / 145 ms |

### 6.4 Udział w czasie ekranu głównego (~700 ms)

| Składnik | ms | Udział |
|---|---|---|
| 14× `agent_status` (5×97 + 9×18) | 647 | 92% |
| — w tym 120 wywołań tmux | 360 | 51% |
| — w tym 14× `systemctl show` | 88 | 13% |
| — w tym hashowanie nazw przez `sha256sum\|cut\|tr` | 86 | 12% |
| — w tym pozostałe forki i podpowłoki | ~110 | 16% |
| `repo_list` | 25 | 4% |
| start interpretera + parsowanie | 6 | 1% |
| stałe opróżnianie wejścia po każdym klawiszu (`read -t 0.08`) | +80 | przy każdym kolejnym renderze |

### 6.5 Wzorce N+1 i powtórzenia

- Działający agent jest odpytywany 22 razy na jeden wiersz: `session_is_dead` jest liczone trzykrotnie (`agent_status` ×2, `session_status` ×1), `has-session` dwukrotnie, każda z 4 opcji metadanych osobnym procesem.
- Każde wywołanie tmux to 2 forki i 2 exec (`$(effective_hubi_socket)` + `env -u HUBI_ACTIVE tmux`).
- Nazwy sesji i scope'ów są przeliczane przez `sha256sum | cut` przy każdym użyciu: 28 razy na ekran.
- `repo_list`: `find` + (`git rev-parse` + `realpath`) na każdy katalog — 3,1 ms na repo, przy każdym renderze.
- `instance_list` i `terminal_list` iterują po **wszystkich** sesjach serwera, po 4–5 wywołań tmux na sesję, dla każdego repo osobno.
- Zero pamięci między renderami: powrót z podmenu i nieznany klawisz kosztują pełne 644 forki.

### 6.6 Skalowalność

Repozytoria (wszystkie STOPPED):

| Repo | start → prompt | pusty ekran przed banerem | `repo_list` |
|---|---|---|---|
| 1 | 58 ms | 20 ms | 5 ms |
| 3 | 138 ms | 25 ms | 11 ms |
| 9 | 394 ms | 43 ms | 30 ms |
| 50 | 513 ms | 170 ms | 157 ms |
| 200 | 976 ms | 633 ms | 612 ms |
| 1000 | 3485 ms | 3132 ms | 3174 ms |

Sesje na serwerze:

| Sesje | MAIN | PROJECT | INSTANCES | TERMINALS | SESSIONS |
|---|---|---|---|---|---|
| 6 | 792 | 294 | 587 | 220 | 276 |
| 26 | 782 | 297 | 1855 | 1095 | 373 |
| 50 | 798 | 300 | 2700 | 1651 | 377 |

Dane zimne kontra ciepłe (wyrzucony page cache skryptu i repo): 300 ms kontra 300 ms. Brak składnika I/O.

Prototypy pomiarowe — **ten sam ekran, wyjście bajt w bajt identyczne z v5**:

| Wariant | 7 repo / 6 sesji | 200 repo | 1000 repo | exec |
|---|---|---|---|---|
| v5 | 682 ms | 972 ms | 3477 ms | 348 |
| Bash, batch (1 tmux + 1 systemctl) | 35 ms | 59 ms | 76 ms | 13 |
| Python, batch (1 tmux + 1 systemctl) | 26 ms | 26 ms | 30 ms | 2 |
| Python, batch (1 tmux + cgroupfs) | 20 ms | 21 ms | 25 ms | 1 |

Start interpretera: `bash -c :` 0,9 ms; `python3 -c pass` 7,2 ms; Python z typowymi importami stdlib 17–31 ms; sam `import asyncio` 35 ms.

### 6.7 Lifecycle

| Operacja | Czas |
|---|---|
| start agenta | ~0,98 s |
| start terminala | ~0,30 s |
| stop: agent wychodzi na Ctrl+C | 0,12 s |
| stop: agent ignoruje INT | 3,46 s |
| stop: agent ignoruje INT i TERM | 5,69 s |
| stop: persistent terminal | 2,37 s |

---

## 7. Why the UI feels slow

Jednoznacznie: **Hubi rysuje każdy wiersz dopiero po wykonaniu serii synchronicznych subprocessów dla tego wiersza, a tych subprocessów jest 645 na jeden ekran z 7 pozycjami.**

- Wiersz repo z dwoma działającymi agentami kosztuje ~190 ms, wiersz bez agentów ~37 ms. Dlatego na Twoim zestawie wiersze pojawiają się w 150, 188, 226, **422**, 458, **647**, 683 ms — widzisz dokładnie te dwa skoki przy Inpost i Pompa-next.
- Do tego dochodzi stałe 80 ms opóźnienia wejścia po każdym klawiszu oraz pełne przeliczenie przy każdym powrocie do menu.

Czym to **nie** jest:

- Nie interpreter Basha: start i parsowanie to 6 ms; Bash z batchowaniem robi ten ekran w 35 ms.
- Nie dysk: zimne dane = ciepłe dane.
- Nie git: 7 wywołań, ~7 ms.
- Nie sam systemd: 88 ms z 700.

To jest architektura zapytań: brak modelu runtime, każda funkcja statusu pyta system od zera.

---

## 8. Functional gap — GitHub repo browser

**Stan faktyczny Twojego środowiska** (sprawdzone, tylko odczyt):

- `gh` 2.46.0 zalogowany jako `shockwave9315`, protokół HTTPS, zakresy `repo`, `read:org`, `workflow`, `gist`.
- `git` używa `gh auth git-credential` jako credential helpera dla `https://github.com`.
- `gh repo list` zwraca 16 repo (3 prywatne, 7 forków, 0 zarchiwizowanych) w 0,6–1,4 s; 5 kB JSON. Limit API: 5000/5000.
- 7 z 16 jest lokalnie, 9 nie. Wszystkie 7 lokalnych ma origin HTTPS zgodny z GitHubem; nazwy katalogów = nazwy repo.
- SSH do GitHuba istniejącymi kluczami: `Permission denied (publickey)`. Klonowanie po SSH u Ciebie dziś nie działa.

**Rekomendowany model**

| Aspekt | Decyzja | Dlaczego |
|---|---|---|
| Źródło listy | `gh repo list --limit 1000 --json name,nameWithOwner,isPrivate,isArchived,isFork,url,pushedAt,description` | Jedno wywołanie, `gh` sam paginuje; brak własnego klienta HTTP |
| Repo organizacji i współpracy | opcja w configu: `gh api --paginate 'user/repos?affiliation=owner,collaborator,organization_member&per_page=100'` | `gh repo list` zwraca tylko repo właściciela |
| Auth | wyłącznie istniejący `gh`; Hubi nie czyta, nie przechowuje i nie przekazuje tokenu | wymóg ownera; token zostaje w magazynie `gh` |
| Transport clone | `gh repo clone OWNER/NAME <tmp>` (HTTPS przez credential helper; dla forków dodaje `upstream`) | działa dla prywatnych; SSH u Ciebie nie działa |
| git-only bez `gh` | odrzucone jako źródło listy | git nie potrafi wylistować repo konta; `git ls-remote` nadaje się tylko do sprawdzenia dostępu (461 ms) |
| Surowe GitHub API | odrzucone | wymagałoby obsługi tokenu przez Hubi |
| LOCAL / NOT LOCAL | dopasowanie po znormalizowanym URL-u `origin` czytanym wprost z `.git/config` (bez subprocessu) do `nameWithOwner`, bez rozróżniania wielkości liter | łapie repo sklonowane pod inną nazwą katalogu |
| Repo przemianowane na GitHubie | lokalne repo z originem spoza listy pokazywane jako „LOCAL · origin spoza listy"; bez automatycznej zmiany originu | stary URL nadal działa przez redirect; automatyka byłaby zgadywaniem |
| Clone atomowy | klon do `<root>/.hubi-clone-<NAME>-<rand>`, potem `rename` na `<root>/<NAME>`; przy błędzie lub Ctrl+C katalog tymczasowy jest usuwany | częściowy klon nigdy nie pojawia się jako repo |
| Kolizja lokalna | katalog istnieje z innym originem → propozycja `<NAME>-<OWNER>` albo anulowanie; nigdy nadpisanie | |
| Nazwy | walidacja `^[A-Za-z0-9._-]+$`, bez wiodącej `.` i `-`, plus polityka nazw Hubi | domyka F1 dla ścieżki clone |
| Archived / fork / private | znaczniki `[A]` `[F]` `[P]`; filtr; zarchiwizowane klonowalne | |
| Cache | `~/.cache/hubi/github-<host>-<user>.json` z `fetched_at`; TTL 10 min; ekran pokazuje cache natychmiast i odświeża w tle; `[r]` wymusza | lista w 0 ms zamiast 0,6–1,4 s |
| Offline / GitHub niedostępny | baner „offline — dane z HH:MM", lista z cache, clone wyłączony z czytelnym powodem | |
| Auth wygasł (`gh` kod 4) | komunikat z poleceniem `gh auth login`; Hubi nie loguje sam | |
| Zawieszenia | `GIT_TERMINAL_PROMPT=0`, timeout 10 s na listę, clone na pierwszym planie z postępem | brak wiszących promptów o hasło |
| Po clone | repo jest od razu w inwentarzu (snapshot budowany przy każdym renderze); propozycja przejścia do menu projektu | |

**UX**: `[g] GitHub` w menu głównym → tabela `nazwa · LOCAL/— · znaczniki · ostatni push`; filtr tekstowy; Enter na NOT LOCAL → potwierdzenie z docelową ścieżką → postęp → „Otwórz projekt?".

---

## 9. Functional gap — unified workload view

**Co jest dziś** (potwierdzone na 50 workloadach):

- Menu główne: tylko `C:` i `A:` dla instancji primary. Repo z 23 instancjami i 22 terminalami wygląda tak samo jak repo z dwoma agentami.
- `[i] Instancje`: tylko agenci jednego repo. `[t] Terminale`: osobne menu jednego repo.
- `[s] Sesje`: surowe nazwy sesji alfabetycznie, więc terminale są zawsze po wszystkich agentach (strony 5–6 z 6); tylko attach; sieroty (scope bez sesji) nie istnieją w tym widoku.

**Proponowany model danych** — jeden rekord na workload, budowany z jednego snapshotu:

```
Workload
  kind         codex | claude | terminal | (kolejni agenci z configu)
  repo         ścieżka bezwzględna + nazwa wyświetlana + status: ok | missing | replaced
  instance     primary | nazwa
  unit         nazwa scope, InvocationID, stan jednostki: active | inactive | unknown
  session      nazwa sesji, pane_dead, pane_pid, cwd
  clients      liczba (szczegóły: tty, readonly — dociągane na ekranie szczegółów)
  created_at   → wiek
  state        RUNNING | ATTACHED(n) | EXITED | ORPHANED | STALE_SESSION | STALE_SCOPE |
               STALE_REPO | UNKNOWN | UNMANAGED
```

Stan jest czystą funkcją (widok tmux, widok jednostek, widok repo) — jedna tabela decyzyjna zamiast trzech rozjeżdżających się funkcji (`agent_status`, `session_status`, `terminal_status`).

**UX**

```
 WORKLOADY (7)                                         runtime: OK
 #  TYP       REPO               INSTANCJA  STAN            KL  WIEK
 1  codex     Inpost-Paczkomaty  primary    ● RUNNING        0  2d 21h
 2  claude    Inpost-Paczkomaty  primary    ● RUNNING        0  2d 18h
 3  claude    hubi-cli           primary    ● ATTACHED       1  2h
 4  codex     Pompa-next         primary    ● RUNNING        0  3d 23h
 5  codex     Pompa-next         c          ● RUNNING        0  3d 2h
 6  terminal  Pompa-next         build      ● RUNNING        0  5h
 7  claude    (repo usunięte)    primary    ⚠ ORPHANED       –  1d
```

- Sortowanie: repo → typ → instancja. Filtr po typie i repo.
- Wybór pozycji → szczegóły: scope, sesja, `InvocationID`, cwd, liczba procesów w cgroup, lista klientów, akcje dozwolone w danym stanie.
- Akcje: `attach` (gdy 0 klientów), `podgląd` / `współdziel` / `przejmij` (gdy są klienci), `stop` (z potwierdzeniem i tokenem generacji), `inspect`, `restart` (dla EXITED/ORPHANED, z potwierdzeniem).
- Workload jest zatrzymywalny niezależnie od tego, czy jego repo nadal istnieje (zamyka F5).
- Menu główne dostaje skrót per repo: `C:● A:● +1 inst · 1 term`.
- Nagłówek pokazuje stan runtime (`OK` / `DOWN` / `DEGRADED`) zamiast udawać „wszystko STOPPED".

Koszt danych: 1 wywołanie `tmux list-panes -a -F …` + lista aktywnych scope'ów (cgroupfs albo 1 `systemctl list-units`). Zmierzone: 20–26 ms dla całego inwentarza.

---

## 10. Bash v5 vs Python v6

| Obszar | Bash v5 (stan faktyczny) | Python v6 (oczekiwane; pomiar tam, gdzie jest) |
|---|---|---|
| Czas startu | 6 ms | 20–30 ms (stdlib) — gorzej o ~20 ms, nieodczuwalne |
| Render ekranu głównego | 682 ms | 20–26 ms (prototyp, wyjście identyczne) |
| Subprocessy na ekran | 348 exec / 640 forków | 1–2 exec |
| Batchowanie | możliwe (prototyp 35 ms), ale wymaga przebudowy wszystkich funkcji statusu | naturalne: jeden snapshot, słowniki |
| Skalowanie | 3,5 s przy 1000 repo; 2,7 s INSTANCES przy 50 sesjach | 25–30 ms przy 1000 repo |
| Współbieżność | zadania w tle + pliki tymczasowe | pula wątków na `gh` i opcjonalny status gita |
| Async I/O | brak | niepotrzebne dla rdzenia (patrz 11, pytanie 6) |
| Cache | brak; trudny bez struktur danych | snapshot w pamięci na czas renderu; cache GitHuba w JSON |
| Model danych | tablice asocjacyjne z kluczami sklejanymi separatorem; stan w 8 zmiennych globalnych (`FOUND_SESSION`, `RESOLVED_*`…) | dataclasses, enum stanów |
| Parsowanie | `IFS`/`read`, awk; wynik zależny od locale (F10) | jawne separatory, `--output=json` z systemctl i gh |
| Obsługa błędów | 75× `\|\| true`, 85× `2>/dev/null`: awaria tmux jest nieodróżnialna od „opcja nieustawiona" (stąd „STOPPED" przy martwej usłudze) | wyjątki i typy wyników; „nie wiem" jako osobny stan |
| Typowanie | brak | adnotacje + mypy w CI |
| Testy jednostkowe | praktycznie niemożliwe; testy robią `source hubi` i wołają funkcje na żywym systemie | logika stanu i lifecycle na fałszywych adapterach, milisekundy |
| Testy integracyjne | 136 testów, 3 min 28 s, wymagają prawdziwego managera wywołującego (F13) | te same kontrakty jako testy black-box, pod osobnym UID |
| Mockowanie | szwy testowe w kodzie produkcyjnym: 7 podmian binarek systemowych (`HUBI_TMUX_BIN`, `HUBI_SYSTEMCTL_BIN`…) oraz `HUBI_PROC_ROOT`, `HUBI_TMUX_SOCKET_PATH`, `HUBI_TMUX_SERVICE` — każda zmienia zachowanie krytyczne dla bezpieczeństwa | porty/adaptery; szwy testowe poza ścieżką produkcyjną |
| GitHub | wykonalne przez `gh --jq`, ale cache, tło i obsługa błędów są niewygodne | JSON natywnie |
| tmux | 89 miejsc wywołań, każde osobny proces | jeden adapter, jedno zapytanie, jedno miejsce escapowania (F1) |
| systemd | `systemctl show` + parsowanie tekstu | `list-units --output=json`; cgroupfs na ścieżce odczytu |
| Zarządzanie subprocessami | brak timeoutów poza flock | timeout na każdym wywołaniu, argv bez powłoki, redakcja sekretów w logach |
| Maszyna stanów lifecycle | 6 funkcji w dwóch prawie identycznych kopiach (agent / terminal) | jedna implementacja sparametryzowana typem workloadu |
| Blokady | `flock` + ponowne uruchomienie całego skryptu jako worker | `fcntl.flock` w procesie, `O_CLOEXEC` |
| Metadane | opcje tmux, każda czytana osobno | te same opcje, czytane jednym zapytaniem |
| Logowanie / obserwowalność | brak; diagnostyka tylko na ekranie | log strukturalny + tryb `--debug` z czasami wywołań |
| Profilowanie / debug | `set -x`; do tego audytu musiałem budować shimy i sterownik pty | `cProfile`, `pdb`, wbudowany trace |
| Pakowanie | 1 plik, `install -m 755` | pakiet stdlib-only + cienki launcher albo zipapp; wymaga Pythona ≥ 3.11 (jest 3.13.5) |
| Zależności | bash, coreutils, git, tmux, systemd | to samo + python3; zero paczek z PyPI |
| Upgrade | podmiana pliku; działające launchery wołają nowego workera starymi argumentami | wersjonowany katalog + symlink; launcher ładuje kod raz |
| Rollback | `install` poprzedniego pliku | przełączenie symlinku |
| Koszt utrzymania | rośnie nieliniowo: każdy nowy typ workloadu to kolejna kopia start/stop/status/menu | nowy typ = wpis w tabeli |
| Tempo rozwoju | wolne dla funkcji z danymi strukturalnymi | wyższe |
| Ryzyka | znane i przetestowane live | nowy kod w ścieżce bezpieczeństwa; nowe klasy błędów (sekcja 12) |

**Hybryda** (launcher Bash + pomocnik Python zwracający snapshot) rozwiązuje lag, ale rozcina logikę na dwa języki i dwa reżimy testów. Nie rekomenduję. Jedyny element, który z natury zostaje powłoką, to fragment autologin w `.bashrc`.

---

## 11. Python benefits — konkretnie dla tego projektu

Odpowiedzi na 10 pytań:

1. **Co Python poprawi?** Model snapshotu (jeden obraz runtime na render), jedną tabelę stanów zamiast trzech funkcji statusu, jedną implementację lifecycle zamiast dwóch kopii, jedno miejsce escapowania do tmux, odróżnienie „błąd zapytania" od „brak", testy logiki bez żywego systemu, JSON i cache dla GitHuba.
2. **Czego nie poprawi automatycznie?** Kontraktów runtime: własność serwera, granice cgroup, tokeny generacji, fail-closed, kolejność TERM→KILL, wyścigi — to trzeba przenieść świadomie i przetestować tak samo jak w v5. Python nie usuwa też F2/F3 (to konfiguracja usługi i środowisko panelu) ani nie przyspieszy `gh`.
3. **Czy lag wynika z Basha?** Nie. Z architektury wywołań. Dowód: Bash z batchowaniem 35 ms kontra v5 682 ms przy identycznym wyjściu.
4. **Czy Python ograniczy subprocessy?** Tak: 348 → 1–2 na ekran (pomiar). Hashowanie w procesie (`hashlib`), repo przez `os.scandir`, scope'y z cgroupfs.
5. **Snapshot raz zamiast wielu pytań?** Tak, sprawdzone: `tmux list-panes -a -F` zwraca sesje, panele, metadane, liczbę klientów, PID i cwd jednym wywołaniem; lista aktywnych scope'ów to jeden `scandir`.
6. **Czy asyncio ma sens?** Nie dla rdzenia: snapshot to 20 ms synchronicznie, a `import asyncio` kosztuje 35 ms. Współbieżność jest potrzebna w dwóch miejscach — odświeżanie listy GitHuba w tle i opcjonalny status gita dla widocznej strony — i wystarczy do tego mała pula wątków.
7. **Czy uprości GitHub browser?** Tak: JSON, cache, dopasowanie originów, atomowy clone z cleanupem, timeouty, tło.
8. **Czy uprości unified inventory?** Tak — to jest główny zysk. Inwentarz staje się strukturą danych, a widok jej projekcją.
9. **Jakie nowe klasy błędów wniesie?** Sekcja 12.
10. **Czy zysk uzasadnia rewrite?** Tak, jeśli zakres obejmuje obie brakujące funkcje i dalszy rozwój. Nie, jeśli celem jest tylko lag.

Dodatkowo: jedna rzecz, którą sprawdziłem i która **nie** działa — `InvocationID` jako xattr cgroupu (`user.invocation_id`) nie jest dostępny w tym LXC. Token generacji musi pochodzić z systemd (1 wywołanie, tylko przy operacjach destrukcyjnych).

---

## 12. Python costs / risks

| Ryzyko | Konkret | Ograniczenie |
|---|---|---|
| Przepisanie ścieżki bezpieczeństwa | ~2000 linii przetestowanego live kodu lifecycle | Topologia runtime bez zmian; najpierw testy kontraktowe uruchamiane na v5, potem ten sam zestaw na v6 |
| Testy nie przenoszą się 1:1 | większość ze 136 testów robi `source hubi` i woła funkcje Basha | przepisać jako black-box przez CLI (`hubi start/stop/list --json`) |
| Nieobsłużony wyjątek w TUI | traceback na ekranie, terminal w trybie raw | jedna granica błędów w `__main__`, przywracanie termios w `finally`, log |
| Sygnały | `KeyboardInterrupt` w dowolnym punkcie; domyślny SIGHUP zabija bez sprzątania | jawne handlery; operacje lifecycle idempotentne i wznawialne (jak v5 po T21) |
| Deadlock na potokach | `subprocess` z dużym wyjściem bez `communicate` | jeden runner z `capture_output` i timeoutem |
| Dziedziczenie deskryptorów | fd blokady w procesach potomnych trzymałby blokadę | `close_fds=True` (domyślne), `O_CLOEXEC`; test jak w v5 („no lifecycle lock fd leak") |
| Kodowanie | nazwy plików spoza UTF-8 | `surrogateescape`; polityka nazw odrzucająca znaki sterujące |
| Zombie / reaping | klienci tmux uruchamiani przez launcher | `subprocess.run` na pierwszym planie; brak procesów w tle poza pulą |
| Wersja Pythona | aktualizacja systemowego Pythona | tylko stdlib, cel ≥ 3.11; `hubi doctor` sprawdza wersję |
| Pakowanie | więcej niż jeden plik | zipapp albo katalog wersji + symlink; brak pip w ścieżce instalacji |
| Start wolniejszy o ~20 ms | 7 ms interpreter + importy | leniwe importy; bez asyncio |
| Pokusa rozrostu | frameworki TUI, D-Bus, asyncio | zakaz zależności spoza stdlib w v6.0 |
| Okres bez nowych funkcji | CP0–CP3 to odtwarzanie tego, co już działa | P2 z v5 naprawić od razu w v5 (sekcja 18), nie czekać na v6 |

---

## 13. Recommended v6 architecture

**Zasada nadrzędna:** v6 zmienia język i model danych, **nie** zmienia topologii runtime. Jeden serwer tmux będący usługą systemd, jeden scope na workload, kill całego cgroup po dokładnej nazwie, token generacji, fail-closed. Rozważałem serwer tmux per workload (usuwa wspólny punkt awarii z F2), ale odrzucam go dla v6.0: tańsze utwardzenia (`Restart=always`, brak `TMUX` w środowisku workloadu) zamykają praktyczne scenariusze, a zmiana topologii razem ze zmianą języka podwaja ryzyko. Do ponownej oceny po v6.0.

**Moduły**

```
hubi/
  __main__.py     granica błędów, przywracanie terminala, kody wyjścia (98/99/129/130/143 jak v5)
  cli.py          menu (domyślnie) · list [--json] · start · stop · attach · doctor · gh list|clone · version
  config.py       ~/.config/hubi/config.toml: repos_root, tabela agentów (nazwa → argv), opcje GitHuba
  core/
    model.py      RepoRef, WorkloadKey, Workload, RuntimeSnapshot, RuntimeHealth, WorkloadState (enum)
    naming.py     nazwy jednostek i sesji, walidacja nazw, escapowanie dla tmux — jedyne miejsce
    state.py      derive_state(widok tmux, widok jednostek, widok repo) → WorkloadState; czysta funkcja
    inventory.py  build_snapshot(): repo + workloady w jednym przebiegu
    lifecycle.py  start / stop / reconcile; blokady; tokeny generacji; wyłącznie przez porty
    repos.py      odkrywanie repo (scandir, .git), tożsamość dev:inode, origin z .git/config
  adapters/
    proc.py       jeden runner: argv bez powłoki, timeout, redakcja sekretów w logu
    tmux.py       jedno zapytanie o inwentarz; tworzenie sesji; metadane i env przez stdin; attach
    systemd.py    run scope · kill cgroup · stan + InvocationID · weryfikacja własności serwera
    cgroupfs.py   szybki odczyt aktywnych scope'ów (tylko do wyświetlania)
    git.py        branch/dirty z --no-optional-locks; clone
    github.py     gh list (JSON), gh clone, cache
  tui/
    input.py      czytnik odporny na wklejanie (semantyka v5: linia + Enter, kwarantanna bracketed paste)
    screens.py    main · project · workloads · workload detail · github — tylko projekcja snapshotu
  doctor.py       wspólne sprawdzenia preflight i doctor (w tym exit-empty, dryf unitu, binarki, wersja)
  log.py          ~/.local/state/hubi/hubi.log, rotacja; --debug = czasy każdego wywołania
tests/
  unit/           logika na fałszywych adapterach
  contract/       kontrakty runtime na prawdziwym tmux + systemd, pod osobnym UID
```

**Granice**

- `core` nie importuje `adapters` ani `tui`; zna tylko protokoły portów. To czyni logikę stanu i lifecycle testowalną bez systemu.
- `tui` nie wykonuje akcji; woła `lifecycle` i rysuje snapshot.
- Decyzja o zabiciu nigdy nie opiera się na danych z cgroupfs ani z cache — tylko na świeżym zapytaniu do systemd z `InvocationID`.

**Model runtime**

| Element | v5 | v6 |
|---|---|---|
| Usługa serwera | `hubi-tmux.service`, `Restart=on-failure` | `hubi6-tmux.service`, `Restart=always` |
| Socket | `$XDG_RUNTIME_DIR/tmux-$UID/hubi` | `$XDG_RUNTIME_DIR/hubi6/tmux.sock` |
| Scope | `hubi-<typ>-<h12>[-<inst>].scope`, hash ścieżki względnej | `hubi6-<typ>-<h12>-<inst>.scope`, hash = sha256(ścieżka bezwzględna repo) |
| Opis jednostki | generowany przez systemd-run | `--description` z typem, instancją i ścieżką repo → sierota sama się opisuje |
| Środowisko workloadu | allowlista przez argv; `TMUX` dziedziczone | allowlista przez stdin; `TMUX`, `TMUX_PANE` usunięte |
| Sieroty przy starcie | zabijane bez pytania | pokazane; kill po jawnym potwierdzeniu (CLI: `--replace-orphan`) |
| Stop terminala | TERM → 2 s → KILL | HUP → TERM → KILL |
| Blokady | `flock` + worker, `$XDG_RUNTIME_DIR` lub `/tmp` | `fcntl.flock` w `$XDG_RUNTIME_DIR/hubi6/locks`, bez fallbacku do `/tmp` |
| `exit-empty off` | przy starcie serwera | przy starcie + w każdym preflight |

Inny prefiks (`hubi6-`) nie służy migracji — służy temu, żeby v6 w trakcie rozwoju i po rollbacku nigdy nie uznał scope'a v5 za swoją sierotę (F4).

**Model stanu**

- Brak trwałego stanu runtime — jak w v5. Dwa autorytatywne źródła (tmux, systemd) czytane w jednym snapshocie.
- Metadane w opcjach sesji tmux (`@hubi6-*`), rozszerzone o ścieżkę bezwzględną repo i `dev:inode`.
- Na dysku tylko: config (TOML), cache GitHuba (JSON, odtwarzalny), log.
- Każdy render = nowy snapshot. Żadnego cache stanu runtime między renderami — przy 20 ms nie jest potrzebny, a eliminuje całą klasę „stale state".

---

## 14. v6 implementation plan

Każdy checkpoint jest osobno implementowalny, recenzowalny, testowalny i akceptowalny.

### CP0 — Izolowane środowisko testowe i kontrakty runtime

- **Scope**: skrypt tworzący i usuwający dedykowanego użytkownika testowego z własnym `systemd --user`; zestaw testów kontraktowych black-box opisujących zachowanie v5 (lista z sekcji 17); zapis decyzji o nazwach v6.
- **Wejście**: ten raport zatwierdzony.
- **Wyjście**: `make contract` działa pod UID testowym; uruchomione na v5 przechodzi (tam, gdzie v5 ma CLI — pozostałe kontrakty oznaczone jako „do weryfikacji na v6").
- **Acceptance**: sondy izolacji jak w sekcji 4 zielone; odcisk live identyczny przed i po; żaden test nie uruchamia się jako `dev`.
- **Poza scope**: jakikolwiek kod v6.

### CP1 — Rdzeń: model, nazwy, stan, config

- **Scope**: `core/model.py`, `naming.py`, `state.py`, `config.py`; testy jednostkowe.
- **Wejście**: CP0.
- **Wyjście**: tabela stanów pokrywająca każdą kombinację (sesja?, pane_dead?, jednostka active/inactive/unknown, repo ok/missing/replaced).
- **Acceptance**: 100% gałęzi `derive_state`; testy escapowania z ładunkami `#(…)`, `#{…}`, `#S`; walidacja nazw niezależna od locale; mypy strict czysty.
- **Poza scope**: jakiekolwiek wywołanie tmux/systemd.

### CP2 — Adaptery odczytu, inwentarz, doctor

- **Scope**: `adapters/{proc,tmux,systemd,cgroupfs}.py` (tylko odczyt), `inventory.py`, `repos.py`, `hubi list --json`, `hubi doctor`.
- **Wejście**: CP1.
- **Wyjście**: snapshot runtime jako JSON.
- **Acceptance**: ≤ 2 exec i ≤ 50 ms dla 7 repo / 6 workloadów; ≤ 100 ms dla 1000 repo / 50 workloadów; awaria tmux lub systemd daje stan `UNKNOWN` / `runtime DOWN`, nigdy `STOPPED`; doctor wykrywa `exit-empty on`, dryf unitu, brak binarek.
- **Poza scope**: tworzenie i zatrzymywanie czegokolwiek.

### CP3 — Lifecycle

- **Scope**: `lifecycle.py` + adaptery zapisu: start, stop, reconcile dla agentów i terminali; blokady; tokeny generacji; `hubi start|stop`.
- **Wejście**: CP2.
- **Wyjście**: v6 tworzy i zatrzymuje workloady w runtime testowym.
- **Acceptance**: wszystkie kontrakty z sekcji 17 zielone pod UID testowym; nowe testy dla F1, F2, F4, F5, F6, F7; fuzz „SIGKILL w losowym punkcie startu" ≥ 100 iteracji bez anomalii; brak sekretu w argv; brak wycieku fd blokady.
- **Poza scope**: UI, attach.

### CP4 — Attach i TUI

- **Scope**: tryby attach (podgląd / współdziel / przejmij), `tui/`, unified workload view, czytnik wejścia z semantyką v5.
- **Wejście**: CP3.
- **Wyjście**: pełny odpowiednik interaktywny v5 + widok workloadów.
- **Acceptance**: testy na pty przeniesione z `adversarial.py` (wklejanie, strzałki, sygnały, kody 98/99/129/130/143); klawisz → prompt ≤ 100 ms p95 na replice; wiersze ekranu pojawiają się w jednym zapisie; terminale i agenci w jednym widoku; workload bez repo da się zatrzymać.
- **Poza scope**: GitHub.

### CP5 — GitHub browser

- **Scope**: `adapters/github.py`, `git.py` (clone), ekran GitHub, cache.
- **Wejście**: CP4.
- **Wyjście**: lista LOCAL / NOT LOCAL, clone do repo root.
- **Acceptance**: testy na fałszywym `gh`: paginacja, brak sieci, auth wygasł, kolizja nazw, przerwany clone nie zostawia katalogu, cache po TTL; Hubi nie czyta tokenu; lista z cache w < 50 ms.
- **Poza scope**: tworzenie repo, PR-y, inne hosty niż github.com.

### CP6 — Pakowanie, instalacja, runbook

- **Scope**: instalator (katalog wersji + symlink), unit `hubi6-tmux.service`, skrypt cutover i rollback, dokumentacja.
- **Wejście**: CP5.
- **Wyjście**: instalacja i rollback wykonane na sucho pod UID testowym.
- **Acceptance**: cutover i rollback przechodzą od zera na użytkowniku testowym; `hubi doctor` PASS po instalacji; `hubi version` podaje commit.
- **Poza scope**: instalacja u ownera.

### CP7 — Cutover na devboxie

Sekcja 15. Wykonuje owner albo agent na jego wyraźne polecenie.

---

## 15. Clean cutover plan

Bez migracji aktywnych sesji.

1. **Praca w v5 zakończona.** Owner potwierdza, że nic nie musi przetrwać.
2. **Zatrzymaj wszystkie workloady v5** z menu Hubi (każdy `[c]`/`[a]`/`[x]` z potwierdzeniem).
3. **Zweryfikuj zero-state:**
   ```
   hubi doctor                                    # managed scopes: (none)
   tmux -N -S "$XDG_RUNTIME_DIR/tmux-$UID/hubi" list-sessions   # brak sesji
   systemctl --user list-units 'hubi-*.scope' --all             # 0 jednostek
   ```
4. **Wyłącz runtime v5:** `systemctl --user disable --now hubi-tmux.service`. Pliki v5 zostają na dysku (rollback).
5. **Zainstaluj v6** (instalator z CP6): unit `hubi6-tmux.service`, pakiet, symlink `~/.local/bin/hubi`.
6. **`hubi doctor`** — musi być PASS.
7. **Smoke test:** utwórz terminal i agenta w jednym repo, attach, detach, stop; sprawdź widok workloadów.
8. **Start świeżych workloadów.**

Konwersja configu: v5 nie ma pliku konfiguracyjnego. v6 przy pierwszym uruchomieniu zapisuje `config.toml` z `repos_root = ~/repos` i dwoma agentami o argv identycznym z v5. Nic więcej do przeniesienia nie ma.

---

## 16. Rollback

1. Zatrzymaj workloady v6; `systemctl --user disable --now hubi6-tmux.service`.
2. Przywróć binarkę v5: `install -m 755 ~/repos/hubi-cli/hubi ~/.local/bin/hubi` (z commitu `def0c84` lub wersji z poprawkami z sekcji 18).
3. `systemctl --user enable --now hubi-tmux.service`; `hubi doctor`.

Sesje nie są odtwarzane. Dzięki odrębnym prefiksom v5 nie zobaczy pozostałości v6 jako swoich sierot i odwrotnie.

---

## 17. What must survive from v5

Kontrakty potwierdzone w tym audycie albo Twoimi testami live. v6 nie może pogorszyć żadnego z nich.

| # | Kontrakt | Dowód |
|---|---|---|
| 1 | Serwer tmux jest usługą systemd; tworzenie tylko po weryfikacji MainPID = PID socketu = cgroup, w stabilnej generacji | `v5_ownership` 12/12 |
| 2 | Klient tmux nigdy nie uruchamia serwera (`-N`) | `v5_tmux_server`, `v5_ownership` |
| 3 | Workload przeżywa utratę SSH, sieci, launchera i klienta tmux | testy live ownera; `v5_lifetime` 2/2 |
| 4 | Każdy workload ma własny cgroup; stop zabija cały cgroup, łącznie z potomkami po `setsid` i ignorującymi TERM | `run.sh`, `persistent_terminal`; T11 |
| 5 | Zabijanie wyłącznie po dokładnej nazwie jednostki i dokładnej nazwie sesji; nigdy po PID, nigdy globem | analiza statyczna: 1 miejsce sygnału |
| 6 | Token generacji (`InvocationID`) sprawdzany przed TERM i przed KILL; nieaktualne potwierdzenie nie zatrzyma następcy | `multi_instance`, `persistent_terminal` |
| 7 | Fail-closed: nieznany stan systemd to `UNKNOWN`, nigdy `INACTIVE`; brak akcji destrukcyjnej przy niepewności | `v5_scope_state` 9/9 |
| 8 | Preflight: bus, Linger=yes, `--kill-whom`, własność serwera — inaczej odmowa | `v5_preflight` 6/6 |
| 9 | Lifecycle serializowany per (typ, repo, instancja); rodzeństwo niezależne; brak blokady w czasie czekania na użytkownika; brak wycieku fd | T9a–d; `persistent_terminal` |
| 10 | Przerwanie startu w dowolnym punkcie jest odzyskiwalne kolejnym startem | T21: 25/25 |
| 11 | Stop przy wielu klientach i równoczesnych takeover nie zostawia klientów ani sesji | T10 |
| 12 | Repo rewalidowane (ścieżka, zawieranie, `dev:inode`) tuż przed utworzeniem; brak fallbacku do `$HOME` | `adversarial.py`, `persistent_terminal` |
| 13 | Wejście: jedna linia + Enter; strzałki, sekwencje ESC i wklejanie nie wywołują akcji | `adversarial.py` |
| 14 | `HUBI_ACTIVE` nie przecieka do serwera, agenta ani paneli; środowisko agenta to wąska allowlista zamrożona przy tworzeniu | `v5_agent_environment` 6/6 |
| 15 | Tryby attach: tylko podgląd / współdzielenie / przejęcie | `adversarial.py` |
| 16 | Argv agenta przekazywane bez powłoki | `multi_instance`, `adversarial.py` |
| 17 | Jawne kody wyjścia launchera: EOF 0, INT 130, HUP 129, TERM 143, `q` 98, `x` 99 | `run.sh`, `adversarial.py` |
| 18 | Doctor jest tylko do odczytu | `v5_doctor` 9/9 |
| 19 | Zakończony agent zostaje jako EXITED z ostatnim wyjściem | `run.sh`; T-exited |

---

## 18. Optional v5 fix-set

Małe, niezależne od decyzji o v6. Nie zaimplementowane.

| Priorytet | Poprawka | Rozmiar | Zamyka |
|---|---|---|---|
| 1 | Escapować `#` → `##` w argumencie `-c` (2 miejsca) | ~5 linii | F1 |
| 2 | `Restart=always` w `hubi-tmux.service` | 1 linia + restart usługi przy zerze sesji | F2, F3 |
| 3 | `env -u TMUX -u TMUX_PANE` przed poleceniem workloadu (2 miejsca) | 2 linie | F2 |
| 4 | `set -g exit-empty off` w `runtime_creation_preflight` | 1 linia | F3 |
| 5 | `GIT_OPTIONAL_LOCKS=0` dla gita z menu | 2 linie | F8 |
| 6 | Env agenta przez `tmux source-file -` zamiast argv | ~10 linii | F7 |
| 7 | `LC_ALL=C` dla `sort` i walidacji nazw | 2 linie | F10 |

Poprawki 1–4 polecam niezależnie od v6: dotyczą runtime, na którym pracujesz dziś. Poprawka 2 wymaga restartu usługi, więc tylko przy zerze workloadów.

Świadomie **nie** proponuję naprawiania laga w v5, jeśli v6 jest zatwierdzone: to przebudowa wszystkich funkcji statusu, czyli praca wyrzucona przy rewrite.

---

## 19. Test environment cleanup

| Sprawdzenie | Wynik |
|---|---|
| Sesje testowe | serwer zatrzymany, socket nie istnieje |
| Scope'y `hubi-*` w managerze 1001 | 0 przed zatrzymaniem managera |
| Klienci testowi (`audit-client-*`) | 0 |
| Usługa `hubiaudit-tmux.service` | wyłączona i zatrzymana |
| Linger `hubiaudit` | wyłączony; `user@1001.service` zatrzymany |
| Procesy uid 1001 | 0 |
| `/run/user/1001` | nie istnieje |
| cgroup `user-1001.slice` | nie istnieje |
| Pliki uid 1001 w `/tmp`, `/var/tmp`, `/dev/shm`, `/run` | 0 (usunięty 1 pusty katalog `/tmp/tmux-1001`) |
| Limity zasobów slice'a | cofnięte (`systemctl revert`), brak drop-inów |
| Użytkownik i grupa `hubiaudit`, `/home/hubiaudit` | usunięte; brak wpisów w `subuid`/`subgid` |
| Jednostki testowe w managerze ownera | 0 |

**Live runtime ownera — nietknięty.** Odcisk końcowy identyczny z odciskiem sprzed testów: `InvocationID` usługi `4553644e…`, MainPID 172, NRestarts 0, `InvocationID` wszystkich 6 scope'ów, id i czasy utworzenia wszystkich 6 sesji, PID wszystkich paneli, inode socketu. Ten sam jeden klient (`/dev/pts/3`, PID 1544604). Katalog blokad: 15 plików, jak na początku. Repo: 0 zmienionych plików, HEAD `def0c84`; zainstalowany `hubi` identyczny z repo.

Zachowane artefakty (poza live runtime, w katalogu roboczym sesji w `/tmp` — **tmpfs, znikną po restarcie**): skrypty pomiarowe i reproducery (`tools/`), prototypy (`proto/`), surowe wyniki (`perf_*.json|txt`, `adv1–5.out`, `suite_result.txt`), odciski live (`live_before.txt`, `live_after.txt`).

---

## 20. Final recommendation

1. **Czy v5 warto jeszcze poprawiać?** Tak, wąsko: poprawki 1–4 z sekcji 18. To łącznie kilkanaście linii, a zamykają oba P2 i jedno P3 na runtime, którego używasz codziennie. Nic więcej.
2. **Czy zamrozić v5?** Tak — po tych poprawkach v5 zostaje zamrożone jako działający fallback i referencja kontraktów. Bez nowych funkcji, bez optymalizacji laga.
3. **Czy rozpocząć Python v6?** Tak. Uzasadnieniem jest model danych i kierunek produktu, nie szybkość języka: pomiar pokazuje, że lag znika w obu językach po zmianie architektury zapytań, ale tylko w Pythonie dostajesz przy okazji testowalny rdzeń, jeden inwentarz i rozsądną bazę pod GitHub browser.
4. **Pierwszy checkpoint v6:** CP0 — izolowane środowisko testowe pod osobnym UID i kontrakty runtime jako testy black-box. Powód jest praktyczny: F4 pokazuje, że kod Hubi uruchomiony obok live runtime pod tym samym użytkownikiem potrafi zabić Twoje agenty. Zanim powstanie pierwsza linia v6, musi istnieć miejsce, w którym można ją bezpiecznie uruchamiać.

Dwie rzeczy do Twojej decyzji przed startem CP0:

- czy poprawki 1–4 do v5 mają być zrobione teraz (rekomenduję: tak),
- czy v6 ma używać odrębnych nazw (`hubi6-*`) — rekomenduję: tak; jedyny koszt to inna nazwa usługi w runbooku.
