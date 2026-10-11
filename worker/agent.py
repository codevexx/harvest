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
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

try:
    import psutil
except ImportError:
    psutil = None

from . import idle
from . import runtimes as setup_mod
from .runner import (NO_WINDOW, START_MARK, _node_exe, _python_exe, cleanup, created_files, reset_discovery,
                     run_job, task_dir)

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
    if _python_exe():  # None if the PC has no real Python (never the Store placeholder)
        found.append("python")
    if setup_mod.playwright_python(_python_exe):  # user's Python with Playwright, or Harvest's own
        found.append("browser")
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


def _raise_with_message(r) -> None:
    """Like raise_for_status, but with the server's own explanation (e.g. storage full)."""
    if r.status_code < 400:
        return
    try:
        msg = r.json().get("error")
    except ValueError:
        msg = None
    raise RuntimeError(msg or f"server returned {r.status_code}")


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
        self.poll_hint: int | None = None

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
                                   "runtimes": ",".join(runtimes) or "none"}, timeout=15)
            if r.status_code == 404:
                raise ReRegister()
        else:
            r = self.s.get(f"{self.base}/api/workers/{self.worker_id}/next",
                           params={"has_gpu": g}, timeout=15)
        data = r.json()
        self.poll_hint = data.get("poll")  # server: "work is waiting, ask again in N s"
        return data.get("job")

    def progress(self, job_id, mode, episodes, logs, episodes_done, total=0) -> str | None:
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
            "episodes": episodes, "logs": logs, "episodes_done": episodes_done, "total": total})
        return r.json().get("status")

    MAX_BUNDLE = 250 * 1024 * 1024  # never unpack an upload past this (zip-bomb guard)

    def files(self, job_id) -> dict:
        """A large task's files, stored on the server as one gzipped JSON bundle."""
        import zlib
        r = self.s.get(f"{self.base}/api/agent/files", params={"job_id": job_id}, timeout=300)
        _raise_with_message(r)
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip
        data = d.decompress(r.content, self.MAX_BUNDLE + 1)
        if len(data) > self.MAX_BUNDLE or d.unconsumed_tail:
            raise RuntimeError("the task's files unpack to more than 250 MB; refusing")
        files = json.loads(data)
        if not isinstance(files, dict) or len(files) > 10000:
            raise RuntimeError("the task's files are not in the expected format")
        return files

    def upload_output(self, job_id, path: Path, size: int, manifest: list, kind: str = "output") -> None:
        """Upload outputs.zip (or a paused task's checkpoint) in pieces, each
        under the server's request limit."""
        r = self.s.post(f"{self.base}/api/agent/output/start", json={"job_id": job_id, "size": size, "kind": kind}, timeout=30)
        if r.status_code == 501:
            raise OutputsDisabled()
        _raise_with_message(r)
        up = r.json()
        parts, n = [], 1
        with open(path, "rb") as f:
            while chunk := f.read(up["part_size"]):
                pr = self.s.put(f"{self.base}/api/agent/output/part", data=chunk, timeout=900,
                                params={"job_id": job_id, "upload_id": up["upload_id"], "n": n, "kind": kind})
                _raise_with_message(pr)
                parts.append(pr.json())
                n += 1
        _raise_with_message(self.s.post(f"{self.base}/api/agent/output/complete", timeout=60, json={
            "job_id": job_id, "upload_id": up["upload_id"], "parts": parts, "files": manifest, "kind": kind,
        }))

    def download_checkpoint(self, job_id, dest: Path, limit: int = 600 * 1024 * 1024) -> None:
        """Save a paused task's checkpoint zip (made by another machine) to dest."""
        with self.s.get(f"{self.base}/api/agent/checkpoint", params={"job_id": job_id}, timeout=300, stream=True) as r:
            _raise_with_message(r)
            got = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    got += len(chunk)
                    if got > limit:
                        raise RuntimeError("checkpoint is larger than expected")
                    f.write(chunk)

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
        self.total = 0  # from {"progress": {"total": N}}
        self.kept = 0
        self.stride = 1
        self.best: float | None = None
        self.status: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def episode(self, i, reward, steps, extra):
        e = {"index": i, "reward": reward, "steps": steps, "extra": extra or {}}
        with self.lock:
            self.done = max(self.done, i + 1)
            self.latest = e
            best = self.best is None or reward > self.best
            if best:
                self.best = reward
            if i % self.stride == 0 or best:  # new bests always reach the chart
                self.episodes.append(e)
                self.kept += 1
                if self.kept >= 2000 - 1000 / self.stride:
                    self.stride *= 2

    def set_total(self, n):
        self.total = max(0, int(n))

    def log(self, line):
        with self.lock:
            self.logs.append(ANSI.sub("", str(line))[:2000])  # drop terminal colour codes
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
            status = self.client.progress(self.job_id, self.mode_fn(), send, logs, done, self.total)
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
    runtimes = specs["runtimes"]  # may be empty: then this PC takes no tasks
    client = Client(cfg["coordinator"], cfg.get("token"))

    # Missing Python / Node.js / website support? Install it in the background
    # (worker/runtimes.py) and tell the server what this PC can run once done.
    want_browser = cfg.get("setup_browser", True)
    needs_setup = (cfg.get("auto_setup", True) and setup_mod.supported() and
                   (not _python_exe() or not _node_exe() or (want_browser and "browser" not in runtimes)))
    setup_done = threading.Event()

    def info(**kw):
        if on_info:
            try:
                on_info(kw)
            except Exception:
                pass

    def run_setup():
        def log(msg):
            print(f"setup: {msg}")
            info(setup=msg)
        try:
            problems = setup_mod.ensure(_python_exe, _node_exe, want_browser, log)
        except Exception as e:
            problems = [str(e)]
        reset_discovery()
        for p in problems:
            print(f"setup problem: {p}")
        info(setup=None, setup_problems=problems)
        setup_done.set()

    if needs_setup:
        specs["setup"] = "Installing what tasks need (Python, Node.js, website support)"
        threading.Thread(target=run_setup, daemon=True).start()

    def connect():
        # Register, retrying until connected (or stopped).
        while not stopped():
            try:
                client.register(cfg["name"], specs)
                info(farm=getattr(client, "farm", None), runtimes=runtimes)
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
            if setup_done.is_set():  # new runtimes installed: report them
                setup_done.clear()
                specs = detect_specs()
                runtimes = specs["runtimes"]
                if not connect():
                    return
                last_poll = 0.0
            mode = idle.get_mode(cfg)
            now = time.monotonic()
            interval = SLOW_POLL if now - quiet_since > SLOW_AFTER else IDLE_POLL
            if client.poll_hint:  # tasks are waiting: check back sooner
                interval = max(2, min(interval, int(client.poll_hint)))
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

    resume_from = job.get("episodes_done", 0)
    if client.cloud and "same_machine" in job:
        resume_from = _prepare_folder(client, job, resume_from, reporter.log)

    def should_stop():
        if stop_event and stop_event.is_set():
            return True
        # reporter.status comes back with each progress update, so stopping a
        # task from the dashboard needs no extra requests.
        return reporter.status == "cancelled" or idle.get_mode(cfg) in ("paused", "held")

    result, error = run_job(
        job,
        on_episode=reporter.episode,
        on_log=reporter.log, on_total=reporter.set_total,
        should_stop=should_stop, mode=mode,
        resume_from=resume_from,
    )
    finished = reporter.status == "cancelled" or result != "interrupted"
    if finished and client.cloud:
        _upload_outputs(client, job_id, reporter.log)
    elif client.cloud:
        _save_checkpoint(client, job_id, reporter.log)  # so any machine can continue it
    reporter.stop()

    if reporter.status == "cancelled":
        client.finish(job_id, "cancelled", None)  # frees this machine; status stays cancelled
    elif result == "interrupted":
        client.requeue(job_id)  # keep the folder: it resumes from its checkpoint
    else:
        client.finish(job_id, result, error)
    if finished:
        cleanup(job_id)


