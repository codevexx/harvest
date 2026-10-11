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
from .runner import NO_WINDOW, _node_exe, _python_exe, run_job

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


def detect_runtimes() -> list[str]:
    """Which task kinds this machine can run: python, node, browser."""
    found = []
    try:
        subprocess.run([_python_exe(), "--version"], capture_output=True, timeout=10,
                       check=True, creationflags=NO_WINDOW)
        found.append("python")
        # Website bots need Playwright in that same Python.
        r = subprocess.run([_python_exe(), "-c", "import playwright"], capture_output=True,
                           timeout=20, creationflags=NO_WINDOW)
        if r.returncode == 0:
            found.append("browser")
    except Exception:
        pass
    if _node_exe():
        found.append("node")
    return found


def machine_id() -> str:
    """A stable, anonymous id for this PC (a hash, never the raw machine GUID), so
    the dashboard keeps one entry per PC across restarts and app modes."""
    import hashlib
    raw = None
    try:
        if platform.system() == "Windows":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                raw = winreg.QueryValueEx(k, "MachineGuid")[0]
        else:
            for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if Path(f).exists():
                    raw = Path(f).read_text().strip()
                    break
    except Exception:
        pass
    if not raw:
        import uuid
        raw = f"{uuid.getnode()}-{platform.node()}"
    return hashlib.sha256(("harvest:" + raw).encode()).hexdigest()[:32]


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
        "runtimes": detect_runtimes(),
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
                            json={"name": name, "specs": specs, "machine_id": machine_id()}, timeout=15)
        else:
            r = self.s.post(f"{self.base}/api/workers/register",
                            json={"name": name, "specs": specs}, timeout=15)
        r.raise_for_status()
        data = r.json()
        self.worker_id = data["worker_id"]
        self.farm = data.get("farm")
        return self.worker_id

    def heartbeat(self, mode, status) -> None:
        if self.cloud:
            self.s.post(f"{self.base}/api/agent/heartbeat",
                        json={"worker_id": self.worker_id, "mode": mode, "status": status}, timeout=15)
        else:
            self.s.post(f"{self.base}/api/workers/{self.worker_id}/heartbeat",
                        json={"mode": mode, "status": status}, timeout=15)

    def next_job(self, has_gpu, runtimes=("python",)) -> dict | None:
        g = str(has_gpu).lower()
        if self.cloud:
            r = self.s.get(f"{self.base}/api/agent/next",
                           params={"worker_id": self.worker_id, "has_gpu": g,
                                   "runtimes": ",".join(runtimes)}, timeout=15)
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

    def job_status(self, job_id) -> str | None:
        """Current status on the server (cloud only), e.g. 'cancelled'."""
        if not self.cloud:
            return None
        try:
            r = self.s.get(f"{self.base}/api/agent/job", params={"job_id": job_id}, timeout=10)
            return r.json().get("status")
        except (requests.RequestException, ValueError):
            return None

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

def run_agent(cfg: dict, stop_event=None, on_status=None, on_info=None) -> None:
    """Connect to Harvest and process work until stop_event is set.

    Shared by the CLI (main) and the desktop tray app. on_status(mode, state,
    detail) is called on changes so a UI can show idle/running/paused/offline.
    on_info({"farm", "runtimes"}) is called once connected.
    """
    def status(mode, state, detail=None):
        if on_status:
            try:
                on_status(mode, state, detail)
            except Exception:
                pass

    def stopped():
        return bool(stop_event and stop_event.is_set())

    def wait(secs):
        stop_event.wait(secs) if stop_event else time.sleep(secs)

    specs = detect_specs()
    has_gpu = specs["has_gpu"]
    runtimes = specs["runtimes"] or ["python"]
    client = Client(cfg["coordinator"], cfg.get("token"))

    # Register, retrying until connected (or stopped).
    while not stopped():
        try:
            client.register(cfg["name"], specs)
            break
        except requests.RequestException:
            status("offline", "idle", "connecting")
            wait(5)
    if stopped():
        return
    if on_info:
        on_info({"farm": getattr(client, "farm", None), "runtimes": runtimes})
    status("full", "idle", None)

    while not stopped():
        try:
            mode = idle.get_mode(cfg)
            if not _run_available_job(client, has_gpu, runtimes, cfg, mode, stop_event, status):
                client.heartbeat(mode, "idle")
                status(mode, "idle", None)
                wait(cfg.get("poll_interval", 3))
        except requests.RequestException:
            status("offline", "idle", "reconnecting")
            wait(5)
        except KeyboardInterrupt:
            break
        except Exception as e:  # never let one bad response kill the agent
            status("offline", "idle", f"error: {e}")
            wait(5)


def main() -> None:
    cfg = load_config()
    print(f"worker '{cfg['name']}' -> {cfg['coordinator'].rstrip('/')} "
          f"({'cloud' if cfg.get('token') else 'local'})")

    def on_status(mode, state, detail):
        print(f"  [{mode}/{state}]" + (f" {detail}" if detail else ""))

    try:
        run_agent(cfg, on_status=on_status)
    except KeyboardInterrupt:
        print("\nstopping worker")


def _run_available_job(client: Client, has_gpu, runtimes, cfg, mode, stop_event=None, status=None) -> bool:
    if mode in ("paused", "held"):
        return False

    client.heartbeat(mode, "idle")
    job = client.next_job(has_gpu, runtimes)
    if not job:
        return False

    job_id = job["id"]
    if status:
        status(mode, "busy", job.get("name"))
    client.heartbeat(mode, "busy")

    cancelled = False
    last_check = time.monotonic()

    def should_stop():
        nonlocal cancelled, last_check
        if stop_event and stop_event.is_set():
            return True
        if time.monotonic() - last_check > 10:  # was it stopped from the dashboard?
            last_check = time.monotonic()
            cancelled = client.job_status(job_id) == "cancelled"
        return cancelled or idle.get_mode(cfg) in ("paused", "held")

    result, error = run_job(
        job,
        on_episode=lambda i, r, s, x: client.episode(job_id, i, r, s, x),
        on_log=lambda line: client.log(job_id, line),
        should_stop=should_stop, mode=mode,
        resume_from=job.get("episodes_done", 0),
    )

    if cancelled:
        client.finish(job_id, "cancelled", None)  # frees this machine; status stays cancelled
    elif result == "interrupted":
        client.requeue(job_id)
    else:
        client.finish(job_id, result, error)
    return True


if __name__ == "__main__":
    main()
