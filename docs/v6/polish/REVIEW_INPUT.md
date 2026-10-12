# Final polish input

Baseline: `ba22947b86537e9fc42a6edc3e96c0998f8f93e6`, branch
`feat/hubi-v6-python`, PR [#8](https://github.com/shockwave9315/hubi-cli/pull/8).

On 2026-10-11 the GitHub issue-comments API returned one comment: the original
[Claude deep review](https://github.com/shockwave9315/hubi-cli/pull/8#issuecomment-6109830600)
for `36fa688`, already preserved in [the earlier input](../fix-set/CLAUDE_REVIEW_INPUT.md).
The review API and inline-review-comments API returned no entries; the PR body
still referred to the preceding fix-set. That complete available comment was
read. A separate targeted re-review comment was not available through these
endpoints. The owner's detailed final-polish instructions supplied the targeted
findings and acceptance criteria; no targeted review text is invented here.

Authorized local changes:

1. P3-A: accepted lines return immediately; render consumes the input quarantine;
   the next read drains until 80 ms of quiet measured from the last received byte.
   Destructive prompts are shown only after the drain. Test raw/split bursts,
   bracketed paste and detach; prove a quarantine-removal mutant fails.
2. P3-B: ordinary directories without `.git` are skipped. Real filesystem errors
   and damaged repositories remain issues; runtime/ownership issues precede repo
   diagnostics in the limited menu space.
3. P4 pane repair: missing journal plus session receipt without invocation can
   recover from the exact verified scope receipt, with runtime, generation,
   session, scope and decoded description agreement.
4. P4 uninstall: persist receipts through partial cleanup; tolerate already
   absent files/units after zero-state; preserve unverified files; restore the
   exact fallback once; retry a deliberately interrupted uninstall.
5. P4 first start: immediate post-exec exit 23 is failure in the narrow ack window,
   remains EXITED/23 in JSON/inspect, and supports explicit stop.
6. P4 F8: replace probabilistic negative-control contention assertions with a
   deterministic environment/lock barrier regression and mutation detection.
7. P4 backspace: empty input does not erase the prompt; normal and UTF-8 deletion
   and echo are covered by PTY tests.
8. XDG UX: document separate state namespaces; doctor counts valid other-runtime
   objects informationally without adoption, stop or ownership weakening.

No runtime-topology change, Bash change, migration, owner install, cutover,
release or merge. Destructive tests use only disposable UID 1001. PR remains
DRAFT. Deliberate residuals: worst-point simultaneous launcher/worker SIGKILL,
hidden historical journals, exotic Git aliases and full large-inventory redesign.
