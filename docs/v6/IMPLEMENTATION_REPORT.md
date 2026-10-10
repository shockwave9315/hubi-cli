# Hubi v6 — raport implementacji

## 1. Executive status

**V6 IMPLEMENTATION COMPLETE. READY FOR INDEPENDENT CLAUDE REVIEW.**

V6 działa jako osobna aplikacja Python, z kompletnym lifecycle Codex/Claude/
persistent terminals, unified inventory, CLI/TUI, read-only doctorem i GitHub
browserem. Końcowy izolowany suite: **64/64**, w tym **100/100** odzyskanych
startów po losowym SIGKILL. Sprawdzono też start prawdziwych programów Codex
i Claude bez poświadczeń ownera. Nie wykonywano uwierzytelnionych zapytań do
modeli. Private clone zweryfikowano przez mock `gh`, prawdziwy Git i systemd.

Nie wykonano pushu, merge, release, instalacji na koncie ownera, wyłączenia v5,
migracji workloadów ani cutoveru. Bash v5 i jego szablony pozostają niezmienione.

## 2. Baseline

- Branch początkowy: `main`.
- Start SHA: `def0c84d4e593755371acb09860fbfbbdb9116c2`.
- Branch implementacji: `feat/hubi-v6-python`.
- Początkowy working tree był czysty.
- Specyfikacja: pełny [audit v5](AUDIT_INPUT.md), zachowany z artefaktu audytu
  z 2026-10-10. Polecenie ownera ma pierwszeństwo przed jego opcjonalnymi
  propozycjami zmian v5 i późniejszego cutoveru.

## 3. Commity i finalny snapshot

1. `b1108dcc86bec1414087e6d7bb7a17a9faf30e2b` — `feat: implement Hubi v6 in Python with isolated workload runtime`.
2. `0857bb1d7bf07374988cd23bbc9e39cffd92b2e3` — `fix: preserve tmux client signal exit status`.
3. `4cf2ec1296df87a444a762033b60a17a0e85d0c5` — `fix: fail closed on malformed systemd batch identity`.
4. Commit raportu i evidence — `docs: record Hubi v6 validation and review handoff`.

Dokładny **final SHA**, obejmujący również ten raport, podano w końcowym
handoffie. Można go odczytać przez `git rev-parse HEAD` na powyższym branchu.
Raport nie wpisuje własnego hasha do własnej zawartości. Kod i testy odpowiadają
trzem pierwszym commitom; czwarty dodaje wyłącznie dokumentację i wyniki.

## 4. Changed files / structure

```text
hubi6/
  core/model.py                  modele, naming, identity, states, transitions/actions
  adapters/{process,tmux,systemd,git}.py
  inventory.py                   jeden snapshot, połączenie trzech receipts
  services.py                    jeden lifecycle dla wszystkich typów
  worker.py                      scope entrypoint i exec bez powłoki
  storage.py                     prywatny journal, atomic writes, flock
  github.py                      discovery, cache, LOCAL, clone/recovery
  config.py, doctor.py, cli.py, tui.py
  __main__.py, __init__.py
config/hubi6-tmux.conf
config/hubi6.example.toml
systemd/user/hubi6-tmux.service
tests_v6/                        unit, runtime, isolation, clone, benchmark, real agents
tools/{v6_lab,test_v6,v6_fingerprint,build_v6}.py
pyproject.toml, Makefile
docs/v6/{README,AUDIT_INPUT,IMPLEMENTATION_REPORT}.md
docs/v6/evidence/                wyniki, odciski i cleanup verification
```

Jedyna zmiana wcześniejszego pliku wspólnego: `.gitignore` dodaje `dist/` i
`.venv/`. `hubi`, stare config/systemd, `.bashrc` helper i legacy tests nie zostały
zmienione. Pakiet nie wymaga zależności runtime poza standardową biblioteką
Pythona i zewnętrznymi narzędziami systemowymi.

## 5. Architecture

Topologia: lingering `user@UID.service` → `app.slice` → `hubi6-tmux.service`
oraz osobny scope na każdy workload. SSH, Hubi launcher i tmux clients tworzą
jednorazową warstwę sterującą. Clone używa oddzielnej, ograniczonej czasowo
transient service z `KillMode=control-group`.

