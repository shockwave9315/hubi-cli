#!/usr/bin/env python3
"""Fresh directory bundle with verifiable source hashes and explicit dirty state."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def build(root):
    root = Path(root).resolve()
    dist = root/"dist"
    dist.mkdir(exist_ok=True)
    out = dist/"hubi6"
    staging = Path(tempfile.mkdtemp(prefix=".hubi6-build-", dir=dist))
    try:
        inputs = sorted((root/"hubi6").rglob("*.py")) + [root/p for p in (
            "config/hubi6-tmux.conf", "config/hubi6.example.toml", "systemd/user/hubi6-tmux.service",
            "tools/build_v6.py", "tools/install_v6.py")]
        files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root))
        revision = head+("-dirty-"+digest[:12] if dirty else "")
        for p in inputs:
            if p.parent == root/"tools":
                continue
            destination = staging/p.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, destination)
        shutil.copy2(root/"tools/install_v6.py", staging/"install.py")
        (staging/"hubi").write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parent))\nfrom hubi6.cli import main\nraise SystemExit(main())\n')
        (staging/"hubi").chmod(0o755)
        (staging/"REVISION").write_text(revision+"\n")
        (staging/"SOURCE.json").write_text(json.dumps({"head": head, "dirty": dirty, "source_sha256": digest, "files": files}, indent=2)+"\n")
        if out.is_symlink():
            out.unlink()
        elif out.exists():
            shutil.rmtree(out)
        staging.rename(out)
        return out
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    print(build(Path(__file__).resolve().parent.parent))
