"""Run one job as a subprocess and stream its results to the coordinator.

Job contract (simple and language-agnostic):
  * The coordinator ships the job's files inline in spec["files"]
    as { "relative/path.py": "<text>" | {"b64": "<base64 bytes>"}, ... }.
  * The worker writes them to a folder and runs spec["entry"] according to
    spec["kind"]: "python" (python <entry>), "node" (node <entry>, after
    npm install if there is a package.json) or "browser" (a website bot, see
    browser_harness.py).
  * The job reports progress by printing ONE JSON object per line to stdout:
        {"episode": {"index": 0, "reward": 23.0, "steps": 23, "extra": {}}}
        {"log": "any message"}
    Any line that is not such a JSON object is forwarded as a log line.
  * Exit code 0 => done, anything else => failed.
  * Files the task saves in outputs/ (path also in HARVEST_OUTPUTS) are zipped
    and uploaded by the agent when it finishes.

The job is paused (killed) if `should_stop()` becomes true — e.g. the user
started a game. Already-reported episodes are kept, so re-queuing resumes
from where it left off (the job reads RESUME_FROM to skip finished episodes).
"""
from __future__ import annotations

import base64
import hashlib
import re
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import runtimes
from .browser_harness import HARNESS

def _real_python(exe: str | None) -> str | None:
    """exe if it is a working interpreter (not the Microsoft Store placeholder)."""
    if not exe or "WindowsApps" in exe or not os.path.isfile(exe):
        return None
    try:
        r = subprocess.run([exe, "-c", "import sys; print(sys.executable)"], capture_output=True,
                           text=True, timeout=15, creationflags=NO_WINDOW)
        out = r.stdout.strip()
        return out if r.returncode == 0 and out and "WindowsApps" not in out else None
    except (OSError, subprocess.SubprocessError):
        return None


def _windows_pythons() -> list[str]:
    """Pythons registered in the Windows registry and common install folders,
    newest first. Finds an install even when it is not on PATH."""
    found: list[tuple[tuple, str]] = []
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    core = winreg.OpenKey(hive, r"SOFTWARE\Python\PythonCore", 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                for i in range(winreg.QueryInfoKey(core)[0]):
                    tag = winreg.EnumKey(core, i)
                    try:
                        with winreg.OpenKey(core, tag + r"\InstallPath", 0, winreg.KEY_READ | view) as k:
                            try:
                                exe = winreg.QueryValueEx(k, "ExecutablePath")[0]
                            except OSError:  # older installs only record the folder
                                exe = os.path.join(winreg.QueryValueEx(k, "")[0], "python.exe")
                    except OSError:
                        continue
                    nums = tuple(int(x) for x in re.findall(r"\d+", tag)[:2])
                    found.append((nums, exe))
    except ImportError:
        pass
    import glob
    for pattern in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe"),
                    r"C:\Program Files\Python3*\python.exe", r"C:\Python3*\python.exe"):
        for exe in glob.glob(pattern):
            nums = tuple(int(x) for x in re.findall(r"Python(\d)(\d+)", exe)[0]) if re.search(r"Python(\d)(\d+)", exe) else (3, 0)
            found.append((nums, exe))
    found.sort(reverse=True)
    return [exe for _, exe in found]


_PYTHON: list = []  # cached discovery result