Runtime ID wiąże UID, HOME, socket, service i journal. Nie zależy od repo root,
więc jego rekonfiguracja nie chowa istniejących workloadów. Key wiąże typ,
bezwzględną canonical workspace path i instancję; generation ma losowy suffix.
Nazwy: `hubi6-RUNTIME-TYPE-KEY-GENERATION.scope` i odpowiadająca sesja. V5 nigdy
nie pasuje do namespace v6. Żadna operacja destrukcyjna nie używa globu jednostek.

Workload ma typ, repo/working directory, health, instancję, session/pane, scope,
generation, InvocationID, stan, liczbę klientów, wiek, cwd i metadata. Health to
OK/MISSING/REPLACED/INVALID, niezależne od stanu procesu. Workspace identity
obejmuje dev/inode katalogu i Git-dir oraz UUID w nieśledzonym pliku administracyjnym
Git; ten ostatni chroni także przed reuse inode. Nie zmienia tracked files/indexu.

Snapshot wykonuje **jedno wywołanie tmux i jedno batched `systemctl show`**.
Repo discovery nie uruchamia Git per wiersz. Wszystkie dalsze połączenia i stan
liczone są w Pythonie; runtime inventory nie jest cache'owany między renderami.
Przed mutacją weryfikowana jest stabilna generacja service, zgodność MainPID/PID
socketu, cgroup i UID. Scope kill wymaga świeżego stanu systemd, dokładnej nazwy,
InvocationID, description i cgroup. UNKNOWN nigdy nie jest utożsamiane z INACTIVE.

Journal zapisuje intent przed utworzeniem sesji. Niezależne receipts znajdują się
również w metadanych tmuxa i description scope'a. Dzięki temu przerwany start
i uszkodzone/usunięte metadata mają ścieżkę odzyskania bez zgadywania ownership.
Start/stop/reconcile mają bounded flock per logical key; attach nie trzyma blokady
przez czas pracy klienta. Osierocone działające workloady wymagają jawnego stopu.

Sekrety API i argv agenta przekazywane są jednorazową kopertą 0600 w prywatnym
runtime; worker usuwa ją przed exec. Sekrety nie są argumentami helperów, opcjami
tmuxa, właściwościami systemd ani zawartością journal/log. Agent otrzymuje wąską
allowlistę oraz stałe parametry użytkownika; nie dziedziczy TMUX/TMUX_PANE/launcher
markers. Program jest wykonywany bez powłoki. Terminal: Bash bez startup files.

State model: STOPPED, STARTING, RUNNING, ATTACHED, EXITED, ORPHANED, STALE,
UNKNOWN; osobne transition rules dla startup journal i actions per state.
EXITED zachowuje output w scrollback. Reconcile usuwa niespójny workload dopiero
po zweryfikowaniu pozostałych receipts i generacji.

CLI: version, doctor, list/json, start, stop, inspect, attach, reconcile,
github list/clone. TUI projektuje gotowy snapshot, zapisuje ekran zbiorczo,
ma spójne klawisze, pusty stan i widoczne diagnostics. Input wymaga linii + Enter,
kwarantannuje bracketed paste, ma timeout i przywraca termios w finally.

## 6. Mapping 19 kontraktów v5 → v6

Nazwy testów poniżej pochodzą z `tests_v6/test_runtime.py`, chyba że podano inaczej.

