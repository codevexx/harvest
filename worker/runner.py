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

The job is paused (killed) if `should_stop()` becomes true — e.g. the user
started a game. Already-reported episodes are kept, so re-queuing resumes
from where it left off (the job reads RESUME_FROM to skip finished episodes).
"""
from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from .browser_harness import HARNESS

def _python_exe() -> str:
    """Interpreter used to run task scripts. In a packaged build (PyInstaller)
    sys.executable is the app itself, so fall back to the Python the user
    installed for their tasks (overridable with HARVEST_PYTHON)."""
    if getattr(sys, "frozen", False):
        return (os.environ.get("HARVEST_PYTHON")
                or shutil.which("python") or shutil.which("py") or "python")
    return sys.executable


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
    return shutil.which("node")


def _npm_install(work: Path, on_log, env) -> None:
    """Install a Node task's dependencies into its own folder (never global)."""
    if not (work / "package.json").exists() or (work / "node_modules").exists():
        return
    npm = shutil.which("npm")
    if not npm:
        on_log("package.json found but npm is not installed; skipping npm install")
        return
    on_log("$ npm install")
    r = subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=work, env=env,
                       capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW)
    for line in (r.stdout + r.stderr).splitlines()[-15:]:
        on_log(line)


def _command(spec: dict, work: Path, on_log, env) -> list[str]:
    kind = spec.get("kind", "python")
    entry = spec.get("entry", "main.py")
    args = [str(a) for a in spec.get("args", [])]
    if kind == "node":
        node = _node_exe()
        if not node:
            raise RuntimeError("Node.js is not installed on this machine")
        _npm_install(work, on_log, env)
        return [node, entry, *args]
    if kind == "browser":
        (work / "_harvest_browser.py").write_text(HARNESS, encoding="utf-8")
        (work / "_harvest_job.json").write_text(json.dumps({
            "url": spec["url"], "script_file": entry, "runs": spec.get("runs", 1),
            "timeout_s": spec.get("timeout_s", 60), "headless": spec.get("headless", True),
        }), encoding="utf-8")
        return [_python_exe(), "-u", "_harvest_browser.py"]
    return [_python_exe(), "-u", entry, *args]


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
) -> tuple[str, str | None]:
    """Returns (status, error): status is 'done' | 'failed' | 'interrupted'."""
    spec = job["spec"]

    work = WORK_ROOT / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    try:
        _write_bundle(spec.get("files", {}), work)

        env = os.environ.copy()
        env["RESUME_FROM"] = str(resume_from)
        # In "light" mode the user is at the machine: keep CPU use modest.
        cpu_budget = "2" if mode == "light" else "0"  # 0 => let the job decide
        env["FARM_CPU_BUDGET"] = cpu_budget
        if mode == "light":
            env["OMP_NUM_THREADS"] = "2"
            env["MKL_NUM_THREADS"] = "2"

        env["PYTHONUNBUFFERED"] = "1"
        cmd = _command(spec, work, on_log, env)
        on_log(f"$ {' '.join(cmd)}  (mode={mode}, resume_from={resume_from})")

        creationflags = NO_WINDOW
        if sys.platform.startswith("win") and mode == "light":
            creationflags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS  # type: ignore[attr-defined]

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
            if isinstance(obj, dict) and "episode" in obj:
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
        shutil.rmtree(work, ignore_errors=True)  # finished for good; clean up
        if status == "done":
            return "done", None
        return "failed", error or f"exited with code {code}"
    except Exception as e:
        # A task that cannot even start (missing runtime, bad files) fails on
        # its own instead of taking the whole agent down.
        shutil.rmtree(work, ignore_errors=True)
        on_log(f"could not run task: {e}")
        return "failed", str(e)
