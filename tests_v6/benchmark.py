"""Real render and lifecycle scaling; disposable UID only."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import statistics
import time

from hubi6.services import App
from tests_v6.support import bootstrap, repo
from tests_v6.test_runtime import Client, wait_for


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


def main():
    app=bootstrap();results=[];runtime=[];actions=[];records=[]
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
                    results.append(measure(measured));runtime.append(dict(measure(measured,True),repo_root_entries=target))
                finally:client.close()
                actions.append(operations(measured,root/'repo-0006',target))
            elif target==200:
                results.append(measure(measured))
            else:
                for r in records:app.stop(r.id)
                records=[]
                results.append(measure(measured))
                records=[measured.start('codex',str(root/'repo-0000'),f'bench{i}') for i in range(50)]
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
    print(json.dumps({'snapshots':results,'runtime_snapshots':runtime,'operations':actions},indent=2))


if __name__=='__main__':main()