| # | Kontrakt / implementacja | Weryfikacja |
|---|---|---|
| 1 | `App.anchored`: service generation, MainPID/socket PID/cgroup/UID | `types_unified_view_and_cli_json_F14`, `no_alternate_server_and_readonly_doctor`, `systemd_unknown_fails_closed` |
| 2 | `Tmux.command` zawsze `-N -S` | `no_alternate_server_and_readonly_doctor` |
| 3 | Scope uruchamiany z tmuxa, poza SSH/launcherem | `real_ssh_disconnect_and_launcher_death`, `client_loss_multiattach_takeover_and_races`, 100 interrupted starts |
| 4 | Osobny scope; INT/HUP→TERM→KILL na cały cgroup | `full_cgroup_stop_setsid_and_sibling`, `types_unified_view_and_cli_json_F14` |
| 5 | Jedno miejsce scope signals; exact unit i exact generation session; brak PID kill workloadu | `runtime_namespace_F4`, `scope_generation_stale_confirmation`, source review adapters |
| 6 | Świeży InvocationID przed każdym sygnałem; immutable generation nazw | `scope_generation_stale_confirmation`, `concurrent_stops_do_not_touch_successor` |
| 7 | UNKNOWN fail-closed | `systemd_unknown_fails_closed`, `test_core.systemd_unknown_parsing`, state matrix |
| 8 | Bus, linger, kill capability, server ownership; brak auto-startu runtime | `preflight_unknown_linger_and_missing_kill_capability`, `no_alternate_server_and_readonly_doctor` |
| 9 | `Store.lock`: bounded flock/O_CLOEXEC/close_fds; lock per key, poza dialogiem/klientem | `lock_timeout_and_no_descriptor_leak`, `concurrent_workloads_and_independent_locks` |
| 10 | Intent journal, bounded startup, independent worker, cancellation/recovery | `interrupted_start_recovery_100`: 100/100 |
| 11 | Stop serializowany; exact immutable session target | `client_loss_multiattach_takeover_and_races`, `concurrent_stops_do_not_touch_successor` |
| 12 | Git root/containment/object revalidation, fchdir na zweryfikowanym fd | `revalidate_repo_before_external_workload`, `missing_replaced_invalid_repo_stoppable_F5_F6` |
| 13 | Line reader, ESC rejection, structural paste quarantine i timeout | `test_core.InputTests`, PTY signal/input cases |
| 14 | Allowlist snapshot w envelope, środowisko od zera, bez markerów launchera | `environment_secret_not_argv_F2_F7`, `absent_environment_and_frozen_on_attach` |
| 15 | Normal/view/shared/takeover | `client_loss_multiattach_takeover_and_races`: 3 klientów, view-only keys, takeover, reconnect |
| 16 | Payload argv → execvpe, systemd `--expand-environment=no` | `agent_argv_literal_and_resume`, `adversarial_repo_names_F1` |
| 17 | EOF 0, INT 130, HUP 129, TERM 143, q 98, x 99; client SIGKILL 137 | `tui_signals_eof_and_terminal_restoration`, `client_loss_multiattach_takeover_and_races` |
| 18 | Doctor bez mkdir/locks/log/config writes i bez startu | `no_alternate_server_and_readonly_doctor`, `types_unified_view_and_cli_json_F14` |
| 19 | remain-on-exit, primary pane, retained history | `session_without_scope_and_exited_output` |

## 7. Mapping F1–F15 → v6

