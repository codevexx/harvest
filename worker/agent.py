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
import gzip
import json
import platform
import subprocess
import threading
import time
from pathlib import Path

import requests

try:
    import psutil
except ImportError:
    psutil = None

from . import idle
from .runner import NO_WINDOW, _node_exe, _python_exe, cleanup, run_job, task_dir

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

IDLE_POLL = 15      # seconds between check-ins while idle
SLOW_POLL = 30      # ... after SLOW_AFTER seconds with nothing to do
SLOW_AFTER = 600
FLUSH_EVERY = 10    # seconds between progress updates while a task runs


class OutputsDisabled(Exception):
    """The server has no file storage, so outputs cannot be uploaded."""


class ReRegister(Exception):
    """The server no longer knows this worker (it was removed): register again."""

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
            r = self.s.post(f"{self.base}/api/agent/heartbeat",
                            json={"worker_id": self.worker_id, "mode": mode, "status": status}, timeout=15)
            if r.status_code == 404:
                raise ReRegister()
        else:
            self.s.post(f"{self.base}/api/workers/{self.worker_id}/heartbeat",
                        json={"mode": mode, "status": status}, timeout=15)

    def next_job(self, has_gpu, runtimes=("python",), mode="full") -> dict | None:
        """Check in (mode doubles as the heartbeat) and claim a task if one is waiting."""
        g = str(has_gpu).lower()
        if self.cloud:
            r = self.s.get(f"{self.base}/api/agent/next",
                           params={"worker_id": self.worker_id, "has_gpu": g, "mode": mode,
                                   "runtimes": ",".join(runtimes)}, timeout=15)
            if r.status_code == 404:
                raise ReRegister()
        else:
            r = self.s.get(f"{self.base}/api/workers/{self.worker_id}/next",
                           params={"has_gpu": g}, timeout=15)
        return r.json().get("job")

    def progress(self, job_id, mode, episodes, logs, episodes_done) -> str | None:
        """Send a batch of chart points and output lines. Returns the task's status
        on the server (e.g. 'cancelled'), or None if it could not be sent."""
        if not self.cloud:  # the old LAN coordinator takes them one by one
            for e in episodes:
                self.episode(job_id, e["index"], e["reward"], e["steps"], e["extra"])
            for line in logs:
                self.log(job_id, line)
            return None
        r = self.s.post(f"{self.base}/api/agent/progress", timeout=20, json={
            "job_id": job_id, "worker_id": self.worker_id, "mode": mode,
            "episodes": episodes, "logs": logs, "episodes_done": episodes_done})
        return r.json().get("status")

    def files(self, job_id) -> dict:
        """A large task's files, stored on the server as one gzipped JSON bundle."""
        r = self.s.get(f"{self.base}/api/agent/files", params={"job_id": job_id}, timeout=300)
        r.raise_for_status()
        return json.loads(gzip.decompress(r.content))

    def upload_output(self, job_id, path: Path, size: int, manifest: list) -> None:
        """Upload outputs.zip in pieces (each under the server's request limit)."""
        r = self.s.post(f"{self.base}/api/agent/output/start", json={"job_id": job_id, "size": size}, timeout=30)
        if r.status_code == 501:
            raise OutputsDisabled()
        r.raise_for_status()
        up = r.json()
        parts, n = [], 1
        with open(path, "rb") as f:
            while chunk := f.read(up["part_size"]):
                pr = self.s.put(f"{self.base}/api/agent/output/part", data=chunk, timeout=900,
                                params={"job_id": job_id, "upload_id": up["upload_id"], "n": n})
                pr.raise_for_status()
                parts.append(pr.json())
                n += 1
        self.s.post(f"{self.base}/api/agent/output/complete", timeout=60, json={
            "job_id": job_id, "upload_id": up["upload_id"], "parts": parts, "files": manifest,
        }).raise_for_status()

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


# --- batched progress for a running task --------------------------------

