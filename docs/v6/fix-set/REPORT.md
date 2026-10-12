# Hubi v6 — fix-set po niezależnym review PR #8

## 1. Executive status

**FIX-SET COMPLETE. READY FOR CLAUDE TARGETED RE-REVIEW.**

Wszystkie 3 P2 i lokalne poprawki wszystkich 12 P3 zostały zaimplementowane.
Naprawiono również tanie P4; pozostałe granice opisano jawnie poniżej. Topologia
systemd/tmux/scope pozostaje ta sama. Nie modyfikowano Basha v5.

Źródło: [cały komentarz Claude](https://github.com/shockwave9315/hubi-cli/pull/8#issuecomment-6109830600),
przeczytany przed rozpoczęciem zmian; [zachowana kopia](CLAUDE_REVIEW_INPUT.md).
Poprzedni raport IMPLEMENTATION_REPORT opisuje historyczny baseline i jego
walidację; ten raport zastępuje jego wnioski o otwartych findings.

## 2. Baseline HEAD

`36fa688953cc796a7e41cb4e43cce80bfebc3bbf`, branch `feat/hubi-v6-python`,
working tree czysty. PR #8 pozostaje na tym samym branchu, z bazą `main`.

## 3. Final HEAD / testowany source

Kod, testy, instalator i runbook zweryfikowano na czystym
`c65f10b98f1ac61718994246417717b454648bf3`.
Końcowy commit dodaje wyłącznie ten raport i evidence. Jego pełny final HEAD
podano w końcowym handoffie oraz w aktualizacji PR; nie wpisujemy hasha commita
raportu do jego własnej zawartości. Fresh bundle po finalnym commicie ma ten
sam source digest i aktualny REVISION.

## 4. Commity

1. `70c2a6396214f5d50172f460d625f914b59c6f35` — `fix: address Hubi v6 deep review findings with regression coverage`.
2. `c65f10b98f1ac61718994246417717b454648bf3` — `fix: verify pane repair receipts and preserve virtual terminal environment`.
3. Commit raportu/evidence — `docs: record Hubi v6 fix-set validation and targeted review handoff`.

## 5. Changed files

- `hubi6/config.py`, `worker.py`, `storage.py`: jawny layout workera, typy configu,
  shell/grace/clone defaults, private directories, bezpieczne temp cleanup.
- `adapters/git.py`, `tmux.py`, `systemd.py`: per-entry errors, GitHub hosts,
  binding policy, exact-pane repair i exit status, higiena importów.
- `inventory.py`, `services.py`, `core/model.py`: oddzielny runtime snapshot,
  UNKNOWN, stale receipts, prefix/EXITED/moved-workspace rules, environment,
  grace i fresh generation checks.
- `tui.py`, `cli.py`, `doctor.py`: burst quarantine, rearm, confirmation,
  terminal/hangup handling, PASS/WARN/FAIL, debug, widok health.
- `github.py`: diagnostics/redaction, timeout/PATH, usuwanie clone-only helper config.
- `tools/build_v6.py`, nowy `tools/install_v6.py`, `tools/v6_lab.py`, `tools/test_v6.py`:
  świeży bundle, source manifest, install/uninstall i wzmocniona walidacja labu.
- `tests_v6/test_fixset.py`, `test_install.py`, zmiany core/runtime/clone/real_agents
  oraz benchmarku. `config/hubi6*`, README v6, nowy [CUTOVER.md](../CUTOVER.md).
- `docs/v6/fix-set/`: pełny input review, raport i raw evidence.

`hubi`, v5 config/unit, autologin helper, legacy tests i README v5 mają pusty diff
względem baseline. Nie ma zmian na `main`.

## 6. Mapping wszystkich P2/P3/P4

Nazwy testów w tabelach pomijają prefiks `test_`. Jeśli nie zaznaczono inaczej,
testy są w `tests_v6/test_fixset.py`.

| Finding | Status | Implementacja | Konkretna regresja |
|---|---|---|---|
| P2-1 discovery/lifecycle | FIXED | Każdy stat/resolve w try per entry; error path nie wykonuje kolejnego stat. `runtime_snapshot()` bez discovery i bez health obcych workspace'ów; find/inspect tylko wybrany health, stop/attach bez niego. | `discovery_isolates_stat_errors_without_error_path_probe`; `bad_entries_do_not_block_inventory_or_lifecycle`: prawdziwe ELOOP, broken link, chmod 000/unreadable target i aktywny terminal, CLI inspect/stop oraz attach; discover mutation guard |
| P2-2 paste | FIXED | Rearm przed każdym logicznym odczytem i po detach; 80 ms quiet quarantine reszty burstu; drain przed pokazaniem potwierdzenia; termios finally. | prawdziwe PTY `raw_paste_burst_cannot_cross_screens_or_confirm_stop`, `paste_after_real_tmux_detach_and_bracketed_paste`; ordinary typing/backspace i baseline signal/EOF tests |
| P2-3 custom state/worker | FIXED | `worker_config` używa jawnej canonical record_path, weryfikuje private state/workloads/RID i payload layout, UID oraz niezależnie wyliczony RID; ignoruje config/env tmux servera. Account HOME zgodny z systemd %h. Doctor sprawdza wykonalność layoutu. | `custom_state_home_worker_and_interrupted_recovery`: terminal, fake Codex, inventory, stop, SIGKILL/recovery, invalid permissions/doctor FAIL; `worker_wrong_layout_refused_and_private_temp_cleanup`: poprawny control, zły RID i layout |
| P3-1 tmux UNKNOWN | FIXED | Aktywny service + nieznany tmux daje runtime/workload UNKNOWN; ORPHANED dopiero po dowodzie braku sesji. | `live_scope_tmux_unavailable_is_unknown_and_closed`: JSON/rc, doctor, stop, reconcile, attach, żywy scope zachowany |
| P3-2 doctor | FIXED | PASS/WARN/FAIL, ready/runtime_ready/warnings, rc 0/3/1. Core/ownership/layout = FAIL; brak Codex/Claude/gh = WARN. | `doctor_warn_fail_and_json_semantics`; custom state test; fresh installed bundle doctor PASS |
| P3-3 extra panes | FIXED | Preflight/attach/stop ponawiają binding policy, również creation menus. Exact-session reconcile usuwa extra panes/windows i zachowuje primary. Scope receipt musi wskazywać dokładnie wybraną generację. | `extra_pane_repair_and_binding_reassertion`: source-file, trzy pane/window objects, stale confirmation, crossed receipt, rodzeństwo nietknięte, repair→normal stop |
| P3-4 stop grace | FIXED | Konfigurowalne INT 3/TERM 2/KILL verify 2; terminal HUP 1/TERM 1/KILL verify 2. Fresh scope checks przed każdym sygnałem. | `slow_graceful_agent_shutdown`: 1,05 s clean INT handler zapisuje plik; full-cgroup setsid baseline; real-agent smoke |
| P3-5 stale journal | FIXED | Read-only snapshot ukrywa tylko receipt z jednoznacznym brakiem obu runtime objects; next same-key start usuwa go pod blokadą. UNKNOWN nigdy nie jest GC. | `stale_journal_hidden_after_runtime_generation_change`: rzeczywisty restart testowego tmux, list/doctor bez usuwania pliku, następny start usuwa stary receipt |
| P3-6 start EXITED | FIXED | Czytelny HubiError, konieczny świadomy stop/reconcile. Acknowledgement nie zwraca sukcesu, gdy już dowiedziono EXITED. | `exited_start_fails_and_reports_exit_status`: exit 23, ponowny start odmawia, stop i nowa generacja działają |
| P3-7 prefix | FIXED | Co najmniej 8 hex characters, do pełnego ID; puste/krótkie/niepoprawne i ambiguous odmawiane przed probe. Wspólne find dla wszystkich komend. | `empty_short_and_ambiguous_prefixes` z probe guard i pozytywnym pełnym ID |
| P3-8 nested Hubi | FIXED | Worker ustawia własny HUBI6_ACTIVE=1; terminal dodatkowo HUBI_NOAUTO=1. TUI i attach odmawiają self-nesting. | `normal_terminal_rc_environment_and_nested_refusal`: rzeczywisty Bash rc i odmowy TUI/attach |
| P3-9 clone | FIXED | Redacted gh stderr/stdout, error vs timeout, configurable 300 s, jawny PATH w unit, confirmation/progress/result/cancel. | `test_clone_runtime.auth_error_redaction_timeout_and_path`; TERM/SIGKILL baseline; `github_clone_confirmation_progress_and_cancel` PTY |
| P3-10 terminal/env | FIXED | Valid SHELL/passwd interactive shell z rc; markery, SSH_AUTH_SOCK/SHELL/LANG/LANGUAGE/standard LC_*, dotychczasowe API/proxy/SSL. TERM pozostaje typem virtual tmux, nie dumb launchera. | `normal_terminal_rc_environment_and_nested_refusal`; baseline env/API/absent/frozen tests; real Codex/Claude |
| P3-11 performance coupling | FIXED (coupling) | Lifecycle runtime snapshot niezależny od repo count, reuse pod blokadą, nadal 2 subprocessy na render; bez N+1. | discover mutation guard; benchmark 7/6, 200/6, 1000/50 oraz parowane 7/50 vs 1000/50 inspect/attach/stop. Bulk render latency pozostaje residual P3 poniżej. |
| P3-12 build/install | FIXED | Świeży staging/output, brak stale modules; dirty REVISION + SOURCE hashes; installer odmawia dirty bez explicit development flag, weryfikuje hashes/zero-state, atomic replace symlink entries; uninstall i runbook. | `fresh_build_no_stale_files_and_dirty_identity`; `test_install.fresh_bundle_install_workflows_and_uninstall`: clean bundle, trzy typy, doctor/CLI/attach/stop, live uninstall refused, restore fallback i marker cleanup |

| P4 z review (kolejność bullets) | Status | Implementacja i test |
|---|---|---|
| Utrata wszystkich receipts / obcy journal file | RESIDUAL — fail-closed; messaging FIXED | Nie wolno odtwarzać ownership z samego prefiksu. Komunikat wskazuje exact unverified session i ręczną diagnostykę; z plikiem: surviving receipt albo manual inspect/move. `all_receipts_lost_and_foreign_journal_remain_fail_closed` dowodzi braku sweepu, doctor FAIL i zachowania rodzeństwa. |
| Prawdziwy hangup rc120 | FIXED | Terminal finally toleruje EIO, przywraca handlery; stdout/stderr flush po hangup nie nadpisuje rc129. `closed_stdin_and_real_pty_hangup_exit_codes` zamyka master PTY. |
| Backspace literal DEL | FIXED | Echo używa backspace-space-backspace, parser edytuje Unicode. PTY raw-paste test zawiera DEL i sprawdza brak literalnego `\\u007f`; unit `InputTests.backspace`. |
| Debug/config diagnostics | FIXED | --debug daje traceback, unknown keys/types i explicit missing --config dają HubiError. `debug_traceback`, `config_errors_are_actionable`. |
| EXITED JSON / main health | FIXED | pane_dead_status/signal w JSON; main pokazuje unhealthy workloads także gdy repo nie ma już w discovery. `exited_start_fails_and_reports_exit_status`, `main_view_shows_unhealthy_workspace_even_when_not_discovered`. |
| Moved workspace duplicate | FIXED | UUID + dev/inode wykrywają ten sam przesunięty working tree; workspace lock serializuje start tej samej type/instance, bez blokowania innych instancji. `moved_workspace_does_not_duplicate_agent`. |
| UUID/helper/lock/temp leftovers | FIXED w bezpiecznym zakresie; RESIDUAL intentional/outside paths | Clone usuwa tylko tymczasowe helper values; real Git clone test to sprawdza. Aged private atomic temps sprzątane podczas mutacji, świeże zachowane; UUID/locks/temp usuwane podczas zero-state uninstall. Testy `worker_wrong_layout_refused_and_private_temp_cleanup`, clone runtime, fresh install. Lock inode nie jest unlinkowany przy możliwych waiterach; UUID w repo poza root/nieczytelnym wymaga późniejszego manual cleanup. |
| Unused imports/Config.timeout/RepoRef.git_ctime_ns | FIXED | Usunięte; odczyt historycznego diagnostic field pozostaje kompatybilny w parse. Unit/compileall oraz kontrola AST importów. |
| Origin www/ssh443/insteadOf | FIXED common hosts; RESIDUAL exotic aliases | www.github.com i ssh://git@ssh.github.com:443 normalizowane bez exec. `origin_common_github_hosts` + baseline matching. Nie implementujemy pełnego Git/SSH insteadOf/host-alias resolvera ani per-repo subprocessów. |
| list rc / closed stdin | FIXED | list daje JSON i rc1 dla DOWN/DEGRADED/UNKNOWN lub UNKNOWN workload; zdrowe rc0. Closed stdin EOF0. `live_scope_tmux_unavailable_is_unknown_and_closed`, closed stdin PTY test, installed CLI. |

Nie ma findingu uznanego za NOT APPLICABLE. Nie wyłączano testów po to, aby
osiągnąć zielony suite. Rozszerzenia zachowują fail-closed ownership.

## 7. Finalna semantyka P3-4 i P3-10

Agent: INT i maksymalnie 3 s na clean exit; następnie TERM i 2 s; potem KILL
całego scope'a i maksymalnie 2 s na potwierdzenie. Terminal: HUP 1 s, TERM 1 s,
KILL/verify 2 s. Czyste zakończenie skraca oczekiwanie. Wszystko pod [stop] w TOML,
positive finite seconds; ownership/InvocationID sprawdzane przed każdym sygnałem.

Terminal to poprawny `$SHELL`, potem passwd shell, potem Bash, z `-i` i normalnymi
rc. Agent i terminal dostają HUBI6_ACTIVE=1, terminal także HUBI_NOAUTO=1.
Allowlista rozszerzona o SSH_AUTH_SOCK, SHELL, LANG, LANGUAGE i standard LC_*;
API/proxy/SSL zachowane. Nie dziedziczymy całego env, TMUX/TMUX_PANE/HUBI_ACTIVE.
TERM=screen-256color opisuje rzeczywisty virtual terminal. Native smoke obejmuje
wyłącznie exec/UI/attach/stop/resume command bez owner credentials/inference.
Zapis i wznowienie uwierzytelnionej rozmowy wymaga przyszłego owner smoke.

## 8. Test results

- **40/40 unit**, także oddzielnie poza labem: [unit.log](evidence/unit.log).
- **90/90 pełny suite** pod disposable UID: 40 unit + 50 integration/regression/adversarial, bez skipów. [final-validation.log](evidence/final-validation.log).
- **100/100** przerwanych startów odzyskanych. Races/multiattach/takeover, SSH/client/launcher death, setsid/full-cgroup stop i wszystkie nowe targeted regresje są w tym przebiegu.
- Git stress: **0/60** nieudanych commitów podczas statusu Hubi; kontrola z optional locks: **4/60**. Negative control musi wykazać contention. Test został wykonany także w poprzednich przebiegach (0 vs 7 i 0 vs 3); końcowy wynik powyżej.
- Fresh clean bundle: manifest dirty=false, hash verification, install z symulowanym v5 symlink launcherem (target bez zmian), Codex/Claude/terminal, doctor PASS, JSON/inspect/attach/stop, odmowa uninstall przy live scope, uninstall i przywrócenie fallbacku. [build-source.json](evidence/build-source.json).
- Real Codex 0.162.1 i Claude 2.1.296: UI + rzeczywisty klient, stop około **242/340 ms**, ponowne uruchomienie `resume` bez auth; brak deklaracji o zapisanej rozmowie/inference. [real-agents.json](evidence/real-agents.json).
- `compileall`, `git diff --check`, fresh build i `--version` przeszły.

W trakcie pracy dwa błędne założenia nowych testów poprawiono (argv fake agents
w labie i escapowane klawisze tmuxa). Native smoke poprawiono z fixed sleep na
rzeczywisty frontend/bounded wait; TERM launchera nie nadpisuje virtual tmux TERM.
Żadnego findingu nie ukryto przez skip ani przez osłabienie testu.

## 9. Performance before/after

Baseline pochodzi z niezależnego komentarza Claude: mediana snapshotu 28,9 ms
(7/6), 57,0 ms (200/6), 288,6 ms (1000/50); 2 exec na snapshot. Historyczny
stop: około 190 ms przy małym i 960 ms przy dużym inventory (różne workload counts).
Nowy benchmark oddziela render, runtime snapshot i lifecycle. Parowane akcje
7/50 i 1000/50 mają stałą liczbę workloadów, więc izolują wpływ repo count.

| Kształt | Before ms (review) | After ms | Subprocessy |
|---|---:|---:|---:|
| 7/6 | 28.9 | 28.00 | 2 |
| 200/6 | 57.0 | 53.98 | 2 |
| 1000/50 | 288.6 | 274.49 | 2 |

Runtime-only snapshot: **25,40 ms** przy 6 workloadach, **142,20 ms** przy 50; po **2 subprocessy**, bez skanu repo.

| Repo / istniejące workloads | Inspect ms | Attach ms | Stop ms | Calls inspect/attach/stop |
|---|---:|---:|---:|---|
| 7/6 | 29.29 | 190.73 | 141.36 | 2/7/15 |
| 7/50 | 145.05 | 790.10 | 369.38 | 2/7/15 |
| 1000/50 | 144.08 | 793.31 | 373.39 | 2/7/15 |

Przy stałych 50 workloadach zmiana 7→1000 repo zmienia stop 369,38→373,39 ms,
inspect 145,05→144,08 ms, attach 790,10→793,31 ms. Nie ma liniowego wpływu
repo count na lifecycle. Liczba workloadów nadal wpływa na koszt batched runtime
inventory. [Raw performance.json](evidence/performance.json).

Dziewięć warm snapshots / pięć próbek akcji; bez globalnego drop_caches. Attach
obejmuje start interpretera/klienta i polling gotowości, inspect dotyczy App.find,
stop pełnego lifecycle. Liczba subprocessów mierzona Runnerem, dla attach przez
shim Runner wewnątrz prawdziwego procesu PTY; nie jest to liczba wewnętrznych
D-Bus calls ani wszystkich forków narzędzi. Test mutation guard dodatkowo odmawia
jakiegokolwiek wywołania discover podczas lifecycle, niezależnie od timings.

## 10. GitHub clone

Istniejące gh HTTPS auth/credential helper, bez token store. Powód błędu gh
zachowany w ograniczonym komunikacie, standard GitHub token patterns, credentials
w URL i znane env secrets redacted. Timeout `[github].clone_timeout` domyślnie
300 s; RuntimeMaxSec i helper timeout są spójne, potem pełny cgroup cleanup.
Unit dostaje jawny PATH launchera, więc helper gh jest dostępny także poza
PATH managera. TUI pokazuje repo/destination, wymaga świeżego yes, potem Cloning
oraz wynik; cancel nie tworzy repo. Cache/refresh pozostają synchroniczne.

Clone najpierw staging, walidacja origin, atomic NOREPLACE. TERM/INT/HUP finally
sprzątają; SIGKILL pozostawia ukryty lease do następnego clone, a unit ma niezależny
limit. Helper values dodane tylko na clone są usuwane z .git/config przed publish.
Private auth i errors testowane bezpiecznym mockiem gh z prawdziwym Git/systemd;
nie używano tokenu ownera ani realnego private clone jako owner w tym fix-secie.

## 11. Residual findings

- **P1: 0 znanych. P2: 0 znanych** po końcowej walidacji; wymaga targeted re-review.
- **P3:** pełny render 1000 repo/50 scope'ów nadal kosztuje setki ms. Repo scan
  jest już poza lifecycle; dalsze przyspieszenie dużego runtime inventory wymaga
  osobnego profilu/zmiany sposobu odczytu systemd, bez osłabiania fail-closed.
- **P4:** bez żadnego ownership receipt potrzebna jest ręczna diagnostyka exact
  object; brak destrukcyjnego zgadywania. Obce pliki journala pozostają do manual
  inspect/move. Komunikaty i testy są poprawione.
- **P4:** egzotyczne insteadOf/SSH host aliases pozostają nierozwiązywane; common
  GitHub URL variants działają bez N+1. Workspace UUID poza repo root/nieczytelny
  nie jest szukany/usuwany przez skan całego filesystemu.
- Stable locks są celowym mechanizmem synchronizacji do zero-state uninstall;
  unlink podczas pracy tworzyłby ABA race. Temp files younger than one day nie
  są automatycznie usuwane. Są usuwane z namespace w poprawnym uninstall.
- Granice: synchroniczny network refresh (30 s) i nieprzechwytywalny SIGKILL clone
  wymagający next-operation staging recovery; bez zmian architektury/async.

Nie zostawiono znanych łatwych P3/P4, które można usunąć lokalnie bez utraty
ownership/synchronizacji lub rozszerzania resolvera poza uzgodniony zakres.

## 12. Test isolation / cleanup

Wszystkie realne lifecycle, sygnały, kill-server, extra panes, races i instalacje
wyłącznie jako `hubi6test` UID 1001 z markerem, własnym systemd --user, busem,
HOME/runtime/socket/unitami/repos i loopback sshd. Limity CPU/Memory/Tasks jak
w implementacji. Testy odmawiają UID 0/1000, kernel probes weryfikują EPERM wobec
procesów ownera i odmowy socket/bus/cgroup. Root supervisor zarządza wyłącznie
oznaczonym UID i jego dokładnymi jednostkami. Kopiowano tylko binarki/pakiet
Codex/Claude/Node, bez auth/config ownera.

Przed teardown: **scopes=0, sessions=0, clients=0, clone_units=0**. Po stopie
usługi testowej socket zniknął. Po teardown: **processes UID1001=0**, user/group,
HOME, runtime, linger, cgroup, slice overrides, transient sshd i wpisy subuid/subgid
usunięte; brak testowych top-level ścieżek w /tmp,/var/tmp,/dev/shm. Synthetic repos,
metadata i kopie binarek znajdowały się w usuniętym HOME/runtime.
[cleanup.json](evidence/cleanup.json): **all_clean=true**. UID nie jest zachowany.
Zostają wyłącznie zamierzone evidence i nieinstalowany bundle w dist/hubi6.

## 13. Live v5 fingerprint

[live-before.json](evidence/live-before.json) i [live-after.json](evidence/live-after.json):
**identical=true**. Wszystkie **9** zastane sesje/panele i odpowiadające scope'y
zachowały tożsamości/generacje/czasy/PID. Service MainPID=172, NRestarts=0;
socket inode i zainstalowane pliki bez zmian. Fingerprinty po wcześniejszych
przebiegach również były zgodne. **Live v5 UNCHANGED.**

Fingerprint jest read-only: service/scope InvocationID, MainPID/NRestarts,
session/pane identity, created time, pane PID, socket inode i SHA256 trzech
zainstalowanych plików. Nie obejmuje zmiennej listy potomków aktywnych agentów
ani nie twierdzi, że uwierzytelnione rozmowy modeli zostały przetestowane.

## 14. GitHub / końcowe potwierdzenia

PR #8 aktualizowany na `feat/hubi-v6-python`, nadal **DRAFT**. No merge, no release,
no owner installation, no live migration, no cutover. Gotowe do niezależnego
Claude targeted re-review z naciskiem na nowe input/worker/pane repair/installer
regresje i residual boundaries. STOP po handoffie.
