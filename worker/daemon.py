"""Harvest as a background service on macOS and Linux.

install.sh sets this up under launchd (macOS, runs while you are signed in) or
systemd (Linux, also before anyone logs in when lingering is allowed):

    <HARVEST_HOME>/app      this code (from the release's harvest-agent.tar.gz)
    <HARVEST_HOME>/venv     its Python packages
    <HARVEST_HOME>/config.json   server, connect token, machine name

It runs the agent and, every few hours, updates itself from the latest GitHub
release: the new harvest-agent.tar.gz is checked against its published .sha256,
unpacked next to the current version and swapped in while no task is running.
The process then exits and the service manager starts the new version.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import requests

from .agent import run_agent

REPO = "codevexx/harvest"
CHECK_EVERY = 6 * 3600


def home() -> Path:
    if os.environ.get("HARVEST_HOME"):
        return Path(os.environ["HARVEST_HOME"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Harvest"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "harvest"


def version() -> str:
    try:
        return (home() / "app" / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


def load_config() -> dict:
    cfg = {"coordinator": "https://harvest.codevexx.com", "name": os.uname().nodename.split(".")[0],
           "idle_threshold": 300, "games": [], "auto_update": True}
    try:
        cfg.update(json.loads((home() / "config.json").read_text()))
    except (OSError, ValueError):
        pass
    cfg["app_version"] = version()
    if cfg.get("python"):
        os.environ.setdefault("HARVEST_PYTHON", cfg["python"])
    return cfg


def _newer(a: str, b: str) -> bool:
    key = lambda v: tuple(int(x) for x in v.lstrip("v").split(".")[:3] if x.isdigit())
    try:
        return key(a) > key(b)
    except ValueError:
        return False


def update_available() -> str | None:
    r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=30,
                     headers={"accept": "application/vnd.github+json"})
    r.raise_for_status()
    tag = r.json().get("tag_name", "")
    return tag if _newer(tag, version()) else None


def install_update(tag: str) -> None:
    """Download, verify and swap in a new version. The caller restarts."""
    base = f"https://github.com/{REPO}/releases/download/{tag}/harvest-agent.tar.gz"
    want = requests.get(base + ".sha256", timeout=30).text.split()[0].lower()
    with tempfile.TemporaryDirectory(dir=home()) as tmp:
        tgz = Path(tmp) / "agent.tar.gz"
        with requests.get(base, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tgz, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        if hashlib.sha256(tgz.read_bytes()).hexdigest() != want:
            raise RuntimeError("the update did not match its published checksum")
        new = home() / "app.new"
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir()
        with tarfile.open(tgz) as t:
            for m in t.getmembers():  # never write outside the folder
                if m.name.startswith(("/", "..")) or ".." in Path(m.name).parts or not (m.isfile() or m.isdir()):
                    raise RuntimeError(f"unsafe file in update: {m.name}")
            t.extractall(new)
        old = home() / "app.old"
        shutil.rmtree(old, ignore_errors=True)
        os.replace(home() / "app", old)
        os.replace(new, home() / "app")


def main() -> None:
    cfg = load_config()
    if not cfg.get("token"):
        sys.exit("No connect token in config.json. Run install.sh again with your token.")
    print(f"Harvest {version()} on {sys.platform}: working as {cfg['name']!r} for {cfg['coordinator']}", flush=True)
    stop = threading.Event()
    state = {"busy": False}

    def on_status(mode, st, detail, last={}):
        state["busy"] = st == "busy"
        if (mode, st, detail) != last.get("v"):
            last["v"] = (mode, st, detail)
            print(f"{mode}/{st}" + (f" {detail}" if detail else ""), flush=True)

    t = threading.Thread(target=run_agent, args=(cfg, stop, on_status), daemon=True)
    t.start()
    next_check = time.monotonic() + 120
    while t.is_alive():
        time.sleep(5)
        if cfg.get("auto_update", True) and time.monotonic() >= next_check and not state["busy"]:
            next_check = time.monotonic() + CHECK_EVERY
            try:
                tag = update_available()
                if tag:
                    print(f"updating to {tag}", flush=True)
                    install_update(tag)
                    stop.set()
                    t.join(timeout=30)
                    print("updated; restarting", flush=True)
                    sys.exit(0)  # launchd / systemd start the new version
            except Exception as e:
                print(f"update check failed: {e}", flush=True)
    sys.exit(1)  # the agent stopped: let the service manager restart us


if __name__ == "__main__":
    main()
