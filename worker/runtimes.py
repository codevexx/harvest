"""Install what tasks need, automatically and privately (Windows).

If a PC has no Python or Node.js, Harvest downloads its own copies into
%LOCALAPPDATA%\\Harvest\\runtimes. Nothing is installed system-wide, no admin
rights are needed, and nothing else on the PC changes. A Python or Node the
user installed themselves always takes priority (see runner._python_exe).

Every download is verified before use:
  * Python: the official "python" package from nuget.org (published by the
    Python core team), checked against the SHA-512 nuget.org records for it,
    and python.exe must carry a valid "Python Software Foundation" signature.
  * Node.js: the official zip from nodejs.org, checked against nodejs.org's
    SHASUMS256.txt, and node.exe must be signed by the "OpenJS Foundation".
  * Website tasks: pip installs Playwright, which downloads its own Chromium.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable

import requests

PY_SERIES = "3.13"
NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
UA = {"user-agent": "Harvest (runtime setup)"}


def supported() -> bool:
    return sys.platform.startswith("win")


def root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".harvest") / "Harvest" / "runtimes"


def _ready(d: Path) -> bool:
    try:
        return (d / ".harvest-ready").exists()
    except OSError:  # unreadable (see repair_permissions): treat as not installed
        return False


def _latest_ready(prefix: str) -> Path | None:
    dirs = [d for d in root().glob(prefix + "-*") if _ready(d)]
    key = lambda d: tuple(int(x) for x in re.findall(r"\d+", d.name.split("-", 1)[1])[:3])
    return max(dirs, key=key) if dirs else None


def managed_python() -> str | None:
    d = _latest_ready("python")
    return str(d / "python.exe") if d and (d / "python.exe").exists() else None


def managed_node_dir() -> str | None:
    d = _latest_ready("node")
    return str(d) if d and (d / "node.exe").exists() else None


# --- verification ------------------------------------------------------------

def _signed_by(path: Path, who: str) -> bool:
    """Valid Authenticode signature whose signer name contains `who`."""
    ps = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
          "if ($s.Status -eq 'Valid') { $s.SignerCertificate.Subject } else { 'INVALID' }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        return r.returncode == 0 and who in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _download(url: str, dest: Path, log, label: str) -> None:
    with requests.get(url, stream=True, timeout=60, headers=UA) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done, last = 0, 0.0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if total and time.monotonic() - last > 3:
                    log(f"{label}: {done * 100 // total}% of {total // 1048576} MB")
                    last = time.monotonic()


def _extract(zpath: Path, dest: Path, inner: str = "") -> None:
    """Unzip only entries under `inner` into dest, refusing paths that escape it."""
    dest_r = dest.resolve()
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith(inner) or name.endswith("/"):
                continue
            target = (dest / name[len(inner):]).resolve()
            if dest_r not in target.parents:
                raise RuntimeError(f"unsafe path in archive: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def _install(name: str, version: str, build: Callable[[Path, Path], None], log) -> Path:
    """Build into a temporary folder, then move it into place in one step."""
    root().mkdir(parents=True, exist_ok=True)
    final = root() / f"{name}-{version}"
    if _ready(final):
        return final
    # A plain folder, not tempfile.mkdtemp: from Python 3.13, mkdtemp folders on
    # Windows only allow the creating logon session, and that would follow the
    # runtime when it is moved into place (the tray app then could not read a
    # runtime the background copy installed).
    work = root() / f".{name}-{os.getpid()}-{int(time.time() * 1000)}"
    work.mkdir()
    try:
        build(work, work / "out")
        shutil.rmtree(final, ignore_errors=True)
        os.replace(work / "out", final)
        (final / ".harvest-ready").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- Python --------------------------------------------------------------------

def install_python(log) -> str:
    idx = requests.get("https://api.nuget.org/v3-flatcontainer/python/index.json", timeout=30, headers=UA).json()
    versions = [v for v in idx["versions"] if re.fullmatch(re.escape(PY_SERIES) + r"\.\d+", v)]
    if not versions:
        raise RuntimeError("no Python release found")
    v = max(versions, key=lambda s: tuple(int(x) for x in s.split(".")))

    def build(work: Path, out: Path):
        reg = requests.get(f"https://api.nuget.org/v3/registration5-semver1/python/{v}.json", timeout=30, headers=UA).json()
        entry = requests.get(reg["catalogEntry"], timeout=30, headers=UA).json()
        if entry.get("packageHashAlgorithm", "").upper() != "SHA512":
            raise RuntimeError("unexpected package hash type")
        pkg = work / "python.nupkg"
        log(f"Downloading Python {v}")
        _download(f"https://api.nuget.org/v3-flatcontainer/python/{v}/python.{v}.nupkg", pkg, log, f"Downloading Python {v}")
        digest = base64.b64encode(hashlib.sha512(pkg.read_bytes()).digest()).decode()
        if digest != entry["packageHash"]:
            raise RuntimeError("Python download did not match its published checksum")
        _extract(pkg, out, "tools/")
        if not _signed_by(out / "python.exe", "Python Software Foundation"):
            raise RuntimeError("python.exe is not validly signed by the Python Software Foundation")
        r = subprocess.run([str(out / "python.exe"), "-m", "pip", "--version"], capture_output=True,
                           text=True, timeout=120, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError("the downloaded Python does not work")

    return str(_install("python", v, build, log) / "python.exe")


# --- Node.js -------------------------------------------------------------------

def install_node(log) -> str:
    dist = requests.get("https://nodejs.org/dist/index.json", timeout=30, headers=UA).json()
    lts = next(r for r in dist if r.get("lts") and "win-x64-zip" in r.get("files", []))
    v = lts["version"]  # e.g. v22.11.0
    zname = f"node-{v}-win-x64.zip"

    def build(work: Path, out: Path):
        sums = requests.get(f"https://nodejs.org/dist/{v}/SHASUMS256.txt", timeout=30, headers=UA).text
        want = next((l.split()[0] for l in sums.splitlines() if l.strip().endswith(zname)), None)
        if not want:
            raise RuntimeError("Node.js checksum not published")
        z = work / zname
        log(f"Downloading Node.js {v}")
        _download(f"https://nodejs.org/dist/{v}/{zname}", z, log, f"Downloading Node.js {v}")
        if hashlib.sha256(z.read_bytes()).hexdigest() != want:
            raise RuntimeError("Node.js download did not match its published checksum")
        _extract(z, out, f"node-{v}-win-x64/")
        if not _signed_by(out / "node.exe", "OpenJS Foundation"):
            raise RuntimeError("node.exe is not validly signed by the OpenJS Foundation")

    return str(_install("node", v.lstrip("v"), build, log) / "node.exe")


# --- Website tasks (Playwright) ---------------------------------------------------

def has_playwright(python: str | None) -> bool:
    if not python:
        return False
    try:
        r = subprocess.run([python, "-c", "import playwright"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def playwright_python(find_python: Callable[[], str | None]) -> str | None:
    """A Python that can run website tasks: the user's own if it already has
    Playwright, otherwise Harvest's private copy (Harvest never adds packages to
    a Python the user installed)."""
    for py in (find_python(), managed_python()):
        if has_playwright(py):
            return py
    return None


def install_playwright(python: str, log) -> None:
    for cmd, label in (([python, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "playwright"], "Installing Playwright"),
                       ([python, "-m", "playwright", "install", "chromium"], "Downloading Chrome for website tasks")):
        log(label)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=NO_WINDOW)
        last = 0.0
        for line in p.stdout:
            if line.strip() and time.monotonic() - last > 3:
                log(f"{label}: {line.strip()[:70]}")
                last = time.monotonic()
        if p.wait() != 0:
            raise RuntimeError(f"{label} failed")


# --- permissions repair ------------------------------------------------------------

ACL_MARK = ".permissions-ok-1"


def repair_permissions(log=print) -> None:
    """Harvest 0.5-0.7 could leave runtimes readable only by the copy of Harvest
    that installed them (see _install). Reset them to the normal inherited
    permissions once. Only works from a process that can still open them, so
    the tray app and the background copy both try; the mark is written once it
    has worked."""
    if not supported() or not root().is_dir() or (root() / ACL_MARK).exists():
        return
    try:
        r = subprocess.run(["icacls", str(root()), "/reset", "/T", "/C", "/Q"], capture_output=True, text=True,
                           timeout=600, creationflags=NO_WINDOW)
        failed = re.search(r"Failed processing (\d+) files", r.stdout or "")
        if r.returncode == 0 and failed and failed.group(1) == "0":
            (root() / ACL_MARK).write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            log("runtime folder permissions checked")
        else:
            log("could not reset runtime folder permissions yet (another Harvest process will retry)")
    except (OSError, subprocess.SubprocessError):
        pass


# --- one call for the agent ----------------------------------------------------

class _Lock:
    """One installer at a time (tray app and background copy share the folder)."""

    def __init__(self):
        self.path = root() / ".installing"

    def __enter__(self):
        root().mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 3600
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                if time.time() - self.path.stat().st_mtime > 3600:  # left over from a crash
                    shutil.rmtree(self.path, ignore_errors=True)
                    continue
                if time.monotonic() > deadline:
                    raise RuntimeError("another setup is still running")
                time.sleep(5)

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


def ensure(find_python: Callable[[], str | None], find_node: Callable[[], str | None],
           want_browser: bool, log: Callable[[str], None]) -> list[str]:
    """Install whatever is missing. Returns a list of problems (empty = all good)."""
    problems = []
    with _Lock():
        python = find_python()
        if not python:
            try:
                python = install_python(log)
                log("Python is ready")
            except Exception as e:
                problems.append(f"Python: {e}")
        if not find_node():
            try:
                install_node(log)
                log("Node.js is ready")
            except Exception as e:
                problems.append(f"Node.js: {e}")
        if want_browser and not playwright_python(find_python):
            try:  # into Harvest's own Python, never the user's
                private = managed_python() or install_python(log)
                install_playwright(private, log)
                log("Website tasks are ready")
            except Exception as e:
                problems.append(f"Website tasks: {e}")
    return problems
