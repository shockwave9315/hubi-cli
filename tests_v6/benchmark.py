"""Real render and lifecycle scaling; disposable UID only."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tarfile
import time

from hubi6.services import App
from tests_v6.support import bootstrap, repo
from tests_v6.test_runtime import Client, wait_for
from tests_v6.test_polish import collect


def measure(app, runtime=False, count=9):
    samples=[app.runtime_snapshot() if runtime else app.snapshot() for _ in range(count)]
    assert all(s.subprocesses==2 for s in samples)
    return {'median_ms':statistics.median(s.elapsed_ms for s in samples),
            'p95_ms':max(s.elapsed_ms for s in samples),'subprocesses':2,
            'repos':len(samples[-1].repos),'workloads':len(samples[-1].workloads)}


def operations(app, fixture, repo_count):
    config=Path.home()/'benchmark.toml'
    config.write_text('repos_root = '+json.dumps(str(app.c.repos))+'\n')
    times={'inspect':[],'attach':[],'stop':[]};counts={'inspect':[],'stop':[],'attach':[]}
    for i in range(5):
        r=app.start('codex',str(fixture),'stopbench')
        calls=app.r.calls;start=time.monotonic();app.find(r.id)
        times['inspect'].append((time.monotonic()-start)*1000);counts['inspect'].append(app.r.calls-calls)
        count_file=Path.home()/'benchmark-client-count.json'
        script = """from hubi6.adapters.process import Runner
from hubi6.cli import main
from pathlib import Path
import sys
calls=[0]
original=Runner.run
def counted(self,*args,**kwargs):
 calls[0]+=1
 return original(self,*args,**kwargs)
