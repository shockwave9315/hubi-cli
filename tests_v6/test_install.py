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
    def test_uninstall_preserves_changed_unit_and_can_retry(self):
        app=bootstrap()
        project=Path(__file__).resolve().parent.parent
        bundle=Path.home()/'unverified-install-bundle'
        shutil.rmtree(bundle,ignore_errors=True)
        shutil.copytree(project/'dist/hubi6',bundle,ignore=shutil.ignore_patterns('__pycache__'))
        install=['python3',str(bundle/'install.py'),'install']
        if json.loads((bundle/'SOURCE.json').read_text())['dirty']:install.append('--allow-dirty')
        def run(*args):
            return subprocess.run(list(args),cwd='/',env=dict(os.environ,PYTHONPATH=''),capture_output=True,text=True)
        unit=Path.home()/'.config/systemd/user/hubi6-tmux.service'
        original=None
        try:
            result=run(*install);self.assertEqual(result.returncode,0,result.stderr)
            original=unit.read_bytes()
            changed=original+b'\n# externally modified; preserve this file\n'
            unit.write_bytes(changed)
            result=run('python3',str(bundle/'install.py'),'uninstall')
            self.assertNotEqual(result.returncode,0)
            self.assertIn('changed',result.stderr)
            self.assertEqual(unit.read_bytes(),changed)
            self.assertTrue((Path.home()/'.local/share/hubi6/install.json').exists())
            unit.write_bytes(original)
            result=run('python3',str(bundle/'install.py'),'uninstall')
            self.assertEqual(result.returncode,0,result.stderr)
        finally:
            if original is not None and unit.exists():unit.write_bytes(original)
            run('python3',str(bundle/'install.py'),'uninstall')
            bootstrap()

    def test_uninstall_resumes_after_partial_failure_and_is_idempotent(self):
        app=bootstrap()
        project=Path(__file__).resolve().parent.parent
        bundle=Path.home()/'resumable-install-bundle'
        shutil.rmtree(bundle,ignore_errors=True)
        shutil.copytree(project/'dist/hubi6',bundle,ignore=shutil.ignore_patterns('__pycache__'))
        bin_dir=Path.home()/'.local/bin';bin_dir.mkdir(parents=True,exist_ok=True)
        alias=bin_dir/'hubi';alias.unlink(missing_ok=True)
        fallback=b'#!/bin/sh\necho exact-v5-fallback\n'
        alias.write_bytes(fallback);alias.chmod(0o751)
        source=json.loads((bundle/'SOURCE.json').read_text())
        install=['python3',str(bundle/'install.py'),'install','--as-hubi']
        if source['dirty']:install.append('--allow-dirty')
        def run(*args):
            return subprocess.run(list(args),cwd='/',env=dict(os.environ,PYTHONPATH=''),capture_output=True,text=True)
        try:
            result=run(*install);self.assertEqual(result.returncode,0,result.stderr)
            app.c.prepare()
            app.c.cache_base.mkdir(mode=0o700,parents=True,exist_ok=True)
            app.c.cache_base.chmod(0o755)  # failure after unit/launchers/prefix/state cleanup
            result=run('python3',str(bundle/'install.py'),'uninstall')
            self.assertNotEqual(result.returncode,0)
            self.assertIn('0700',result.stderr)
            registry=Path.home()/'.local/share/hubi6'
            self.assertTrue(json.loads((registry/'install.json').read_text())['uninstalling'])
            self.assertFalse((Path.home()/'.local/lib/hubi6').exists())
            self.assertFalse((Path.home()/'.config/systemd/user/hubi6-tmux.service').exists())
            self.assertEqual(alias.read_bytes(),fallback)
            self.assertEqual(alias.stat().st_mode & 0o777,0o751)
            restored=alias.stat().st_ino,alias.stat().st_mtime_ns
            app.c.cache_base.chmod(0o700)
            result=run('python3',str(bundle/'install.py'),'uninstall')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(alias.read_bytes(),fallback)
            self.assertEqual((alias.stat().st_ino,alias.stat().st_mtime_ns),restored)
            result=run('python3',str(bundle/'install.py'),'uninstall')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(alias.read_bytes(),fallback)
            self.assertEqual((alias.stat().st_ino,alias.stat().st_mtime_ns),restored)
            for path in (registry,app.c.state_base,app.c.cache_base,app.c.runtime_base/'hubi6',bin_dir/'hubi6'):
                self.assertFalse(path.exists(),path)
            print('Resumable uninstall: partial cache-permission failure, retry with absent unit, repeated uninstall, exact v5 fallback once: PASS',flush=True)
        finally:
            if app.c.cache_base.exists():app.c.cache_base.chmod(0o700)
            run('python3',str(bundle/'install.py'),'uninstall')
            bootstrap()

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