| Finding | Implementacja | Testy / evidence |
|---|---|---|
| F1 | Jedno `tmux_format`: #→## dla cwd; bez shell evaluation | real names C#Sharp, #(...), #{...}, spacje, Unicode, $(), backticks, globy; `adversarial_repo_names_F1`, `agent_argv_literal_and_resume` |
| F2 | Usuwanie TMUX/TMUX_PANE; prywatny niestandardowy socket; Restart=always | `environment_secret_not_argv_F2_F7`, `bare_tmux_cannot_kill_runtime_F2`, `tmux_server_crash_restarts_without_touching_sibling_runtime_F2` |
| F3 | Config + preflight wymuszają exit-empty off; option hook; Restart=always | `source_file_and_restart_policy_F3`, `source_file_last_stop_without_preflight_F3` |
| F4 | Runtime/UID/service/socket/journal identity w scope/session; generation; brak automatycznego orphan kill | `runtime_namespace_F4`: dwa aktywne runtime IDs, stop B zachowuje A |
| F5 | Inventory z journal/tmux/systemd niezależny od repo list | `missing_replaced_invalid_repo_stoppable_F5_F6`, `scope_without_session_and_lost_receipt` |
| F6 | dev/inode + Git-dir identity + UUID; REPLACED przed startem | runtime replacement i `test_core.repo_identity_replacement_and_invalid_F5_F6` |
| F7 | Jednorazowy prywatny envelope, brak API values w helper argv/logs/metadata | `environment_secret_not_argv_F2_F7`, scan `/proc/*/cmdline` test UID; source argv paths |
| F8 | `Git.env` zawsze GIT_OPTIONAL_LOCKS=0 dla odczytów | `test_core.git_optional_locks_F8`; prawdziwe rev-parse/status w adapterze |
| F9 | Jedyny fallback `/run/user/UID`, private ownership/modes; ten sam bus/socket/locks | `runtime_path_fallback_F9`, `private_directories_and_metadata`, `absent_environment_and_frozen_on_attach` |
| F10 | ASCII regex instancji; porządek os.fsencode, bez locale sort subprocess | `instances_ascii_in_all_locales_F10`, `namespace_and_generation` |
| F11 | Trzy receipts, UNKNOWN + inspect + jawny reconcile; doctor sygnalizuje niespójność | `corrupt_metadata_repair_F11`, `scope_without_session_and_lost_receipt` |
| F12 | Terminal dostaje HUP; gotowość z potwierdzenia workera, bez stałej pętli dziesięciu opóźnień | start/stop prawdziwego Bash w typach, adversarial names, SSH, 50 workloadach benchmarku |
| F13 | Tylko oznaczony disposable UID, kernel probes, cleanup całego labu | `test_isolation`, `tools/test_v6.py`, cleanup.json |
| F14 | Pełne CLI, version/list/json/start/stop/inspect; brak TTY daje błąd zamiast prompt loop | `types_unified_view_and_cli_json_F14`, CLI invocations w suite, JSON również na TTY |
| F15 | Reject control paths; display sanitization, spójne klawisze, paginacja, paste quarantine | `control_characters_F15`, `InputTests`, `render_exception_restores_terminal_and_json_on_tty` |

## 8. Unified workload view

Codex, Claude i terminale znajdują się w tym samym inventory i ekranie `w`.
Wszystkie mają te same ID, state/health, klientów, wiek, cwd, inspect i lifecycle.
Akcje wynikają ze stanu. MISSING/REPLACED/INVALID nie usuwają workloadu z widoku
ani możliwości stopu. Scope bez sesji można odzyskać z własnego description;
zakończona sesja zachowuje output. Niespójność pozostaje UNKNOWN i oferuje repair
z użyciem niezależnych receipts.

## 9. GitHub browser