Runner.run=counted
try:code=main(sys.argv[1:])
finally:Path(sys.argv[2]).with_name('benchmark-client-count.json').write_text(str(calls[0]))
raise SystemExit(code)
"""
        start=time.monotonic();client=Client([],command=['python3','-c',script,'--config',str(config),'attach',r.id,'--mode','shared'])
        try:
            wait_for(lambda:app.find(r.id).session.attached==1)
            times['attach'].append((time.monotonic()-start)*1000)
            client.send(b'\x02d');client.p.wait(3);counts['attach'].append(int(count_file.read_text()))
        finally:client.close()
        calls=app.r.calls;start=time.monotonic();app.stop(r.id)
        times['stop'].append((time.monotonic()-start)*1000);counts['stop'].append(app.r.calls-calls)
    return {'repos':repo_count,'workloads':len(app.runtime_snapshot().workloads),'median_ms':{k:statistics.median(v) for k,v in times.items()},
            'subprocesses':{k:sorted(set(v)) for k,v in counts.items()}}


def latency(samples):
    ordered=sorted(samples)
    return {'p50_ms':statistics.median(samples),'p90_ms':ordered[int((len(ordered)-1)*.9)],'samples_ms':samples}


def ui_measure(app, baseline=None):
    """Real PTY Enter -> visible prompt; no polling interval is subtracted."""
    config=Path.home()/'benchmark-ui.toml'
    config.write_text('repos_root = '+json.dumps(str(app.c.repos))+'\n')
    command=[sys.executable,'-m','hubi6','--config',str(config)]
    if baseline:
        script='import sys;sys.path.insert(0,'+repr(str(baseline))+');from hubi6.cli import main;raise SystemExit(main(sys.argv[1:]))'
        command=[sys.executable,'-c',script,'--config',str(config)]
    client=Client([],command=command)
    screen=[];confirmation=[]
    def action(data,needle=b'Choice + Enter:'):
        # A separate manual action, after the previous burst's quiet window.
        time.sleep(.1);client.output=b''
        start=time.perf_counter();client.send(data);client.read_until(needle)
        return (time.perf_counter()-start)*1000
    try:
        client.read_until(b'Choice + Enter:')
        for i in range(20):
            screen.append(action(b'w\n' if i%2==0 else b'b\n'))
        action(b'w\n');action(b'1\n')
        for _ in range(12):
            confirmation.append(action(b'd\n',b'Type yes to stop this generation:'))
            action(b'no\n')
        action(b'b\n');action(b'b\n')
        time.sleep(.1);client.output=b'';client.send(b'w\n1\nd\nyes\n')
        client.read_until(b'Choice + Enter:');collect(client)
        assert b'Type yes' not in client.output and b'Clients:' not in client.output
        assert len(app.runtime_snapshot().workloads)==9
        return {'repos':7,'workloads':9,'enter_next_prompt':latency(screen),
                'stop_confirmation':latency(confirmation),'raw_paste_burst_safe':True}
    finally:
        client.close()


def baseline_measure(root, baseline):
    config=Path.home()/'benchmark-baseline.toml'
    config.write_text('repos_root = '+json.dumps(str(root))+'\n')
    script=('import sys,json,statistics;sys.path.insert(0,'+repr(str(baseline))+');'
            'from hubi6.services import App;from hubi6.config import Config;'
            'a=App(Config.load('+repr(str(config))+'));s=[a.snapshot() for _ in range(9)];'
            'print(json.dumps({"median_ms":statistics.median(x.elapsed_ms for x in s),'
            '"p95_ms":max(x.elapsed_ms for x in s),"subprocesses":s[-1].subprocesses,'
            '"repos":len(s[-1].repos),"workloads":len(s[-1].workloads)}))')
    return json.loads(subprocess.check_output([sys.executable,'-c',script],text=True))


def main():
    app=bootstrap();results=[];runtime=[];actions=[];records=[];ui={};before=[]
    archive=Path(__file__).resolve().parent.parent/'dist/hubi6-polish-baseline.tar'
    baseline=None
    if archive.exists():
        baseline=Path.home()/'benchmark-baseline-source'
        baseline.mkdir(exist_ok=True)
        with tarfile.open(archive) as source:
            source.extractall(baseline,filter='data')
    root=Path.home()/'benchmark-repos';shutil.rmtree(root,ignore_errors=True);root.mkdir()
    fixture=repo('benchmark-template');measured=App(replace(app.c,repos=root))
    try:
        for target in (7,200,1000):
            for i in range(len(list(root.iterdir())),target):
                path=root/f'repo-{i:04}';path.mkdir();shutil.copytree(fixture/'.git',path/'.git')
            if target==7:
                results.append(measure(measured))
                records=[measured.start('codex' if i%2==0 else 'claude',str(root/f'repo-{i:04}')) for i in range(6)]
                client=Client(['attach',records[0].id,'--mode','shared'])
                try:
                    wait_for(lambda:app.find(records[0].id).session.attached==1)
                    if baseline:before.append(baseline_measure(root,baseline))
                    results.append(measure(measured));runtime.append(dict(measure(measured,True),repo_root_entries=target))
                finally:client.close()
                actions.append(operations(measured,root/'repo-0006',target))
                extra=[measured.start('terminal',str(root/f'repo-{i:04}'),'ui') for i in range(3)]
                try:
                    if baseline:ui['before']=ui_measure(measured,baseline)
                    ui['after']=ui_measure(measured)
                finally:
                    for r in extra:measured.stop(r.id)
            elif target==200:
                if baseline:before.append(baseline_measure(root,baseline))
                results.append(measure(measured))
            else:
                for r in records:app.stop(r.id)
                records=[]
                results.append(measure(measured))
                records=[measured.start('codex',str(root/'repo-0000'),f'bench{i}') for i in range(50)]
                if baseline:before.append(baseline_measure(root,baseline))
                results.append(measure(measured));runtime.append(dict(measure(measured,True),repo_root_entries=target))
                # Compare lifecycle at constant workload count, varying ONLY
                # repo discovery size; the earlier 7/6 case is realistic UX.
                small=Path.home()/'lifecycle-small-repos';small.mkdir(exist_ok=True)
                for i in range(7):
                    p=small/f'repo-{i}';p.mkdir(exist_ok=True)
                    if not (p/'.git').exists():shutil.copytree(fixture/'.git',p/'.git')
                actions.append(operations(App(replace(measured.c,repos=small)),small/'repo-0',7))
                actions.append(operations(measured,root/'repo-0006',target))
    finally:
        for r in records:app.stop(r.id)
    print(json.dumps({'baseline_head':'ba22947b86537e9fc42a6edc3e96c0998f8f93e6' if baseline else None,
                      'snapshots_before':before,'snapshots':results,'runtime_snapshots':runtime,'operations':actions,'ui':ui},indent=2))


if __name__=='__main__':main()
