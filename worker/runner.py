"""Run one job as a subprocess and stream its results to the coordinator.

Job contract (simple and language-agnostic):
  * The coordinator ships the job's files inline in spec["files"]
    as { "relative/path.py": "<file contents>", ... }.
  * The worker writes them to a temp folder and runs spec["entry"].
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

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

# Each job gets a stable folder here (keyed by job id) instead of a throwaway
# temp dir, so a job paused by gaming keeps its checkpoint and resumes for real
# when the same machine picks it back up.
WORK_ROOT = Path.home() / ".farm_work"


def _write_bundle(files: dict[str, str], dest: Path) -> None:
    for rel, content in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


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
    entry = spec.get("entry", "main.py")
    args = [str(a) for a in spec.get("args", [])]

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

        cmd = [sys.executable, "-u", entry, *args]
        on_log(f"$ {' '.join(cmd)}  (mode={mode}, resume_from={resume_from})")

        creationflags = 0
        if sys.platform.startswith("win") and mode == "light":
            creationflags = subprocess.BELOW_NORMAL_PRIORITY_CLASS  # type: ignore[attr-defined]

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
                on_log("paused: machine is busy, job stopped (will resume on re-queue)")
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
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
