import re
import os

from ..core.model import HubiError, Record, Unit


PROPERTIES = "Id,LoadState,ActiveState,InvocationID,ControlGroup,MainPID,Description,Restart,KillMode"


def parse_units(output: str) -> dict[str, Unit]:
    units = {}
    for block in output.strip().split("\n\n"):
        if not block.strip():
            continue
        fields = {}
        valid = True
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if not sep or key in fields:
                valid = False
            fields[key] = value
        name = fields.get("Id", "")
        if not name or name in units:
            raise HubiError("Malformed systemd inventory: missing or duplicate unit identity")
        active = fields.get("ActiveState", "unknown")
        if not {"Id", "LoadState", "ActiveState", "InvocationID", "ControlGroup", "Description"}.issubset(fields):
            valid = False
        if not valid or active not in ("active", "inactive", "failed", "activating", "deactivating", "reloading"):
            active = "unknown"
        inv = fields.get("InvocationID", "")
        if active in ("active", "activating", "deactivating", "reloading") and (not inv or not fields.get("ControlGroup")):
            valid = False
            active = "unknown"
        if inv and not re.fullmatch(r"[a-f0-9]{32}", inv):
            active = "unknown"
        try:
            pid = int(fields.get("MainPID", "0"))
        except ValueError:
            pid, active = 0, "unknown"
        units[name] = Unit(name, active, inv, fields.get("ControlGroup", ""), pid,
                           fields.get("Description", ""), fields.get("Restart", ""))
    return units


class Systemd:
    def __init__(self, config, runner):
        self.c, self.r = config, runner
        self.other_runtime_scopes = set()

    def inventory(self, *, include_other=False):
        self.other_runtime_scopes.clear()
        p = self.r.run(["systemctl", "--user", "show", self.c.service,
                        "hubi6-*.scope" if include_other else f"hubi6-{self.c.runtime_id}-*.scope",
                        "--no-pager", "--property="+PROPERTIES], check=False)
        units = parse_units(p.stdout)
        # A missing service gives exit 1; a reachable manager still supplies Id.
        known = bool(units) and (p.returncode == 0 or all(u.active == "inactive" for u in units.values()))
        own = {}
        for name, unit in units.items():
            if name == self.c.service or name.startswith("hubi6-"+self.c.runtime_id+"-"):
                own[name] = unit
                continue
            match = re.match(r"hubi6-([a-f0-9]{20})-", name)
            if match and unit.live and unit.active != "unknown":
                try:
                    receipt = Record.decode(unit.description.split(":", 3)[3], match[1], os.getuid())
                    if receipt.scope == name and self.owns_description(receipt, unit.description) and unit.cgroup.endswith("/"+name):
                        self.other_runtime_scopes.add(name)
                except (HubiError, IndexError):
                    pass
        return own, known

    def unit(self, name):
        if name != self.c.service and not re.fullmatch(r"hubi6-"+self.c.runtime_id+r"-(codex|claude|terminal)-[a-f0-9]{20}-[a-f0-9]{16}\.scope", name):
            raise HubiError("Refusing foreign systemd unit")
        p = self.r.run(["systemctl", "--user", "show", name, "--no-pager", "--property="+PROPERTIES], check=False)
        units = parse_units(p.stdout)
        u = units.get(name)
        if u is None or (p.returncode and u.active not in ("inactive", "failed")):
            raise HubiError("Systemd state UNKNOWN; operation refused")
        return u

    def linger(self):
        p = self.r.run(["loginctl", "show-user", self.c.user, "--property=Linger", "--value"], check=False)
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() in ("yes", "no") else "unknown"

    def capabilities(self):
        p = self.r.run(["systemctl", "kill", "--help"])
        return "--kill-whom=" in p.stdout and "--signal=" in p.stdout

    def verify_scope(self, record, invocation):
        u = self.unit(record.scope)
        if u.active == "unknown":
            raise HubiError("Scope state UNKNOWN")
        if u.live:
            if not invocation or u.invocation != invocation or not self.owns_description(record, u.description):
                raise HubiError("Scope generation/ownership changed; operation refused")
            if not u.cgroup.endswith("/"+record.scope):
                raise HubiError("Scope cgroup identity mismatch")
        return u

    @staticmethod
    def owns_description(record, value):
        return value.startswith(record.description+":")

    def kill(self, record, invocation, signal):
        if signal not in ("HUP", "TERM", "KILL", "INT"):
            raise HubiError("Unsupported signal")
        u = self.verify_scope(record, invocation)
        if not u.live:
            return
        self.r.run(["systemctl", "--user", "kill", "--kill-whom=all", "--signal="+signal,
                    record.scope], check=False)
        # Caller always verifies disappearance; an unsuccessful kill is never success.