- Discovery: `gh repo list --json` z kompletną paginacją oraz domyślnie paginated
  `gh api user/repos` dla collaborator/organization access. `repo list` samo
  obejmuje repo właściciela; uzupełnienie wynika z
  [dokumentacji gh repo list](https://cli.github.com/manual/gh_repo_list) i
  [gh api pagination](https://cli.github.com/manual/gh_api).
- LOCAL: normalized origin HTTPS/SCP/SSH, również worktrees/common config,
  bez zależności od nazwy katalogu. Private, fork i archived są prezentowane.
- Auth: wyłącznie istniejące `gh`; HTTPS clone i `gh auth git-credential`.
  Hubi nie odczytuje i nie przechowuje tokenu GitHub.
- Cache: prywatny JSON, domyślnie 600 s; explicit offline; stale/error marker;
  ekran może najpierw pokazać cache. Network nie należy do runtime renderu.
- Clone: prywatny staging na tym samym filesystemie, własny bounded cgroup,
  walidacja origin, atomic rename NOREPLACE, natychmiastowe wejście do zwykłego
  repo workflow. Brak nadpisywania katalogów/symlinków.
- Failures: offline/auth/cache corruption/pagination/kolizja/interrupt są
  pokryte testami. TERM sprząta od razu; SIGKILL pozostawia ukryty marked staging
  do odzyskania przy następnej operacji, z helperem ograniczonym do 60 s.
- Private auth: bez realnego tokenu ownera; test mock `gh` + rzeczywisty Git clone
  + rzeczywista transient service, z weryfikacją HTTPS/credential-helper argv.

## 10. Performance

Dziewięć ciepłych snapshotów na kształt inventory. Licznik Runner potwierdza
**dwa subprocess invocations na snapshot** dla każdego rozmiaru. Nie jest to
pomiar całego startu interpretera, opóźnienia sieci ani liczby wewnętrznych
wywołań D-Bus przez systemctl. Nie wykonywano globalnego drop_caches na devboxie.

| Repo | Workloads | Klienci | Mediana ms | p95/max z 9 ms | Subprocessy |
|---:|---:|---:|---:|---:|---:|
| 7 | 0 | 0 | 10.83 | 11.75 | 2 |
| 7 | 6 | 1 | 27.64 | 28.95 | 2 |
| 200 | 0 | 0 | 36.80 | 39.80 | 2 |
| 1000 | 0 | 0 | 143.18 | 152.96 | 2 |
| 1000 | 50 | 0 | 272.49 | 278.82 | 2 |

Kształt zbliżony do audytu mieści się w około 30 ms dla snapshotu. Przy 1000 repo
i 50 workloadach koszt pozostaje zauważalny; jest to jawny residual performance
finding. Nie wraca architektura setek exec/forków na render. Raw evidence:
[final-suite-and-cleanup.log](evidence/final-suite-and-cleanup.log),
[performance.json](evidence/performance.json).

## 11. Tests

- 33 unit tests: state matrix, transitions/actions, identity/naming/escaping,
  metadata/UNKNOWN, repo matching/health, config/permissions, input, GitHub.
- 28 runtime/adversarial tests: prawdziwe tmux/systemd/Git, wszystkie typy,
  scope descendants/setsid/TERM resistance, missing/replaced repo, orphan/stale
  receipts, concurrency/locks, attach/view/share/takeover, reconnect, launcher
  SIGKILL, bezpośredni SIGKILL klienta tmux, realne zerwanie SSH i terminal restore.
- 2 clone integration tests: prawdziwy Git/systemd, fake gh, private flags,
  LOCAL po clone, TERM i SIGKILL/staging recovery.
- 1 kernel isolation test: signals, sockets, cgroups, brak capabilities.
- **64/64**, końcowy suite; **100/100** interrupted starts w tym przebiegu.
- Dodatkowy real-agent smoke: Codex CLI 0.162.1 i Claude Code 2.1.296,
  oba RUNNING z widocznym UI, następnie pełny stop. Bez kopiowania credentials.
- Performance: 7/200/1000 repo, 6 workloadów + klient, 50 workloadów; 2 calls.
- `compileall`, `git diff --check`, build directory bundle i `--version`.

Fuzz stosuje deterministyczny seed i SIGKILL w losowych opóźnieniach 1–240 ms
podczas startu. Testy wstrzykują też niespójność receipts i awarie prerequisite
bez modyfikacji live v5. Nie uruchamiano legacy v5 harnessów jako owner.

## 12. Test isolation

Disposable `hubi6test`, UID 1001, osobny HOME, lingering `user@1001.service`,
bus, socket, service/scopes, synthetic repos i fake agents. Test driver działał
jako transient service managera 1001. Limity: TasksMax=4096, MemoryMax=2G,
CPUWeight=40. Osobny root-owned transient sshd nasłuchiwał wyłącznie na loopback
na losowym porcie i dopuszczał wyłącznie `hubi6test`; miał własne host/login keys.
Nie zmieniano konfiguracji ani połączeń SSH ownera.

Kernel probes potwierdziły EPERM dla sygnałów do wszystkich zastanych procesów
UID 1000, odmowę socketu tmux i busu ownera, brak zapisu do jego cgroups oraz
CapEff=0. Test user nie miał sudo. Pomyłka w komendzie testowej nie mogła wysłać
sygnału do v5 lub połączyć się z jego managerem/socketem. Root supervisor używał
wyłącznie oznaczonego disposable UID i jego dokładnych jednostek.

## 13. Cleanup verification

Finalny supervisor zakończył się sukcesem. Przed teardown: zero workload scopes.
Po teardown niezależne sprawdzenie potwierdza: brak test user/group, HOME,
`/run/user/1001`, linger, cgroup slice, resource overrides, transient sshd,
procesów UID 1001, wpisów subuid/subgid i testowych top-level ścieżek w
`/tmp`, `/var/tmp`, `/dev/shm`. Runtime/socket/journal/cache/synthetic repos
i kopie realnych binarek były wewnątrz usuniętego HOME/runtime.

[cleanup.json](evidence/cleanup.json): `all_clean=true`. Testowy UID nie został
zachowany. Zachowane są wyłącznie celowe artefakty review w repo i uninstalled
directory bundle w ignorowanym `dist/hubi6`.

## 14. Live v5 fingerprint before/after

[live-before.json](evidence/live-before.json),
[live-after.json](evidence/live-after.json): **identical=true**.
Usługa v5 zachowała MainPID **172**, InvocationID i NRestarts **0**; wszystkie
osiem początkowych scope'ów i sesji zachowało tożsamości/generacje, czasy sesji
i PID paneli. Inode socketu i SHA256 trzech zainstalowanych plików v5 są takie same.

[client-continuity.json](evidence/client-continuity.json): ci sami dwaj klienci,
PID **1544604** i **2098447**, tty i sesje. Początkowe dane klientów pochodzą
z pierwszego read-only `ps` w transcript, końcowe z jawnego `tmux -N -S ...
list-clients`. Nie dopisano zmyślonych początkowych timestamps.

**Live v5 pozostało nietknięte.**

## 15. Residual findings

- **P1:** brak znanych otwartych findings po wykonanych testach.
- **P2:** brak znanych otwartych findings po wykonanych testach.
- **P3 / performance:** 1000 repo + 50 workloadów nadal kosztuje setki ms;
  pełny filesystem scan i batched systemctl properties skalują się z inventory,
  mimo stałej liczby subprocessów. Potencjalna dalsza optymalizacja na podstawie
  osobnego profilu, bez zmiany fail-closed lifecycle.
- **P4 / UX:** odświeżenie GitHub jest synchroniczne, bounded 30 s; cache wyświetla
  się od razu, refresh wymaga `r`. Brak background worker/frameworka TUI.
- **P4 / recovery:** zniszczenie wszystkich trzech niezależnych ownership receipts
  wymaga ręcznej diagnostyki; v6 pokazuje unverified objects i odmawia zgadywania
  oraz destrukcyjnego sweepu.

Brak znanych P1/P2 nie zastępuje niezależnego deep review.

## 16. Known limitations / granice dowodów

- Gwarancja lifecycle obejmuje primary pane i jego scope. Ręczne extra panes/
  windows poza Hubi nie są adoptowane do cgroup; v6 blokuje domyślne bindings,
  wykrywa extras jako UNKNOWN i wymaga ich usunięcia przed stopem. Nie pogarsza
  zakresu gwarancji v5, który również ich nie sprzątał.
- Restart wspólnego tmux servera nie rekonstruuje sesji ani zakończonych agentów.
  Żywe scope'y pozostają orphanami do explicit stop. Restart managera/hosta nie
  oznacza odtworzenia workloadów.
- Testy realnych Codex/Claude potwierdzają exec/UI/scope/stop, nie uwierzytelnioną
  sesję inference. Workload persistence/adversarial sprawdzono na kontrolowanych
  agentach oraz realnym Bash; private GitHub auth używa bezpiecznego mocku.
- Clone ma limit 60 s; bardzo duże/wolne repo może wymagać późniejszego ustawienia
  innego timeoutu. Kolizja nazw forków jest zgłaszana bez automatycznej zmiany nazwy.
- SIGKILL clone launchera jest nieprzechwytywalny: hidden staging recovery następuje
  przy następnym clone, a helper ma niezależny limit czasu. Visible pół-repo nie powstaje.
- Target: Linux/cgroup v2/systemd z `--expand-environment=no`, tmux z `-N`, Git,
  gh; przetestowano Debian 13/Python 3.13.5/tmux 3.6b/systemd 257/gh 2.46.0.
  Nie deklarujemy testów na innych platformach ani cold-cache latency.

## 17. Handoff

**READY FOR INDEPENDENT CLAUDE REVIEW.**

Review może zacząć od `python3 -m hubi6 --version`, `make test`, `make integration`
i [README v6](README.md), po czym analizować lifecycle/ownership/receipts/clone
cleanup i adversarial rozszerzenia. Cutover pozostaje osobnym krokiem ownera po
review. W tym zadaniu nie wykonano żadnej instalacji ani przełączenia live.