def _python_exe() -> str | None:
    """Interpreter used to run task scripts, or None if the PC has no usable
    Python. In a packaged build (PyInstaller) sys.executable is the app itself,
    so look for the Python the user installed: HARVEST_PYTHON, then PATH, then
    the py launcher, the registry and the usual install folders. The Microsoft
    Store "python" placeholder in WindowsApps is never used."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    if os.environ.get("HARVEST_PYTHON") and _real_python(os.environ["HARVEST_PYTHON"]):
        return os.environ["HARVEST_PYTHON"]
    if _PYTHON:
        return _PYTHON[0]
    candidates = []
    for name in ("python", "python3"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            exe = shutil.which(name, path=d)
            if exe:
                candidates.append(exe)
    if sys.platform.startswith("win"):
        for launcher in (shutil.which("py"), r"C:\Windows\py.exe",
                         os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Launcher\py.exe")):
            if launcher and os.path.isfile(launcher):
                try:  # ask the launcher where the newest Python 3 lives
                    r = subprocess.run([launcher, "-3", "-c", "import sys; print(sys.executable)"],
                                       capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
                    if r.returncode == 0:
                        candidates.append(r.stdout.strip())
                except (OSError, subprocess.SubprocessError):
                    pass
        candidates += _windows_pythons()
        managed = runtimes.managed_python()  # Harvest's own copy (see runtimes.py)
        if managed:
            candidates.append(managed)
    for exe in candidates:
        real = _real_python(exe)
        if real:
            _PYTHON.append(real)
            return real
    return None


# Each job gets a stable folder here (keyed by job id) instead of a throwaway
# temp dir, so a job paused by gaming keeps its checkpoint and resumes for real
# when the same machine picks it back up.
WORK_ROOT = Path.home() / ".farm_work"


def _write_bundle(files: dict, dest: Path) -> None:
    root = dest.resolve()
    for rel, content in files.items():
        target = (dest / rel).resolve()
        # Never write outside the task folder, whatever the server sent.
        if root != target and root not in target.parents:
            raise ValueError(f"unsafe file path in task: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict) and "b64" in content:
            target.write_bytes(base64.b64decode(content["b64"]))
        else:
            target.write_text(str(content), encoding="utf-8")


# Task processes must not flash a console window when started from the tray app.
NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW


def _node_exe() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    d = runtimes.managed_node_dir()
    return os.path.join(d, "node.exe") if d else None


def _npm_exe() -> str | None:
    found = shutil.which("npm")
    if found:
        return found
    d = runtimes.managed_node_dir()
    return os.path.join(d, "npm.cmd") if d and os.path.exists(os.path.join(d, "npm.cmd")) else None


def reset_discovery() -> None:
    """Forget cached interpreter paths (after Harvest installed a runtime)."""
    _PYTHON.clear()


# Python tasks with a requirements.txt get their own environment, built once per
# PC and reused by any later task with the same requirements and Python.
ENVS_ROOT = (Path(os.environ["LOCALAPPDATA"]) / "Harvest" / "envs"
             if os.environ.get("LOCALAPPDATA") else Path.home() / ".harvest" / "envs")
KEEP_ENVS = 8
INTERRUPTED = object()


def _run_setup(cmd, cwd, env, on_log, should_stop, timeout=1800):
    """Run a setup step (venv, pip, npm), streaming its output into the task log.
    Returns True on success, False on failure, INTERRUPTED if should_stop()."""
    on_log("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=env, text=True, bufsize=1,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    q: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()
    deadline = time.monotonic() + timeout
    while True:
        if should_stop() or time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            if time.monotonic() > deadline:
                on_log(f"setup step took longer than {timeout // 60} minutes; stopped")
                return False
            return INTERRUPTED
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            break
        if line.strip():
            on_log(line)
    return proc.wait() == 0


def _python_env(work: Path, on_log, env, should_stop):
    """The Python to run this task with: the PC's own, or (if the task has a
    requirements.txt) a saved environment with those packages installed."""
    base = _python_exe()
    if not base:
        raise RuntimeError("Python was not found on this PC. Install Python 3.11+ from python.org "
                           "(tick 'Add python.exe to PATH'), then restart Harvest")
    req = work / "requirements.txt"
    if not req.exists():
        return base
    wanted = "\n".join(sorted(l.strip() for l in req.read_text(encoding="utf-8", errors="replace").splitlines()
                              if l.strip() and not l.strip().startswith("#")))
    try:
        ver = subprocess.run([base, "-c", "import sys; print(sys.version)"], capture_output=True, text=True,
                             timeout=30, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Python is not installed on this machine")
    key = hashlib.sha256((ver + "\n" + wanted).encode()).hexdigest()[:16]
    home = ENVS_ROOT / key
    py = home / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    ready = home / ".harvest-ready"
    if ready.exists():
        ready.touch()
        on_log(f"using this PC's saved environment for requirements.txt ({key})")
        return str(py)

    on_log("requirements.txt found: installing packages (once per PC, reused next time)")
    shutil.rmtree(home, ignore_errors=True)  # an earlier attempt that did not finish
    ENVS_ROOT.mkdir(parents=True, exist_ok=True)
    # --system-site-packages: packages already installed globally (say a CUDA
    # build of torch) are reused instead of downloaded again.
    for cmd in ([base, "-m", "venv", "--system-site-packages", home],
                [py, "-m", "pip", "install", "--disable-pip-version-check", "-r", req]):
        ok = _run_setup(cmd, work, env, on_log, should_stop)
        if ok is not True:
            shutil.rmtree(home, ignore_errors=True)
            if ok is INTERRUPTED:
                return INTERRUPTED
            raise RuntimeError("installing requirements.txt failed (see the output above)")
    ready.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    _prune_envs()
    return str(py)


def _prune_envs() -> None:
    """Keep the most recently used environments; delete the rest."""
    envs = sorted((d for d in ENVS_ROOT.iterdir() if (d / ".harvest-ready").exists()),
                  key=lambda d: (d / ".harvest-ready").stat().st_mtime, reverse=True)
    for d in envs[KEEP_ENVS:]:
        shutil.rmtree(d, ignore_errors=True)


def _command(spec: dict, work: Path, on_log, env, should_stop):
    """The command that runs the task, after any setup. INTERRUPTED if setup was stopped."""
    kind = spec.get("kind", "python")
    entry = spec.get("entry", "main.py")
    args = [str(a) for a in spec.get("args", [])]
    if kind == "node":
        node = _node_exe()
        if not node:
            raise RuntimeError("Node.js is not installed on this machine")
        node_dir = os.path.dirname(node)
        if node_dir not in env.get("PATH", "").split(os.pathsep):  # so npm and scripts find node
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        if (work / "package.json").exists() and not (work / "node_modules").exists():
            npm = _npm_exe()
            if not npm:
                on_log("package.json found but npm is not installed; skipping npm install")
            else:
                ok = _run_setup([npm, "install", "--no-audit", "--no-fund"], work, env, on_log, should_stop, 600)
                if ok is INTERRUPTED:
                    return INTERRUPTED
                if not ok:
                    raise RuntimeError("npm install failed (see the output above)")
        return [node, entry, *args]
    if kind == "browser":
        (work / "_harvest_browser.py").write_text(HARNESS, encoding="utf-8")
        (work / "_harvest_job.json").write_text(json.dumps({
            "url": spec["url"], "script_file": entry, "runs": spec.get("runs", 1),
            "timeout_s": spec.get("timeout_s", 60), "headless": spec.get("headless", True),
        }), encoding="utf-8")
        py = runtimes.playwright_python(_python_exe)
        if not py:
            raise RuntimeError("website tasks are not set up on this PC (Harvest installs them automatically, "
                               "or use Settings > Website tasks > Set up in the Harvest app)")
        return [py, "-u", "_harvest_browser.py"]
    py = _python_env(work, on_log, env, should_stop)
    if py is INTERRUPTED:
        return INTERRUPTED
    return [py, "-u", entry, *args]


def task_dir(job_id: str) -> Path:
    return WORK_ROOT / job_id


START_MARK = ".harvest-started"
_SKIP_DIRS = {"outputs", "node_modules", "__pycache__", ".git", ".venv", "venv"}


def created_files(job_id: str, limit: int = 500) -> list[Path]:
    """Files the task itself made or changed in its folder (outside outputs/),
    for tasks that save results next to their script instead of in outputs/."""
    work = task_dir(job_id)
    try:
        since = float((work / START_MARK).read_text())
    except (OSError, ValueError):
        return []
    found = []
    for p in work.rglob("*"):
        rel = p.relative_to(work)
        if (rel.parts[0] in _SKIP_DIRS or p.name == START_MARK or p.name.endswith(".tmp")
                or not p.is_file() or p.is_symlink()):
            continue
        if p.stat().st_mtime >= since:
            found.append(p)
            if len(found) >= limit:
                break
    return found


def cleanup(job_id: str) -> None:
    """Remove a finished task's folder (not done for paused tasks: they resume)."""
    shutil.rmtree(task_dir(job_id), ignore_errors=True)


