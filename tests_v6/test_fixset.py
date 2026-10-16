"""Regressions derived from Claude's complete PR #8 review, not implementation mirrors."""
from dataclasses import replace
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

from hubi6.adapters.git import Git, normalized_origin
from hubi6.config import Config
from hubi6.core.model import HubiError, State, RepoHealth
from hubi6.doctor import doctor
from hubi6.adapters.tmux import NEW_PANE_HINT
from hubi6.i18n import pl
from hubi6.services import App
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_runtime import Client, wait_for


class FixsetUnitTests(unittest.TestCase):
    def test_config_errors_are_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.toml"
            with self.assertRaisesRegex(HubiError, "does not exist"):
                Config.load(str(path))
            for text in ('unknown = 1', 'repos_root = 7', 'agents = 1', '[github]\ncache_ttl = true',
                         '[github]\nclone_timeout = "slow"', '[stop]\nagent_int_grace = -1',
                         '[stop]\nunknown = 1', 'lock_timeout = false', '[agents]\ncodex = 8'):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(HubiError):
                    Config.load(str(path))
            path.write_text('[stop]\nagent_int_grace=3.5\n[github]\nclone_timeout=900')
            c = Config.load(str(path))
            self.assertEqual(c.agent_int_grace, 3.5)
            self.assertEqual(c.clone_timeout, 900)

    def test_origin_common_github_hosts(self):
        for url in ('https://www.github.com/OWNER/repo.git/', 'ssh://git@ssh.github.com:443/Owner/repo.git',
                    'git@www.github.com:owner/repo', 'ssh://git@github.com:22/owner/repo'):
            self.assertEqual(normalized_origin(url), 'github.com/owner/repo')
        self.assertEqual(normalized_origin('https://www.github.com.evil/owner/repo'), '')

    def test_discovery_isolates_stat_errors_without_error_path_probe(self):
        c = Config.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'loop').symlink_to('loop')
            (root/'broken').symlink_to(root/'missing')
            git = Git(replace(c, repos=root), None)
            refs, issues = git.discover()
            self.assertFalse(refs)
            self.assertTrue(any('loop' in issue for issue in issues))
            self.assertTrue(any('broken' in issue for issue in issues))
            with patch('hubi6.adapters.git.reference', side_effect=PermissionError(errno.EACCES, 'denied')):
                (root/'unreadable').mkdir()
                (root/'unreadable/.git').mkdir()
                self.assertTrue(any('unreadable' in issue for issue in git.discover()[1]))

    def test_doctor_warn_fail_and_json_semantics(self):
        from unittest.mock import Mock
        app = Mock(c=replace(Config.load(), agents={'codex':['codex'], 'claude':['claude'], 'terminal':['/bin/bash','-i']}))
        snapshot = Mock(runtime_health='OK', exit_empty='0', systemd_known=True, issues=[], workloads=[], other_runtime_objects=0)
        snapshot.service.restart = 'always'
        app.snapshot.return_value = snapshot
        app.systemd.linger.return_value = 'yes'
        app.systemd.capabilities.return_value = True
        for missing in ('codex', 'claude', 'gh', 'git', 'tmux', 'systemd-run', 'loginctl'):
            with patch('hubi6.doctor.shutil.which', side_effect=lambda name: None if name == missing else '/bin/mock'), patch('hubi6.doctor.check_layout', return_value=True):
                result = doctor(app)
                self.assertEqual(result['status'], 'WARN' if missing in ('codex','claude','gh') else 'FAIL')
                self.assertFalse(result['ready'])
                self.assertEqual(result['runtime_ready'], missing in ('codex','claude','gh'))

    def test_empty_short_and_ambiguous_prefixes(self):
        from unittest.mock import Mock
        app = App(Config.load())
        with patch.object(app, 'runtime_snapshot', side_effect=AssertionError('invalid IDs must fail before probing')):
            for prefix in ('', 'a', 'abc1234', ' ', '*', 'a'*38):
                for method in (app.find, app.stop):
                    with self.subTest(prefix=prefix), self.assertRaises(HubiError):
                        method(prefix)
        snapshot = Mock(workloads=[Mock(record=Mock(id='a'*20+'-'+'b'*16)), Mock(record=Mock(id='a'*20+'-'+'c'*16))])
        with self.assertRaisesRegex(HubiError,'ambiguous'):
            app.find('a'*8, snapshot)
        self.assertEqual(app.find('a'*20+'-'+'b'*16, snapshot).record.id, 'a'*20+'-'+'b'*16)

    def test_debug_traceback(self):
        from hubi6.cli import main
        output = io.StringIO()
        with patch('hubi6.cli.execute', side_effect=RuntimeError('diagnostic-test')), patch('sys.stderr', output):
            self.assertEqual(main(['--debug','list']),1)
        self.assertIn('Traceback', output.getvalue())
        self.assertIn('diagnostic-test', output.getvalue())

    def test_fresh_build_no_stale_files_and_dirty_identity(self):
        from tools.build_v6 import build
        source = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('hubi6','config','systemd','tools'):
                shutil.copytree(source/name, root/name, ignore=shutil.ignore_patterns('__pycache__'))
            (root/'.gitignore').write_text('dist/\n__pycache__/\n')
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            subprocess.run(['git','-C',str(root),'-c','user.name=Lab','-c','user.email=lab@example.invalid','-c','commit.gpgsign=false','commit','-qm','build fixture'],check=True)
            out = build(root)
            revision = (out/'REVISION').read_text()
            self.assertFalse(json.loads((out/'SOURCE.json').read_text())['dirty'])
            (out/'hubi6/stale_module.py').write_text('obsolete=True')
            out = build(root)
            self.assertFalse((out/'hubi6/stale_module.py').exists())
            (root/'hubi6/__init__.py').write_text('__version__="changed"\n')
            out = build(root)
            self.assertIn('-dirty-', (out/'REVISION').read_text())
            self.assertNotEqual(revision, (out/'REVISION').read_text())
            self.assertTrue(json.loads((out/'SOURCE.json').read_text())['dirty'])


