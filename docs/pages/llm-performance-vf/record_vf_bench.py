#!/usr/bin/env python3
"""Read-only PB telemetry recorder around a CUDA llama-bench run."""
import csv, datetime, json, pathlib, shutil, socket, subprocess, threading, time
ROOT = pathlib.Path.home()/'.local/share/llama.cpp'
def request(method):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect('/run/penguin-burnerd.sock')
        s.sendall((json.dumps({'method':method} if method == 'status' else {'method':method, 'gpu_index':0})+'\n').encode())
        with s.makefile('r') as f: reply=json.loads(f.readline())
    if not reply.get('ok'): raise RuntimeError(reply)
    return reply['result']
def save(path, data): path.write_text(json.dumps(data,indent=2)+'\n')
def capture(out, suffix):
    save(out/f'status-{suffix}.json',request('status'))
    save(out/f'curve-{suffix}.json',request('gpu_vf_snapshot'))
    (out/f'nvidia-smi-{suffix}.txt').write_text(subprocess.check_output(['nvidia-smi','-q'],text=True))
def main():
    out=ROOT/'results'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-perf-vf')
    out.mkdir(parents=True)
    capture(out,'before')
    status=json.loads((out/'status-before.json').read_text())
    profile_id=status.get('active_job',{}).get('profile_id')
    if not profile_id: raise RuntimeError('No active profile; refusing to mislabel this run.')
    profile=pathlib.Path.home()/'.config/PenguinBurner/auto-uv-profiles'/f'auto-uv-profile-{profile_id}.json'
    shutil.copy2(profile,out/'profile.json')
    if json.loads((out/'profile.json').read_text()).get('generated_profile_tier') != 'performance':
        raise RuntimeError('Active profile is not Performance')
    shutil.copy2(ROOT/'installation.json',out/'installation.json')
    shutil.copy2(__file__,out/'record_vf_bench.py')
    cmd=[str(pathlib.Path.home()/'.local/opt/llama.cpp-v0.5.0/build/bin/llama-bench'),'-m',str(ROOT/'models/Qwen3-8B-Q4_K_M.gguf'),'-ngl','99','-dev','CUDA0','-fa','on','-p','2048','-n','256','-d','0,4096','-r','5','-o','json','--progress']
    start=time.monotonic(); stop=threading.Event(); errors=[]
    save(out/'run.json',{'command':cmd,'start_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sample_interval_s':0.1,'profile_id':profile_id})
    def sampler():
        with (out/'telemetry.jsonl').open('w') as raw, (out/'telemetry.csv').open('w') as cf:
            writer=csv.DictWriter(cf,fieldnames=['elapsed_s','unix_ns','request_ms','graphics_mhz','memory_mhz','voltage_mv','power_w','temperature_c','utilization_pct','throttle_mask'])
            writer.writeheader(); deadline=time.monotonic()
            while not stop.is_set():
                before=time.monotonic()
                try:
                    t=request('gpu_telemetry'); after=time.monotonic()
                    t['elapsed_s']=(before+after)/2-start; t['request_ms']=(after-before)*1000
                    raw.write(json.dumps(t)+'\n'); raw.flush()
                    writer.writerow(dict(elapsed_s=t['elapsed_s'],unix_ns=t['updated_unix_ns'],request_ms=t['request_ms'],graphics_mhz=t['clocks_mhz']['graphics'],memory_mhz=t['clocks_mhz']['memory'],voltage_mv=t['voltage_mv'],power_w=t['power_draw_w'],temperature_c=t['temperature_c'],utilization_pct=t['gpu_utilization_pct'],throttle_mask=t['throttle_reason_mask'])); cf.flush()
                except Exception as e: errors.append({'elapsed_s':time.monotonic()-start,'error':str(e)})
                deadline+=0.1
                stop.wait(max(0,deadline-time.monotonic()))
    thread=threading.Thread(target=sampler); thread.start()
    print(out,flush=True)
    try:
        time.sleep(2)
        with (out/'results.json').open('w') as result, (out/'benchmark.log').open('w') as log, (out/'events.jsonl').open('w') as events:
            proc=subprocess.Popen(cmd,stdout=result,stderr=subprocess.PIPE,text=True,bufsize=1)
            for line in proc.stderr:
                elapsed=time.monotonic()-start
                log.write(line); log.flush()
                events.write(json.dumps({'elapsed_s':elapsed,'line':line.rstrip()})+'\n'); events.flush()
                if 'llama-bench: benchmark' in line: print(f'{elapsed:.2f}s {line}',end='',flush=True)
            code=proc.wait()
        time.sleep(2)
    finally:
        stop.set(); thread.join()
        save(out/'telemetry-errors.json',errors)
        capture(out,'after')
    save(out/'completion.json',{'exit_code':code,'elapsed_s':time.monotonic()-start,'telemetry_errors':len(errors)})
    print(f'Completed exit={code}: {out}',flush=True)
    raise SystemExit(code)
if __name__=='__main__': main()
