"""Self-update from GitHub Releases.

Every few hours the app asks GitHub for the latest release of codevexx/harvest.
If it is newer, it downloads the platform's asset (Harvest.exe on Windows) plus
the matching .sha256 file that CI publishes, checks the hash, and swaps the new
build in: a running .exe can be renamed but not overwritten, so the current one
is moved to Harvest.old.exe, the new one takes its place, and the app restarts.
The old file is deleted on the next start.

Linux will get its own asset name here when that app exists.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import requests

REPO = "codevexx/harvest"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAMES = {"win32": "Harvest.exe"}  # future: "linux": "harvest-linux-x86_64"
ASSET = ASSET_NAMES.get(sys.platform)
CHECK_EVERY = 6 * 3600

DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "Harvest"
UPDATE_DIR = DATA_DIR / "updates"


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def current_exe() -> Path | None:
    """The installed executable, if this is a packaged build we can replace."""
    if getattr(sys, "frozen", False) and ASSET:
        return Path(sys.executable)
    return None


def cleanup_old() -> None:
    exe = current_exe()
    if not exe:
        return
    old = exe.with_name(exe.stem + ".old" + exe.suffix)
    for _ in range(20):  # the previous process may still be exiting
        try:
            if old.exists():
                old.unlink()
            return
        except OSError:
            time.sleep(0.5)


class Updater:
    def __init__(self, version: str, on_change: Callable[[], None], relaunch: bool = True):
        self.version = version
        self.relaunch = relaunch  # background mode restarts through Task Scheduler instead
        self.on_change = on_change
        self.lock = threading.Lock()
        self.state = {"checking": False, "available": None, "downloading": False,
                      "progress": 0.0, "ready": False, "error": None, "message": None,
                      "can_self_update": current_exe() is not None}
        self._asset_url = self._sha_url = None
        self._file: Path | None = None
        self.s = requests.Session()
        self.s.headers.update({"user-agent": f"Harvest/{version}",
                               "accept": "application/vnd.github+json"})

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)
        self.on_change()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    # --- steps ------------------------------------------------------------

    def check(self, manual: bool = False) -> bool:
        """True if a newer version is available."""
        self._set(checking=True, error=None, message=None)
        try:
            r = self.s.get(LATEST_URL, timeout=20)
            r.raise_for_status()
            rel = r.json()
            tag = rel.get("tag_name") or ""
            if parse_version(tag) <= parse_version(self.version):
                self._set(checking=False, available=None,
                          message="You have the latest version" if manual else None)
                return False
            assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
            self._asset_url = assets.get(ASSET or "")
            self._sha_url = assets.get((ASSET or "") + ".sha256")
            self._set(checking=False, available=tag.lstrip("v"))
            return True
        except Exception as e:
            self._set(checking=False, message=f"Could not check for updates ({type(e).__name__})" if manual else None)
            return False

    def download(self) -> bool:
        if not (self._asset_url and self._sha_url):
            self._set(error="This release has no verified download for your system")
            return False
        ver = self.state["available"]
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        part = UPDATE_DIR / f"Harvest-{ver}.part"
        final = UPDATE_DIR / f"Harvest-{ver}{Path(ASSET).suffix}"
        self._set(downloading=True, progress=0.0, error=None)
        try:
            expected = self.s.get(self._sha_url, timeout=20).text.split()[0].lower()
            h = hashlib.sha256()
            with self.s.get(self._asset_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done = 0
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total:
                            self.state["progress"] = done / total
                            if done % (1 << 21) < (1 << 16):
                                self.on_change()
            if h.hexdigest() != expected:
                part.unlink(missing_ok=True)
                self._set(downloading=False, error="Download did not match its checksum, skipped")
                return False
            os.replace(part, final)
            self._file = final
            self._set(downloading=False, ready=True, progress=1.0)
            return True
        except Exception as e:
            part.unlink(missing_ok=True)
            self._set(downloading=False, error=f"Download failed ({type(e).__name__})")
            return False

    def apply(self) -> bool:
        """Swap in the downloaded build and start it. Caller must exit right after."""
        exe = current_exe()
        if not (exe and self._file and self._file.exists()):
            return False
        old = exe.with_name(exe.stem + ".old" + exe.suffix)
        try:
            if old.exists():
                old.unlink()
            os.replace(exe, old)
            try:
                shutil.copy2(self._file, exe)
            except Exception:
                os.replace(old, exe)  # put the working build back
                raise
        except Exception as e:
            self._set(error=f"Could not install the update ({type(e).__name__}). Is Harvest in a protected folder?")
            return False
        self._file.unlink(missing_ok=True)
        if self.relaunch:
            flags = 0x00000008 | 0x00000200 if sys.platform.startswith("win") else 0  # DETACHED | NEW_GROUP
            subprocess.Popen([str(exe), "--updated"], creationflags=flags, close_fds=True)
        return True

    # --- background loop --------------------------------------------------

    def run(self, stop: threading.Event, auto: Callable[[], bool], idle: Callable[[], bool],
            restart: Callable[[], None]) -> None:
        """auto(): user allows automatic updates. idle(): no task running.
        restart(): quit this process after apply() started the new one."""
        if stop.wait(20):
            return
        while True:
            if self.check() and auto() and self.state["can_self_update"] and not self.state["ready"]:
                self.download()
            # Once downloaded, wait for a quiet moment to restart.
            while self.state["ready"] and auto() and not stop.is_set():
                if idle() and self.apply():
                    restart()
                    return
                if stop.wait(30):
                    return
            if stop.wait(CHECK_EVERY):
                return
