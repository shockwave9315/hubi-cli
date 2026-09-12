# Review and threat-model boundaries

## NO FALSE ROOT!!!!!!!!!!!!!!!

Hubi does NOT attempt to defend against a privileged local administrator
intentionally changing required host/runtime prerequisites while an operation
is in progress.

A finding is NOT a valid reliability, race, security, or persistence bug if
reproducing it requires an explicit root/sudo/administrator action whose sole
purpose is to invalidate an already-established prerequisite.

Examples of out-of-scope privileged interference include:

- disabling `Linger=yes`;
- stopping or killing the user's systemd manager;
- stopping or replacing `hubi-tmux.service`;
- killing Hubi-owned scopes directly;
- modifying cgroups, runtime directories, sockets, permissions, binaries, or
  system configuration behind Hubi's back;
- deliberately changing a required prerequisite in a narrow timing window in
  order to manufacture a race.

Do not report findings of the form:

> "If root/admin runs command X between check A and operation B, invariant Y
> can be violated."

That is privileged administrative interference, not a Hubi runtime bug.

A privileged-action scenario is in scope only when at least one of the
following is true:

1. Hubi itself performs or can cause the privileged state change.
2. An unprivileged Hubi user/process can cause the state change.
3. The state change can occur naturally during the documented supported
   workflow without deliberate administrator intervention.
4. Hubi explicitly claims to remain correct despite that specific privileged
   state change.
5. The review task explicitly asks for resilience against malicious or
   concurrent root/admin actions.

Reviewers must distinguish:

- state that can change naturally or through Hubi itself;
- state changed deliberately by root/admin.

Do not invent a privileged actor solely to make a race reproducible.

The normal threat/reliability model assumes that required host prerequisites
remain valid until the administrator intentionally changes them.