CHECKPOINT_LIMIT = 500 * 1024 * 1024


def _prepare_folder(client: Client, job: dict, resume_from: int, log) -> int:
    """Set up the task folder for a run. A task paused on this machine keeps its
    folder; one paused elsewhere gets the checkpoint that machine saved.
    Returns the step to resume from (0 = start over)."""
    import zipfile
    job_id, work = job["id"], task_dir(job["id"])
    if job.get("same_machine") and work.is_dir():
        return resume_from
    cleanup(job_id)  # an old copy from an earlier attempt would be out of date
    if not job.get("checkpoint") or not resume_from:
        return resume_from
    zpath = work.parent / f"{job_id}-checkpoint.zip"
    try:
        work.mkdir(parents=True, exist_ok=True)
        log("downloading the checkpoint saved by the previous machine")
        client.download_checkpoint(job_id, zpath)
        root, total = work.resolve(), 0
        with zipfile.ZipFile(zpath) as z:
            infos = z.infolist()
            if len(infos) > 10000:
                raise RuntimeError("too many files")
            for info in infos:
                name = info.filename.replace("\\", "/")
                target = (work / name).resolve()
                if name.startswith("/") or ":" in name or root not in target.parents:
                    raise RuntimeError(f"unsafe path {name!r}")
                total += info.file_size
                if total > 4 * CHECKPOINT_LIMIT:
                    raise RuntimeError("unpacks too large")
                if name.endswith("/"):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
        log(f"checkpoint restored ({len(infos)} files); continuing from step {resume_from}")
        return resume_from
    except Exception as e:
        cleanup(job_id)
        log(f"could not restore the checkpoint ({e}); starting over")
        return 0
    finally:
        zpath.unlink(missing_ok=True)


