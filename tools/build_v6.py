#!/usr/bin/env python3
"""Build an uninstalled directory bundle for distribution/review, stdlib only."""
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parent.parent
out = root/"dist/hubi6"
out.mkdir(parents=True, exist_ok=True)
shutil.copytree(root/"hubi6", out/"hubi6", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
(out/"hubi").write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parent))\nfrom hubi6.cli import main\nraise SystemExit(main())\n')
(out/"hubi").chmod(0o755)
(out/"config").mkdir(exist_ok=True)
(out/"systemd/user").mkdir(parents=True, exist_ok=True)
for name in ("hubi6-tmux.conf", "hubi6.example.toml"):
    shutil.copy(root/"config"/name, out/"config"/name)
shutil.copy(root/"systemd/user/hubi6-tmux.service", out/"systemd/user/hubi6-tmux.service")
revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
(out/"REVISION").write_text(revision+"\n")
print(out)
