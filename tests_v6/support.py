import os
from pathlib import Path
import pwd
import shutil
import subprocess

from hubi6.config import Config
from hubi6.services import App


LAB = os.environ.get("HUBI6_LAB") == "1" and pwd.getpwuid(os.getuid()).pw_name == "hubi6test" and os.getuid() not in (0, 1000)


def bootstrap():
    assert LAB, "Real/destructive runtime tests require tools/v6_lab.py and a separate UID"
    assert Path.home() == Path("/home/hubi6test")
    assert (Path.home()/".hubi6-disposable-lab").read_text().strip() == "hubi6-only"
    home = Path.home()
    conf = home/".config/hubi6"
    units = home/".config/systemd/user"
    conf.mkdir(parents=True, exist_ok=True, mode=0o700)
    units.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent
    shutil.copy(root/"config/hubi6-tmux.conf", conf/"tmux.conf")
    shutil.copy(root/"systemd/user/hubi6-tmux.service", units/"hubi6-tmux.service")
    fake = home/"fake-agent.py"
    fake.write_text('''#!/usr/bin/python3
import json,os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGHUP,signal.SIG_IGN)
print("FAKE AGENT READY",flush=True)
Path(".agent-"+str(os.getpid())).write_text(json.dumps({"argv":sys.argv,"env":dict(os.environ)}))
while True: time.sleep(.1)
''')
    fake.chmod(0o700)
    (conf/"config.toml").write_text('[agents]\ncodex = ["/usr/bin/python3", "'+str(fake)+'"]\nclaude = ["/usr/bin/python3", "'+str(fake)+'", "--permission-mode", "bypassPermissions"]\n')
    (home/"repos").mkdir(exist_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", "hubi6-tmux.service"], check=True)
    import time
    app = App(Config.load())
    for _ in range(100):
        if app.snapshot().runtime_health == "OK":
            return app
        time.sleep(.02)
    raise AssertionError("Isolated runtime did not become ready")


def repo(name):
    path = Path.home()/"repos"/name
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path


if __name__ == "__main__":
    app = bootstrap()
    ref = repo("smoke")
    r = app.start("codex", str(ref))
    print(r.id)
    print(app.snapshot().data())
    app.stop(r.id)
    print("SMOKE PASS")
