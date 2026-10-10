import os
from pathlib import Path
import re

from ..core.model import HubiError, Record, Session, tmux_format


FORMAT = "P\t#{session_name}\t#{session_id}\t#{pane_id}\t#{pane_dead}\t#{pane_pid}\t#{session_attached}\t#{@hubi6}"


class Tmux:
    def __init__(self, config, runner):
        self.c, self.r = config, runner

    def command(self, *args, **kw):
        return self.r.run(["tmux", "-N", "-S", str(self.c.socket), *args], **kw)

    def inventory(self):
        p = self.command("display-message", "-p", "H\t#{pid}\t#{exit-empty}", ";",
                         "list-panes", "-a", "-F", FORMAT, check=False)
        sessions, errors, pid, exit_empty = {}, [], 0, "unknown"
        if p.returncode and not (p.stderr.strip() == "no current target" and p.stdout.startswith("H\t")):
            return sessions, False, pid, exit_empty, ["tmux unavailable"]
        for line in p.stdout.splitlines():
            fields = line.split("\t")
            if fields[0] == "H" and len(fields) == 3:
                try:
                    pid = int(fields[1])
                    exit_empty = fields[2]
                except ValueError:
                    errors.append("Invalid tmux server identity")
            elif fields[0] == "P" and len(fields) == 8:
                _, name, sid, pane, dead, process, attached, meta = fields
                if not name.startswith("hubi6-"+self.c.runtime_id+"-"):
                    continue
                try:
                    if not re.fullmatch(r"\$[0-9]+", sid) or not re.fullmatch(r"%[0-9]+", pane) or dead not in ("0", "1"):
                        raise ValueError()
                    s = Session(name, sid, pane, dead == "1", int(process), int(attached), meta)
                    try:
                        s.cwd = os.readlink(f"/proc/{s.pid}/cwd")
                    except OSError:
                        pass
                    if name in sessions:
                        old = sessions[name]
                        s.panes = old.panes+1
                        try:
                            primary = Record.decode(meta, self.c.runtime_id, os.getuid()).pane
                        except HubiError:
                            primary = old.pane
                        if old.pane == primary:
                            old.panes = s.panes
                            continue
                    sessions[name] = s
                except ValueError:
                    errors.append("Malformed tmux pane inventory")
            else:
                errors.append("Malformed tmux inventory row")
        return sessions, bool(pid) and not errors, pid, exit_empty, errors

    def enforce(self):
        # Guard later option changes. Restart=always also covers empty-server
        # exit during a source-file; every creation preflight resets the option.
        self.command("set-option", "-g", "exit-empty", "off", ";", "set-hook", "-g", "after-set-option[900]",
                     "set-option -g exit-empty off")

    def create(self, record):
        # No repo text is ever a command. -c is a tmux format, hence explicit # escaping.
        p = self.command("new-session", "-d", "-s", record.session, "-c", tmux_format(record.repo.path),
                         "-P", "-F", "#{pane_id}", "--", "/usr/bin/sleep", "infinity")
        pane = p.stdout.strip()
        if not re.fullmatch(r"%[0-9]+", pane):
            raise HubiError("tmux returned an invalid pane identity")
        record.pane = pane
        self.metadata(record)

    def metadata(self, record):
        # set-option resolves -t as a window/pane target, unlike kill-session's
        # exact-name grammar. Numeric pane IDs are exact and never reused.
        self.command("set-option", "-t", record.pane, "@hubi6", record.encoded(), ";",
                     "set-option", "-w", "-t", record.pane, "remain-on-exit", "on", ";",
                     "set-option", "-w", "-t", record.pane, "window-size", "largest")

    def launch(self, record, argv):
        self.command("respawn-pane", "-k", "-t", record.pane, "-c", tmux_format(record.repo.path), "--", *argv)

    def remove(self, record, *, repair=False):
        sessions, known, *_ = self.inventory()
        if not known:
            # Unavailable service is handled by lifecycle; never create a server.
            raise HubiError("Cannot verify tmux session for removal")
        s = sessions.get(record.session)
        if s is None:
            return
        if not repair:
            check = Record.decode(s.metadata, self.c.runtime_id, os.getuid())
            if check.id != record.id:
                raise HubiError("Session ownership mismatch; use reconcile")
        self.command("kill-session", "-t", "="+record.session)

    def attach(self, record, mode):
        args = ["attach-session"]
        if mode == "view":
            args.append("-r")
        elif mode == "takeover":
            args.append("-d")
        elif mode not in ("normal", "shared"):
            raise HubiError("Unknown attach mode")
        return self.command(*args, "-t", "="+record.session, interactive=True).returncode
