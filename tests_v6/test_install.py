"""Fresh bundle install/CLI/uninstall under the marked lab UID only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_runtime import Client, wait_for


@unittest.skipUnless(LAB, 'installation requires disposable UID')
class InstallTests(unittest.TestCase):
    def test_fresh_bundle_install_workflows_and_uninstall(self):
        app=bootstrap()
        project=Path(__file__).resolve().parent.parent
        bundle=Path.home()/'install-bundle'
        shutil.rmtree(bundle,ignore_errors=True)
        shutil.copytree(project/'dist/hubi6',bundle,ignore=shutil.ignore_patterns('__pycache__'))
        source=json.loads((bundle/'SOURCE.json').read_text())
        if os.environ.get('HUBI6_REQUIRE_CLEAN_BUILD')=='1':
            self.assertFalse(source['dirty'],'Final install validation requires clean source build')
        bin_dir=Path.home()/'.local/bin';bin_dir.mkdir(parents=True,exist_ok=True)
        fallback=Path.home()/'v5-fallback-fixture'
        fallback.write_text('#!/bin/sh\necho v5-fallback-fixture\n');fallback.chmod(0o755)
        (bin_dir/'hubi').unlink(missing_ok=True)
        (bin_dir/'hubi').symlink_to(fallback)
        backup=(bin_dir/'hubi').read_bytes()
        args=['python3',str(bundle/'install.py'),'install','--as-hubi']
        if source['dirty']:args.append('--allow-dirty')
        installed=False
        def run(*args):
            return subprocess.run(list(args),cwd='/',env=dict(os.environ,PYTHONPATH=''),capture_output=True,text=True)
        try:
            result=run(*args);self.assertEqual(result.returncode,0,result.stderr);installed=True
            self.assertEqual(fallback.read_bytes(),backup) # symlink target must never be overwritten
            wait_for(lambda:app.runtime_snapshot().runtime_health=='OK')
            result=run(str(bin_dir/'hubi'),'--version')
            self.assertEqual(result.stdout.strip(),'Hubi 6.0.0')
            result=run(str(bin_dir/'hubi6'),'doctor','--json')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'],'PASS')
            path=repo('installed-bundle-workflow')
            records=[]
            for kind in ('codex','claude','terminal'):
                result=run(str(bin_dir/'hubi6'),'start',kind,str(path),'--json')
                self.assertEqual(result.returncode,0,result.stderr)
                data=json.loads(result.stdout);records.append(data['key']+'-'+data['generation'])
            result=run(str(bin_dir/'hubi6'),'list','--json')
            self.assertEqual(len(json.loads(result.stdout)['workloads']),3)
            result=run('python3',str(bundle/'install.py'),'uninstall')
            self.assertEqual(result.returncode,2)
            self.assertIn('Stop all v6 scopes',result.stderr)
            for identity in records:
                result=run(str(bin_dir/'hubi6'),'inspect',identity)
                self.assertEqual(result.returncode,0,result.stderr)
                client=Client([],command=[str(bin_dir/'hubi6'),'attach',identity,'--mode','shared'])
                try:
                    wait_for(lambda:app.find(identity).session.attached==1)
                    client.send(b'\x02d');self.assertEqual(client.p.wait(3),0)
                finally:client.close()
                result=run(str(bin_dir/'hubi6'),'stop',identity)
                self.assertEqual(result.returncode,0,result.stderr)
            result=run('python3',str(bundle/'install.py'),'uninstall')
            self.assertEqual(result.returncode,0,result.stderr);installed=False
            self.assertEqual((bin_dir/'hubi').read_bytes(),backup)
            self.assertFalse((path/'.git/hubi6-workspace-id').exists())
            self.assertFalse((bin_dir/'hubi6').exists())
            for p in (Path.home()/'.local/lib/hubi6',app.c.socket,app.c.state_base,app.c.cache_base):
                self.assertFalse(p.exists(),p)
            self.assertFalse(app.r.run(['systemctl','--user','list-units','--all','--no-legend','hubi6-*.scope']).stdout.strip())
            print('Fresh bundle install: Codex/Claude/terminal, attach/inspect/stop, uninstall/rollback alias: PASS; dirty='+str(source['dirty']),flush=True)
        finally:
            if installed:
                for w in app.runtime_snapshot().workloads:app.stop(w.record.id,repair=True)
                run('python3',str(bundle/'install.py'),'uninstall')
            bootstrap()  # restore only the disposable test runtime for later cases