class Reporter:
    """Collects a running task's episodes and output lines and sends them in one
    request every FLUSH_EVERY seconds, instead of one request per line.

    Long runs keep about 2,000 chart points at most: once 1,000 are stored only
    every 2nd episode is kept, then every 4th, and so on. The latest episode is
    always sent, and the true episode count is reported separately."""

    MAX_LOGS = 60  # per update; extra lines are summarised

    def __init__(self, client: "Client", job_id: str, mode_fn):
        self.client, self.job_id, self.mode_fn = client, job_id, mode_fn
        self.lock = threading.Lock()
        self.episodes: list[dict] = []
        self.logs: list[str] = []
        self.skipped_logs = 0
        self.latest: dict | None = None
        self.done = 0
        self.kept = 0
        self.stride = 1
        self.status: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def episode(self, i, reward, steps, extra):
        e = {"index": i, "reward": reward, "steps": steps, "extra": extra or {}}
        with self.lock:
            self.done = max(self.done, i + 1)
            self.latest = e
            if i % self.stride == 0:
                self.episodes.append(e)
                self.kept += 1
                if self.kept >= 2000 - 1000 / self.stride:
                    self.stride *= 2

    def log(self, line):
        with self.lock:
            self.logs.append(str(line)[:2000])
            if len(self.logs) > self.MAX_LOGS:
                self.logs.pop(0)
                self.skipped_logs += 1

    def flush(self):
        with self.lock:
            eps, logs = self.episodes, self.logs
            if self.latest and (not eps or eps[-1]["index"] != self.latest["index"]):
                eps = eps + [self.latest]
            if self.skipped_logs:
                logs = [f"({self.skipped_logs} more lines of output not shown)"] + logs
            self.episodes, self.logs, self.skipped_logs, done = [], [], 0, self.done
        send, later = eps[:300], eps[300:]  # the server takes 300 per update
        try:
            status = self.client.progress(self.job_id, self.mode_fn(), send, logs, done)
            if status:
                self.status = status
        except (requests.RequestException, ValueError):
            later, logs_back = eps, logs  # keep them for the next try
            with self.lock:
                self.logs = (logs_back + self.logs)[-self.MAX_LOGS:]
        if later:
            with self.lock:
                self.episodes = (later + self.episodes)[-2000:]

    def _loop(self):
        if self._stop.wait(2):  # first update soon, so the dashboard shows it started
            return
        self.flush()
        while not self._stop.wait(FLUSH_EVERY):
            self.flush()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.flush()


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

    def connect():
        # Register, retrying until connected (or stopped).
        while not stopped():
            try:
                client.register(cfg["name"], specs)
                if on_info:
                    on_info({"farm": getattr(client, "farm", None), "runtimes": runtimes})
                return True
            except requests.RequestException:
                status("offline", "idle", "connecting")
                wait(5)
        return False

    if not connect():
        return
    status("full", "idle", None)

    # Check the machine's mode every few seconds locally, but only talk to the
    # server every IDLE_POLL seconds (SLOW_POLL when it has been quiet), or right
    # away when the mode changes, e.g. a game starts.
    last_poll, last_mode, quiet_since = 0.0, None, time.monotonic()
    while not stopped():
        try:
            mode = idle.get_mode(cfg)
            now = time.monotonic()
            interval = SLOW_POLL if now - quiet_since > SLOW_AFTER else IDLE_POLL
            if now - last_poll >= interval or mode != last_mode:
                last_poll, last_mode = now, mode
                job = None
                if mode in ("paused", "held"):
                    client.heartbeat(mode, "idle")
                else:
                    job = client.next_job(has_gpu, runtimes, mode)
                if job:
                    _run_job(client, job, cfg, mode, stop_event, status)
                    quiet_since, last_poll = time.monotonic(), 0.0  # look for more work right away
                    continue
                status(mode, "idle", None)
            wait(3)
        except ReRegister:
            if not connect():
                return
        except requests.RequestException:
            status("offline", "idle", "reconnecting")
            last_poll = 0.0
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


def _run_job(client: Client, job: dict, cfg, mode, stop_event=None, status=None) -> None:
    job_id = job["id"]
    if status:
        status(mode, "busy", job.get("name"))
    reporter = Reporter(client, job_id, lambda: idle.get_mode(cfg))
    reporter.start()

    if job["spec"].get("files_key"):  # too big to send inline: fetch the bundle
        try:
            reporter.log("downloading the task's files")
            job["spec"]["files"] = client.files(job_id)
        except Exception as e:
            reporter.log(f"could not download the task's files: {e}")
            reporter.stop()
            client.finish(job_id, "failed", "could not download the task's files")
            return

    def should_stop():
        if stop_event and stop_event.is_set():
            return True
        # reporter.status comes back with each progress update, so stopping a
        # task from the dashboard needs no extra requests.
        return reporter.status == "cancelled" or idle.get_mode(cfg) in ("paused", "held")

    result, error = run_job(
        job,
        on_episode=reporter.episode,
        on_log=reporter.log,
        should_stop=should_stop, mode=mode,
        resume_from=job.get("episodes_done", 0),
    )
    finished = reporter.status == "cancelled" or result != "interrupted"
    if finished and client.cloud:
        _upload_outputs(client, job_id, reporter.log)
    reporter.stop()

    if reporter.status == "cancelled":
        client.finish(job_id, "cancelled", None)  # frees this machine; status stays cancelled
    elif result == "interrupted":
        client.requeue(job_id)  # keep the folder: it resumes from its checkpoint
    else:
        client.finish(job_id, result, error)
    if finished:
        cleanup(job_id)


def _upload_outputs(client: Client, job_id: str, log) -> None:
    """Zip the task's outputs/ folder and upload it, if it has anything in it."""
    import zipfile
    out = task_dir(job_id) / "outputs"
    files = [p for p in out.rglob("*") if p.is_file()] if out.is_dir() else []
    if not files:
        return
    zpath = task_dir(job_id).parent / f"{job_id}-outputs.zip"
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, p.relative_to(out))
        size = zpath.stat().st_size
        if size > 2 * 1024 ** 3:
            log(f"outputs are {size / 1e9:.1f} GB, over the 2 GB limit; not uploaded")
            return
        manifest = [{"path": p.relative_to(out).as_posix(), "size": p.stat().st_size} for p in files[:500]]
        log(f"uploading outputs ({len(files)} files, {size / 1e6:.1f} MB)")
        client.upload_output(job_id, zpath, size, manifest)
        log("outputs uploaded: download them from the dashboard")
    except OutputsDisabled:
        log("outputs were not uploaded: file storage is not enabled on the server yet")
    except Exception as e:
        log(f"uploading outputs failed: {e}")
    finally:
        zpath.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