def _reader(stream, q: "queue.Queue[str | None]") -> None:
    for line in iter(stream.readline, ""):
        q.put(line.rstrip("\n"))
    q.put(None)  # sentinel: stream closed


def run_job(
    job: dict,
    *,
    on_episode: Callable[[int, float, int, dict], None],
    on_log: Callable[[str], None],
    should_stop: Callable[[], bool],
    mode: str,
    resume_from: int,
    on_total: Callable[[int], None] | None = None,
) -> tuple[str, str | None]:
    """Returns (status, error): status is 'done' | 'failed' | 'interrupted'."""
    spec = job["spec"]

    work = WORK_ROOT / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    try:
        _write_bundle(spec.get("files", {}), work)

        (work / "outputs").mkdir(exist_ok=True)
        env = os.environ.copy()
        env["RESUME_FROM"] = str(resume_from)
        env["HARVEST_OUTPUTS"] = str(work / "outputs")
        env["HARVEST_STEPS"] = str(spec.get("episodes_total") or "")  # "Expected steps" from the task
        env["HARVEST_PARAMS"] = json.dumps(spec.get("params") or {})  # this run's sweep values
        # In "light" mode the user is at the machine: keep CPU use modest.
        cpu_budget = "2" if mode == "light" else "0"  # 0 => let the job decide
        env["FARM_CPU_BUDGET"] = cpu_budget
        if mode == "light":
            env["OMP_NUM_THREADS"] = "2"
            env["MKL_NUM_THREADS"] = "2"

        env["PYTHONUNBUFFERED"] = "1"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        cmd = _command(spec, work, on_log, env, should_stop)
        if cmd is INTERRUPTED:
            on_log("stopped while setting up; it will continue later")
            return "interrupted", None  # keep the folder
        on_log(f"$ {' '.join(cmd)}  (mode={mode}, resume_from={resume_from})")

        creationflags = NO_WINDOW
        if sys.platform.startswith("win") and mode == "light":
            creationflags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS  # type: ignore[attr-defined]

        started = work / START_MARK
        if not started.exists():  # first run only: a resumed task keeps its original start
            started.write_text(str(time.time()))
        proc = subprocess.Popen(
            cmd, cwd=work, env=env, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

        q: "queue.Queue[str | None]" = queue.Queue()
        threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

        error: str | None = None
        while True:
            if should_stop():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                on_log("stopped on this machine (paused for a game, or stopped from the dashboard)")
                return "interrupted", None  # keep work dir so the checkpoint survives

            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break  # stdout closed; process is finishing

            obj = None
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = None
            if isinstance(obj, dict) and isinstance(obj.get("progress"), dict):
                # General form: {"progress": {"step": 3, "value": 0.81, "total": 100}}
                pr = obj["progress"]
                try:
                    on_episode(int(pr.get("step", 0)), float(pr.get("value") or 0),
                               int(pr.get("steps", 0) or 0), pr.get("extra") or {})
                    if on_total and pr.get("total"):
                        on_total(int(pr["total"]))
                except (TypeError, ValueError):
                    on_log(line)
            elif isinstance(obj, dict) and "episode" in obj:
                # Reinforcement-learning form: {"episode": {"index": i, "reward": r}}
                e = obj["episode"]
                on_episode(
                    int(e["index"]), float(e.get("reward", 0)),
                    int(e.get("steps", 0)), e.get("extra", {}),
                )
            elif isinstance(obj, dict) and "log" in obj:
                on_log(str(obj["log"]))
            else:
                on_log(line)
                if "Traceback" in line or "Error" in line:
                    error = line

        code = proc.wait()
        status = "done" if code == 0 else "failed"
        # The agent uploads outputs/ and then removes the folder (see cleanup()).
        if status == "done":
            return "done", None
        return "failed", error or f"exited with code {code}"
    except Exception as e:
        # A task that cannot even start (missing runtime, bad files) fails on
        # its own instead of taking the whole agent down.
        shutil.rmtree(work, ignore_errors=True)
        on_log(f"could not run task: {e}")
        return "failed", str(e)
