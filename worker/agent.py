"""Harvest worker agent. Run this on each PC that should do work.

Cloud mode (connect to harvest.codevexx.com with your farm token):
    python -m worker.agent --coordinator https://harvest.codevexx.com --token hv_xxx --name xeon-pc

Local mode (connect to a coordinator you run yourself on your LAN):
    python -m worker.agent --coordinator http://192.168.1.50:8000 --name xeon-pc

It registers, reports whether the machine is idle/busy/gaming, claims jobs when
the machine is free, runs them, streams results back, and pauses jobs the moment
you start a game. Config can also live in worker/worker.config.json.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import time
from pathlib import Path

import requests

try:
    import psutil
except ImportError:
    psutil = None

from . import idle
from .runner import run_job

CONFIG_PATH = Path(__file__).parent / "worker.config.json"


# --- describe this machine ----------------------------------------------

def detect_gpu() -> tuple[str | None, bool]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        if out:
            return out.splitlines()[0].strip(), True
    except Exception:
        pass
    return None, False


def detect_specs() -> dict:
    gpu, is_nvidia = detect_gpu()
    ram_gb = cores = None
    if psutil is not None:
        ram_gb = round(psutil.virtual_memory().total / 1e9)
        cores = psutil.cpu_count(logical=True)
    return {
        "cpu": platform.processor() or platform.machine(),
        "cores": cores, "ram_gb": ram_gb, "gpu": gpu,
        "has_gpu": is_nvidia, "os": f"{platform.system()} {platform.release()}",
    }


# --- coordinator client (cloud or local) --------------------------------

class Client:
    """Talks to either the hosted control plane (token + /api/agent/*) or a
    self-hosted local coordinator (/api/workers/* and /api/jobs/*)."""

    def __init__(self, base: str, token: str | None):
        self.base = base.rstrip("/")
        self.token = token
        self.cloud = bool(token)
        self.s = requests.Session()
        if token:
            self.s.headers["authorization"] = f"Bearer {token}"
        self.worker_id: str | None = None

    def register(self, name, specs) -> str:
        if self.cloud:
            r = self.s.post(f"{self.base}/api/agent/register",
                            json={"name": name, "specs": specs}, timeout=15)
        else:
            r = self.s.post(f"{self.base}/api/workers/register",
                            json={"name": name, "specs": specs}, timeout=15)
        r.raise_for_status()
        self.worker_id = r.json()["worker_id"]
        return self.worker_id

    def heartbeat(self, mode, status) -> None:
        if self.cloud:
            self.s.post(f"{self.base}/api/agent/heartbeat",
                        json={"worker_id": self.worker_id, "mode": mode, "status": status}, timeout=15)
        else:
            self.s.post(f"{self.base}/api/workers/{self.worker_id}/heartbeat",
                        json={"mode": mode, "status": status}, timeout=15)

    def next_job(self, has_gpu) -> dict | None:
        g = str(has_gpu).lower()
        if self.cloud:
            r = self.s.get(f"{self.base}/api/agent/next",
                           params={"worker_id": self.worker_id, "has_gpu": g}, timeout=15)
        else:
            r = self.s.get(f"{self.base}/api/workers/{self.worker_id}/next",
                           params={"has_gpu": g}, timeout=15)
        return r.json().get("job")

    def episode(self, job_id, index, reward, steps, extra):
        if self.cloud:
            self.s.post(f"{self.base}/api/agent/episode",
                        json={"job_id": job_id, "index": index, "reward": reward,
                              "steps": steps, "extra": extra}, timeout=15)
        else:
            self.s.post(f"{self.base}/api/jobs/{job_id}/episode",
                        json={"index": index, "reward": reward, "steps": steps, "extra": extra}, timeout=15)

    def log(self, job_id, line):
        if self.cloud:
            self.s.post(f"{self.base}/api/agent/log", json={"job_id": job_id, "line": line}, timeout=15)
        else:
            self.s.post(f"{self.base}/api/jobs/{job_id}/log", json={"line": line}, timeout=15)

    def finish(self, job_id, status, error):
        if self.cloud:
            self.s.post(f"{self.base}/api/agent/finish",
                        json={"job_id": job_id, "status": status, "error": error}, timeout=15)
        else:
            self.s.post(f"{self.base}/api/jobs/{job_id}/finish",
                        json={"status": status, "error": error}, timeout=15)

    def requeue(self, job_id):
        if self.cloud:
            self.s.post(f"{self.base}/api/agent/requeue", json={"job_id": job_id}, timeout=15)
        else:
            self.s.post(f"{self.base}/api/jobs/{job_id}/requeue", timeout=15)


# --- config --------------------------------------------------------------

def load_config() -> dict:
    cfg = {
        "coordinator": "http://localhost:8000",
        "token": None,
        "name": platform.node(),
        "poll_interval": 3,
        "idle_threshold": 300,
        "pause_on_fullscreen": True,
        "games": [],
    }
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    ap = argparse.ArgumentParser()
    ap.add_argument("--coordinator")
    ap.add_argument("--token")
    ap.add_argument("--name")
    ap.add_argument("--idle-threshold", type=int)
    args = ap.parse_args()
    if args.coordinator:
        cfg["coordinator"] = args.coordinator
    if args.token:
        cfg["token"] = args.token
    if args.name:
        cfg["name"] = args.name
    if args.idle_threshold is not None:
        cfg["idle_threshold"] = args.idle_threshold
    return cfg


# --- main loop -----------------------------------------------------------

def main() -> None:
    cfg = load_config()
    specs = detect_specs()
    has_gpu = specs["has_gpu"]
    client = Client(cfg["coordinator"], cfg.get("token"))

    print(f"worker '{cfg['name']}' -> {client.base} ({'cloud' if client.cloud else 'local'})")
    print(f"  {specs['cpu']} · {specs['ram_gb']} GB · GPU: {specs['gpu'] or 'none'}")

    client.register(cfg["name"], specs)
    print(f"  registered as {client.worker_id}")

    while True:
        try:
            mode = idle.get_mode(cfg)
            if not _run_available_job(client, has_gpu, cfg, mode):
                client.heartbeat(mode, "idle")
                time.sleep(cfg["poll_interval"])
        except requests.RequestException as e:
            print(f"  coordinator unreachable: {e}; retrying")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nstopping worker")
            return


def _run_available_job(client: Client, has_gpu, cfg, mode) -> bool:
    if mode == "paused":
        return False

    client.heartbeat(mode, "idle")
    job = client.next_job(has_gpu)
    if not job:
        return False

    job_id = job["id"]
    print(f"  running job {job_id}: {job['name']}")
    client.heartbeat(mode, "busy")

    def should_stop():
        return idle.get_mode(cfg) == "paused"

    status, error = run_job(
        job,
        on_episode=lambda i, r, s, x: client.episode(job_id, i, r, s, x),
        on_log=lambda line: client.log(job_id, line),
        should_stop=should_stop, mode=mode,
        resume_from=job.get("episodes_done", 0),
    )

    if status == "interrupted":
        client.requeue(job_id)
        print(f"  job {job_id} paused; re-queued")
    else:
        client.finish(job_id, status, error)
        print(f"  job {job_id} {status}")
    return True


if __name__ == "__main__":
    main()