def _save_checkpoint(client: Client, job_id: str, log) -> None:
    """Upload a paused task's results so far (outputs/ plus files it created),
    so whichever machine runs it next can continue instead of starting over."""
    import zipfile
    work = task_dir(job_id)
    out = work / "outputs"
    files = [p for p in out.rglob("*") if p.is_file() and not p.is_symlink()] if out.is_dir() else []
    files += created_files(job_id)
    if not files:
        return
    mark = work / START_MARK
    zpath = work.parent / f"{job_id}-checkpoint.zip"
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files + ([mark] if mark.exists() else []):
                z.write(p, p.relative_to(work).as_posix())
        size = zpath.stat().st_size
        if size > CHECKPOINT_LIMIT:
            log(f"checkpoint is {size / 1e6:.0f} MB (over 500 MB): it will continue on this machine only")
            return
        client.upload_output(job_id, zpath, size, [], kind="checkpoint")
        log(f"checkpoint saved ({size / 1e6:.1f} MB): any of your machines can continue this task")
    except OutputsDisabled:
        pass
    except Exception as e:
        log(f"checkpoint not saved ({e}): it will continue on this machine, or start over elsewhere")
    finally:
        zpath.unlink(missing_ok=True)


def _upload_outputs(client: Client, job_id: str, log) -> None:
    """Zip the task's outputs/ folder and upload it, if it has anything in it.
    A task that never used outputs/ gets the files it created kept instead."""
    import zipfile
    out = task_dir(job_id) / "outputs"
    files = [p for p in out.rglob("*") if p.is_file()] if out.is_dir() else []
    if not files:
        files = created_files(job_id)
        if not files:
            return
        out = task_dir(job_id)
        names = ", ".join(p.relative_to(out).as_posix() for p in files[:5]) + (" ..." if len(files) > 5 else "")
        log(f"nothing in outputs/, so keeping the files this task created: {names}")
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
        log(f"outputs were not saved: {e}")
    finally:
        zpath.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
