"""Contabo-owned isolated replay controller, without application restart."""
import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import time
from pathlib import Path

ROOT=Path(os.environ["EPISODE_PROBE_ROOT"])
NAME="cw-episode-pool-preflight-20261107"
IMAGE=os.environ["EPISODE_PROBE_IMAGE"]
D=["docker","--host","unix:///var/run/docker.sock"]
G=1024**3
MEMORY=4*G
RESERVE=1536*1024**2
DISK=6*G
MAXWORK=G

def docker(*args,timeout=20):
    return subprocess.check_output([*D,*args],text=True,timeout=timeout).strip()

def cpus():
    return list(map(int,Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))

def sample(seconds):
    a=cpus()
    time.sleep(seconds)
    values=[y-x for x,y in zip(a,cpus())]
    mem=int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:")))*1024
    build=False
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc/"comm").read_text().strip() in ("docker-buildx","buildctl"):
                build=True
        except OSError:
            pass
    return dict(idle=100*values[3]/sum(values),iowait=100*values[4]/sum(values),
                available=mem,free=shutil.disk_usage(ROOT).free,build=build)

def cgroup(pid):
    relative=next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if line.startswith("0::"))
    return Path("/sys/fs/cgroup")/relative.lstrip("/")

def read_group(p):
    fields=dict(line.split() for line in (p/"cpu.stat").read_text().splitlines())
    return dict(cpu=int(fields["usage_usec"])/1e6,throttled=int(fields.get("throttled_usec",0))/1e6,
                memory_peak=int((p/"memory.peak").read_text()),io=(p/"io.stat").read_text())

def on_signal(*_):
    raise TimeoutError("controller signal")

for sig in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT,signal.SIGHUP):
    signal.signal(sig,on_signal)
assert ROOT.is_dir() and not ROOT.is_symlink()
proof={"state":"WAITING_ADMISSION","admission":[],"market_runs":0,"cpu_cap":2,"cpu_seconds_limit":600,"window_selection_idle":None,"stop_idle":None,"host_policy":"dedicated_runner"}
ident=None
work=ROOT/"work"
owned=False
own_cpu=time.process_time()
try:
    if docker("ps","-a","--filter","name=^/"+NAME+"$","--format","{{.ID}}"):
        raise RuntimeError("existing test container")
    work.mkdir(mode=0o700)
    owned=True
    os.chown(work,10001,10001)
    for _ in range(1):
        s=sample(5)
        proof["admission"].append(s)
        if s["available"]>=MEMORY and s["free"]>=DISK+MAXWORK:
            break
    else:
        proof["state"]="NO_RUN_RESOURCE_ADMISSION"
        raise RuntimeError("resource admission")
    signal.alarm(600)
    ident=docker(
        "create","--name",NAME,"--label","io.cryptowatcher.test=episode-pool-preflight",
        "--network=none","--no-healthcheck","--cpus=2","--memory=4g","--memory-swap=4g",
        "--pids-limit=64","--ulimit","cpu=600:600","--read-only","--cap-drop=ALL",
        "--security-opt=no-new-privileges:true","--user=10001:10001",
        "--mount",f"type=bind,src={ROOT}/case,dst=/case,readonly",
        "--mount",f"type=bind,src={work},dst=/work",
        "--entrypoint","/bin/sh",IMAGE,"-ec",
        "cp -r /case/cryptowatcher /work/; ln -s /app/cryptowatcher/_native /work/cryptowatcher/_native; "
        "cp /case/replay.py /case/burst_timing.py /work/; cd /work; exec python -B replay.py")
    docker("start",ident)
    facts=json.loads(docker("inspect",ident))[0]
    assert facts["Image"]==IMAGE and facts["HostConfig"]["NanoCpus"]==2*10**9 and facts["HostConfig"]["Memory"]==MEMORY
    assert facts["HostConfig"]["NetworkMode"]=="none"
    assert {x["Destination"] for x in facts["Mounts"]}=={"/case","/work"}
    proof.update(state="RUNNING",market_runs=1,image=IMAGE)
    group=cgroup(facts["State"]["Pid"])
    began=time.monotonic()
    samples=[]
    while True:
        host=sample(3)
        samples.append(host)
        usage=read_group(group)
        size=sum(p.stat().st_size for p in work.glob("case.sqlite3*") if p.is_file())
        elapsed=time.monotonic()-began
        control=time.process_time()-own_cpu+resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime+resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime
        proof.update(elapsed=elapsed,cgroup=usage,controller_cpu=control,max_work=size,
                     min_host_idle=min(x["idle"] for x in samples),min_host_available=min(x["available"] for x in samples))
        reason=None
        if host["available"]<RESERVE: reason="STOP_HOST_MEMORY_RESERVE"
        elif host["free"]<DISK or size>MAXWORK: reason="STOP_DISK_RESERVE"
        elif usage["cpu"]+control>=600: reason="STOP_CPU_BUDGET"
        elif elapsed>=510: reason="STOP_WALL_BUDGET"
        if (work/"progress.json").is_file():
            progress=json.loads((work/"progress.json").read_text())
            old=proof.get("last_progress")
            if progress!=old:
                proof["last_progress"]=progress
                milestone={key:value for key,value in progress.items() if key not in ("ingestion_work","periodic_elapsed_by_job")}
                print("REPLAY_PROGRESS "+json.dumps(milestone,separators=(",",":")),flush=True)
        if reason:
            proof.update(state="INTERRUPTED",reason=reason)
            break
        if (work/"result.json").exists():
            result=json.loads((work/"result.json").read_text())
            proof.update(state="COMPLETE" if result["state"]=="COMPLETE" else "CASE_ERROR",result=result)
            break
except BaseException as exc:
    if proof["state"]!="NO_RUN_RESOURCE_ADMISSION":
        proof.update(state="CONTROLLER_ERROR",error=type(exc).__name__)
finally:
    signal.alarm(0)
    if ident:
        try:
            if proof["state"] in ("CASE_ERROR","CONTROLLER_ERROR"):
                proof["trace"]=docker("logs","--tail","12",ident,timeout=8)[-3000:]
        except (OSError,subprocess.SubprocessError):
            pass
        docker("rm","-f",ident)
        proof["container_removed"]=not docker("ps","-a","--filter","name=^/"+NAME+"$","--format","{{.ID}}")
    if owned:
        shutil.rmtree(work)
        proof["work_removed"]=not work.exists()
    print(json.dumps(proof,separators=(",",":")))