@unittest.skipUnless(LAB, 'requires disposable UID; never run lifecycle as owner')
class FixsetRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        shutil.rmtree(self.app.c.repos/('fix-'+self._testMethodName), ignore_errors=True)
        self.path = repo('fix-'+self._testMethodName)

    def tearDown(self):
        for w in self.app.runtime_snapshot().workloads:
            self.app.stop(w.record.id, repair=True)
            if any(x.record.id == w.record.id for x in self.app.runtime_snapshot().workloads):
                self.app.stop(w.record.id, repair=True)

    def start(self, kind='codex'):
        return self.app.start(kind,str(self.path))

    def menu(self):
        client = Client([])
        self.addCleanup(client.close)
        client.read_until(b'Choice + Enter:')
        return client

    def send_prompt(self, client, text, needle=b'Choice + Enter:'):
        # A rendered normal screen can precede input readiness by the remainder
        # of the burst window. Manual responses start after that quiet period;
        # paste regressions below still send their entire burst without pauses.
        time.sleep(.1)
        client.output=b''
        client.send(text)
        client.read_until(needle)

    def test_bad_entries_do_not_block_inventory_or_lifecycle(self):
        r = self.start('terminal')
        bad = []
        denied = self.app.c.repos/'fix-denied'
        denied.mkdir(exist_ok=True)
        denied.chmod(0)
        bad.append(denied)
        for name,target in (('fix-loop','fix-loop'),('fix-broken','fix-missing'),('fix-unreadable',str(denied/'subdir'))):
            link=self.app.c.repos/name
            link.symlink_to(target)
            bad.append(link)
        try:
            snapshot = self.app.snapshot()
            self.assertTrue(any('fix-loop' in issue for issue in snapshot.issues))
            self.assertTrue(any('fix-unreadable' in issue for issue in snapshot.issues))
            self.assertTrue(any('fix-broken' in issue for issue in snapshot.issues))
            with patch.object(self.app.git,'discover',side_effect=AssertionError('lifecycle scanned repos')):
                self.assertEqual(self.app.find(r.id).state, State.RUNNING)
                client=Client(['attach',r.id])
                try:
                    wait_for(lambda:self.app.find(r.id).session.attached==1)
                    client.send(b'\x02d')
                    self.assertEqual(client.p.wait(3),0)
                finally:
                    client.close()
                self.app.stop(r.id)
                r=self.start('terminal')
                self.app.stop(r.id,repair=True)
            # CLI inspect/attach/stop must also bypass filesystem discovery.
            r=self.start('terminal')
            script='from unittest.mock import patch;from hubi6.cli import main;import sys;patch("hubi6.adapters.git.Git.discover",side_effect=AssertionError("SCANNED")).start();sys.exit(main(sys.argv[1:]))'
            for args in (['inspect',r.id],['stop',r.id]):
                p=subprocess.run([sys.executable,'-c',script,*args],text=True,capture_output=True)
                self.assertEqual(p.returncode,0,p.stderr)
        finally:
            denied.chmod(0o700)
            for p in bad:
                if p.is_symlink():p.unlink()
            shutil.rmtree(denied)

    def test_custom_state_home_worker_and_interrupted_recovery(self):
        custom=Path.home()/'custom-state'
        with patch.dict(os.environ,{'XDG_STATE_HOME':str(custom)}):
            app=App(Config.load())
            self.assertEqual(app.c.state_base,custom/'hubi6')
            self.assertEqual(doctor(app)['status'],'PASS')
            for kind in ('terminal','codex'):
                r=app.start(kind,str(self.path))
                self.assertEqual(app.find(r.id).state,State.RUNNING)
                self.assertTrue(app.store.path(r).is_relative_to(custom))
                app.stop(r.id)
            # Intent is on disk while the launcher is killed before tmux launch.
            script='from hubi6.services import App;from hubi6.config import Config;from unittest.mock import patch;import time;app=App(Config.load());patch.object(app.tmux,"launch",side_effect=lambda *a:time.sleep(30)).start();app.start("codex",'+repr(str(self.path))+')'
            proc=subprocess.Popen([sys.executable,'-c',script],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                wait_for(lambda:any(app.c.records.glob('*.json')))
                proc.kill();proc.wait()
                for w in app.runtime_snapshot().workloads:
                    app.stop(w.record.id,repair=True)
                r=app.start('codex',str(self.path));app.stop(r.id)
                self.assertFalse(app.snapshot().workloads)
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
            app.c.state_base.chmod(0o755)
            try:self.assertEqual(doctor(app)['status'],'FAIL')
            finally:app.c.state_base.chmod(0o700)

    def test_live_scope_tmux_unavailable_is_unknown_and_closed(self):
        r=self.start()
        with patch.object(self.app.tmux,'inventory',side_effect=HubiError('unavailable')):
            self.assertEqual(self.app.find(r.id).state,State.UNKNOWN)
            self.assertEqual(self.app.snapshot().data()['workloads'][0]['state'],'UNKNOWN')
            self.assertEqual(doctor(self.app)['status'],'FAIL')
            for repair in (False,True):
                with self.assertRaises(HubiError):self.app.stop(r.id,repair=repair)
        script='from hubi6.cli import main;from unittest.mock import patch;from hubi6.core.model import HubiError;import sys;patch("hubi6.adapters.tmux.Tmux.inventory",side_effect=HubiError("unavailable")).start();sys.exit(main(sys.argv[1:]))'
        p=subprocess.run([sys.executable,'-c',script,'list','--json'],capture_output=True,text=True)
        self.assertEqual(p.returncode,1)
        self.assertEqual(json.loads(p.stdout)['runtime_health'],'UNKNOWN')
        client=Client([],command=[sys.executable,'-c',script,'attach',r.id])
        try:self.assertEqual(client.p.wait(3),2)
        finally:client.close()
        self.assertEqual(self.app.find(r.id).state,State.RUNNING)

    def test_extra_pane_repair_and_binding_reassertion(self):
        r=self.start('terminal')
        sibling=self.app.start('terminal',str(self.path),'sibling')
        config=Path.home()/'restore-bindings.conf'
        config.write_text('bind c new-window\nbind % split-window -h\nbind \'"\' split-window\n')
        self.app.tmux.command('source-file',str(config))
        self.app.preflight()
        bindings=self.app.tmux.command('list-keys','-T','prefix').stdout
        for key in ('c', '%', '"'):
            lines=[line for line in bindings.splitlines() if line.split()[3].replace('\\','') == key]
            self.assertTrue(lines, key)
            self.assertTrue(all(NEW_PANE_HINT in line for line in lines), lines)
        self.app.tmux.command('split-window','-d','-t',r.pane,'--','/usr/bin/sleep','999')
        self.app.tmux.command('new-window','-d','-t','='+r.session+':','--','/usr/bin/sleep','999')
        self.assertEqual(self.app.find(r.id).state,State.UNKNOWN)
        self.assertEqual(self.app.find(r.id).data()['id'],r.id)
        with self.assertRaises(HubiError):self.app.stop(r.id)
        with self.assertRaises(HubiError):self.app.stop(r.id,expected='a'*16,repair=True)
        self.assertEqual(self.app.find(r.id).session.panes,3)
        self.app.tmux.command('split-window','-d','-t',sibling.pane,'--','/usr/bin/sleep','999')
        original=self.app.systemd.verify_scope(r,r.invocation)
        crossed=replace(original,description=r.description+':'+sibling.encoded())
        with patch.object(self.app.systemd,'verify_scope',return_value=crossed), self.assertRaisesRegex(HubiError,'receipt'):
            self.app.stop(r.id,repair=True)
        self.assertEqual(self.app.find(sibling.id).session.panes,2)
        self.app.stop(r.id,repair=True)
        self.assertEqual(self.app.find(r.id).state,State.RUNNING)
        self.assertEqual(self.app.find(r.id).session.panes,1)
        self.assertEqual(self.app.find(sibling.id).session.panes,2)
        self.app.stop(sibling.id,repair=True)
        self.assertEqual(self.app.find(sibling.id).state,State.RUNNING)
        self.app.stop(r.id)

    def test_slow_graceful_agent_shutdown(self):
        script=Path.home()/'slow-shutdown.py'
        script.write_text('import signal,time\nfrom pathlib import Path\ndef done(*a):\n time.sleep(1.05)\n Path("clean-shutdown").touch()\n raise SystemExit(0)\nsignal.signal(signal.SIGINT,done)\nPath("slow-ready").touch()\nwhile True:time.sleep(.05)\n')
        app=App(replace(self.app.c,agents=dict(self.app.c.agents,codex=[sys.executable,str(script)])))
        r=app.start('codex',str(self.path))
        wait_for(lambda:(self.path/'slow-ready').exists())
        started=time.monotonic();app.stop(r.id)
        self.assertTrue((self.path/'clean-shutdown').exists())
        self.assertGreater(time.monotonic()-started,1)
        self.assertFalse(app.runtime_snapshot().workloads)

    def test_stale_journal_hidden_after_runtime_generation_change(self):
        r=self.start('terminal')
        self.app.systemd.kill(r,r.invocation,'KILL')
        wait_for(lambda:self.app.find(r.id).state==State.EXITED)
        self.app.tmux.command('kill-session','-t','='+r.session)
        old=self.app.snapshot().server_pid
        self.app.tmux.command('kill-server')
        wait_for(lambda:self.app.runtime_snapshot().runtime_health=='OK' and self.app.runtime_snapshot().server_pid!=old)
        self.assertFalse(self.app.snapshot().workloads)
        self.assertTrue(self.app.store.path(r).exists()) # list/doctor didn't delete
        self.assertEqual(doctor(self.app)['status'],'PASS')
        newer=self.start('terminal')
        self.assertNotEqual(newer.generation,r.generation)
        self.assertFalse(self.app.store.path(r).exists())

    def test_exited_start_fails_and_reports_exit_status(self):
        app=App(replace(self.app.c,agents=dict(self.app.c.agents,codex=['/bin/sh','-c','sleep .2; exit 23'])))
        r=app.start('codex',str(self.path))
        wait_for(lambda:app.find(r.id).state==State.EXITED)
        self.assertEqual(app.find(r.id).data()['exit_status'],23)
        with self.assertRaisesRegex(HubiError,'EXITED'):app.start('codex',str(self.path))
        app.stop(r.id)
        r=self.start();self.assertEqual(self.app.find(r.id).state,State.RUNNING)

    def test_normal_terminal_rc_environment_and_nested_refusal(self):
        rc=Path.home()/'.bashrc';old=rc.read_text() if rc.exists() else None
        rc.write_text('test "$HUBI_NOAUTO" = 1 || touch "$HOME/AUTOLOGIN_LOOP"\nexport RC_LOADED=1\n')
        try:
            with patch.dict(os.environ,{'SHELL':'/bin/bash','SSH_AUTH_SOCK':'/tmp/synthetic-agent.sock','LANG':'pl_PL.UTF-8','LC_TIME':'C','TERM':'dumb','TMUX':'bad','TMUX_PANE':'%99','HUBI_ACTIVE':'bad','HUBI6_ACTIVE':'inherited-bad'}):
                app=App(Config.load());r=app.start('terminal',str(self.path))
            self.app.tmux.command('send-keys','-t',r.pane,'-l','python3 -c \'import os,json;open("terminal-env","w").write(json.dumps(dict(os.environ)))\'')
            self.app.tmux.command('send-keys','-t',r.pane,'Enter')
            wait_for(lambda:(self.path/'terminal-env').exists())
            env=json.loads((self.path/'terminal-env').read_text())
            self.assertEqual(env['HUBI6_ACTIVE'],'1');self.assertEqual(env['HUBI_NOAUTO'],'1')
            self.assertEqual(env['RC_LOADED'],'1');self.assertEqual(env['SSH_AUTH_SOCK'],'/tmp/synthetic-agent.sock')
            self.assertEqual(env['LANG'],'pl_PL.UTF-8');self.assertEqual(env['LC_TIME'],'C')
            self.assertEqual(env['TERM'],'screen-256color') # virtual tmux terminal, not launcher's dumb/xterm
            for key in ('TMUX','TMUX_PANE','HUBI_ACTIVE'):self.assertNotIn(key,env)
            self.assertFalse((Path.home()/'AUTOLOGIN_LOOP').exists())
            client=Client([],command=['env','HUBI6_ACTIVE=1',sys.executable,'-m','hubi6'])
            try:
                self.assertEqual(client.p.wait(3),2)
                client.read_until(pl('Hubi is already active').encode())  # CLI wording is Polish
            finally:client.close()
            client=Client([],command=['env','HUBI6_ACTIVE=1',sys.executable,'-m','hubi6','attach',r.id])
            try:self.assertEqual(client.p.wait(3),2)
            finally:client.close()
        finally:
            if old is None:rc.unlink(missing_ok=True)
            else:rc.write_text(old)

    def test_moved_workspace_does_not_duplicate_agent(self):
        r=self.start()
        moved=self.path.with_name(self.path.name+'-moved')
        self.path.rename(moved)
        try:
            with self.assertRaisesRegex(HubiError,'Moved workspace'):
                self.app.start('codex',str(moved))
            self.assertEqual(len(self.app.snapshot().workloads),1)
            self.app.stop(r.id)
            newer=self.app.start('codex',str(moved))
            self.assertEqual(self.app.find(newer.id).state,State.RUNNING)
        finally:
            for w in self.app.runtime_snapshot().workloads:self.app.stop(w.record.id,repair=True)
            moved.rename(self.path)

    def test_raw_paste_burst_cannot_cross_screens_or_confirm_stop(self):
        r=self.start('terminal')
        client=self.menu()
        self.send_prompt(client,b'w\n')
        self.send_prompt(client,b'1\nd\nyes\n')
        time.sleep(.2)
        self.assertEqual(self.app.find(r.id).state,State.RUNNING)
        self.assertNotIn(b'Type yes',client.output)
        # Ordinary fast typing within each prompt remains valid.
        self.send_prompt(client,b'd\n',b'Type yes')
        self.send_prompt(client,b'y\x7fes\n')
        self.assertEqual(self.app.find(r.id).state,State.RUNNING) # "es", not yes
        self.assertNotIn(b'\\u007f',client.output)
        self.send_prompt(client,b'd\n',b'Type yes')
        self.send_prompt(client,b'yes\n')
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_paste_after_real_tmux_detach_and_bracketed_paste(self):
        r=self.start('terminal');client=self.menu()
        self.send_prompt(client,b'w\n');self.send_prompt(client,b'1\n')
        time.sleep(.1)
        client.output=b'';client.send(b'a\n')
        wait_for(lambda:self.app.find(r.id).session.attached==1)
        client.send(b'\x02d')
        client.read_until(b'Choice + Enter:')
        self.assertGreater(client.output.rfind(b'\x1b[?2004h'),client.output.rfind(b'\x1b[?2004l'))
        self.send_prompt(client,b'd\nyes\n',b'Type yes')
        self.assertEqual(self.app.find(r.id).state,State.RUNNING)
        self.send_prompt(client,b'\x1b[200~yes\n\x1b[201~\n')
        self.assertEqual(self.app.find(r.id).state,State.RUNNING)
        self.send_prompt(client,b'q\n',b'\x1b[?2004l')
        self.assertEqual(client.p.wait(3),98)
        self.assertEqual(termios.tcgetattr(client.slave),client.before)

    def test_closed_stdin_and_real_pty_hangup_exit_codes(self):
        p=subprocess.run([sys.executable,'-m','hubi6'],preexec_fn=lambda:os.close(0),capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
        client=Client([])
        client.read_until(b'Choice + Enter:')
        os.close(client.master)
        self.assertEqual(client.p.wait(3),129)
        os.close(client.slave)

    def test_main_view_shows_unhealthy_workspace_even_when_not_discovered(self):
        r=self.start()
        moved=self.path.with_name(self.path.name+'-moved')
        self.path.rename(moved)
        try:
            for expected in (b'MISSING',b'REPLACED',b'INVALID'):
                if expected == b'REPLACED':repo(self.path.name)
                if expected == b'INVALID':
                    self.app.stop(r.id)
                    r=self.start()
                    (self.path/'.git/HEAD').write_text('broken')
                client=Client([])
                try:client.read_until(b'repo:'+expected)
                finally:client.close()
        finally:
            self.app.stop(r.id)
            shutil.rmtree(self.path)
            moved.rename(self.path)

    def test_github_clone_confirmation_progress_and_cancel(self):
        picked={'nameWithOwner':'owner/tui-clone','local':False,'local_paths':[], 'isPrivate':True,'isFork':False,'isArchived':False}
        script=('from unittest.mock import patch;from hubi6.cli import main;from hubi6.github import Catalogue;'
                'from hubi6.adapters.git import reference;from pathlib import Path;import sys;'
                'patch("hubi6.github.GitHub.cached",return_value=None).start();'
                'patch("hubi6.github.GitHub.list",return_value=Catalogue([],False,False)).start();'
                'patch("hubi6.github.GitHub.view",return_value=['+repr(picked)+']).start();'
                'patch("hubi6.github.GitHub.clone",side_effect=lambda name:(Path('+repr(str(self.path/'clone-called'))+').touch(),reference(Path('+repr(str(self.path))+')))[1]).start();'
                'sys.exit(main([]))')
        client=Client([],command=[sys.executable,'-c',script]);self.addCleanup(client.close)
        client.read_until(b'Choice + Enter:')
        self.send_prompt(client,b'g\n')
        self.send_prompt(client,b'1\nyes\n',b'Type yes:')
        self.assertFalse((self.path/'clone-called').exists())
        self.send_prompt(client,b'no\n')
        self.assertIn(b'Clone cancelled',client.output)
        self.send_prompt(client,b'1\n',b'Type yes:')
        self.send_prompt(client,b'yes\n')
        self.assertIn(b'Cloning...',client.output)
        self.assertIn(b'Cloned owner/tui-clone',client.output)
        self.assertTrue((self.path/'clone-called').exists())

    def test_git_read_status_write_deterministic_F8(self):
        # A real writer's hook holds an explicit index.lock behind a barrier. The shim
        # checks the subprocess environment, then execs real Git. It makes loss
        # of the optional-lock contract deterministic, independent of scheduling.
        (self.path/'tracked').write_text('tracked\n')
        git=['git','-C',str(self.path),'-c','user.name=Lab','-c','user.email=lab@example.invalid','-c','commit.gpgsign=false']
        subprocess.run(git+['add','.'],check=True)
        ready, release = self.path/'writer-ready', self.path/'writer-release'
        hook=self.path/'.git/hooks/pre-commit'
        hook.write_text('#!/usr/bin/python3\nfrom pathlib import Path\nimport time\nwith Path(".git/index.lock").open("x"):\n Path("writer-ready").touch()\n end=time.monotonic()+10\n while not Path("writer-release").exists():\n  if time.monotonic()>end:raise SystemExit(1)\n  time.sleep(.01)\nPath(".git/index.lock").unlink()\n')
        hook.chmod(0o700)
        shim=self.path/'shim';shim.mkdir()
        real=shutil.which('git'); log=self.path/'git-env.jsonl'
        wrapper=shim/'git'
        wrapper.write_text('#!/usr/bin/python3\nimport os,json,sys\nfrom pathlib import Path\nwith Path('+repr(str(log))+').open("a") as f: f.write(json.dumps({"argv":sys.argv[1:],"optional":os.environ.get("GIT_OPTIONAL_LOCKS")})+"\\n")\nif os.environ.get("GIT_OPTIONAL_LOCKS") != "0":\n print("optional-lock contract missing",file=sys.stderr)\n raise SystemExit(49)\nos.execv('+repr(real)+',['+repr(real)+']+sys.argv[1:])\n')
        wrapper.chmod(0o700)
        writer=subprocess.Popen(git+['commit','-qm','barrier fixture'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            wait_for(ready.exists)
            self.assertTrue((self.path/'.git/index.lock').exists())
            with patch.dict(os.environ,{'PATH':str(shim)+os.pathsep+os.environ['PATH']}):
                for _ in range(8):
                    self.app.git.summary(self.app.git.resolve(str(self.path)))
                safe=self.app.git.env()
                safe.pop('GIT_OPTIONAL_LOCKS')
                with patch.object(self.app.git,'env',return_value=safe), self.assertRaises(HubiError):
                    self.app.git.summary(self.app.git.resolve(str(self.path)))
            rows=[json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(rows),17)
            self.assertTrue(all(row['optional']=='0' for row in rows[:-1]))
            self.assertIsNone(rows[-1]['optional'])
            self.assertTrue(any('status' in row['argv'] for row in rows))
            self.assertTrue(any('rev-parse' in row['argv'] for row in rows))
        finally:
            release.touch()
            out,err=writer.communicate(timeout=5)
        self.assertEqual(writer.returncode,0,err)
        self.assertFalse((self.path/'.git/index.lock').exists())
        print('Git F8 deterministic barrier: 16 protected real Git reads, real writer committed; missing-env control rejected',flush=True)

    def test_worker_wrong_layout_refused_and_private_temp_cleanup(self):
        from hubi6.config import worker_config
        from hubi6.storage import atomic_json
        r=self.start()
        valid_config, valid_record=worker_config(str(self.app.store.path(r)),str(self.app.c.runtime/(r.id+'.payload')))
        self.assertEqual(valid_config.records,self.app.c.records)
        self.assertEqual(valid_record.id,r.id)
        tampered=r.data();tampered['runtime']='f'*20
        atomic_json(self.app.store.path(r),tampered)
        try:
            with self.assertRaises(HubiError):worker_config(str(self.app.store.path(r)),str(self.app.c.runtime/(r.id+'.payload')))
        finally:self.app.store.save(r)
        copied=self.app.c.state_base/'wrong-layout.json'
        atomic_json(copied,r.data())
        with self.assertRaises(HubiError):worker_config(str(copied),str(self.app.c.runtime/(r.id+'.payload')))
        copied.unlink()
        temp=self.app.c.records/'.hubi6-interrupted-temp'
        temp.write_text('partial');temp.chmod(0o600)
        os.utime(temp,(time.time()-90000,)*2)
        fresh=self.app.c.records/'.hubi6-recent-temp'
        fresh.write_text('active');fresh.chmod(0o600)
        self.app.store.cleanup_temps()
        self.assertFalse(temp.exists());self.assertTrue(fresh.exists())
        fresh.unlink()

    def test_all_receipts_lost_and_foreign_journal_remain_fail_closed(self):
        r=self.start()
        sibling=self.app.start('terminal',str(self.path),'sibling')
        self.app.systemd.kill(r,r.invocation,'KILL')
        wait_for(lambda:self.app.find(r.id).state==State.EXITED)
        self.app.tmux.command('set-option','-u','-t',r.pane,'@hubi6')
        self.app.store.path(r).unlink()
        try:
            snapshot=self.app.snapshot()
            self.assertEqual([w.record.id for w in snapshot.workloads],[sibling.id])
            self.assertTrue(any('manually' in issue and r.session in issue for issue in snapshot.issues))
            self.assertEqual(doctor(self.app)['status'],'FAIL')
            self.assertIn(r.session,self.app.tmux.inventory()[0])
            self.assertEqual(self.app.find(sibling.id).state,State.RUNNING)
        finally:
            # Operator cleanup of the exact session retained by this test's
            # original receipt; product correctly refuses guessing a lost one.
            self.app.tmux.command('kill-session','-t','='+r.session)
        foreign=self.app.c.records/'notes.json'
        foreign.write_text('{}');foreign.chmod(0o600)
        try:
            self.assertEqual(doctor(self.app)['status'],'FAIL')
            self.assertTrue(foreign.exists())
            self.assertTrue(any('manually' in issue for issue in self.app.snapshot().issues))
        finally:foreign.unlink()
