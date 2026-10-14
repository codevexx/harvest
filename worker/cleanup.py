"""Process clean-up for the dashboard's "Clean up processes" button.

scan()  lists the apps running as this user, grouped by program, with their RAM and
        CPU use, and sorts them into "safe to close" (known background helpers with
        no window) and "your call" (everything else the user could close). System
        processes, security software, drivers and Harvest itself (with any running
        task) are left out and never touched.
clean() closes the apps the user ticked and can turn their startup entries off
        (Windows: the same switch as Task Manager's Startup tab, so it is reversible).

Nothing here acts on its own: the dashboard shows the scan and the user decides.
Every request is checked again on this PC, so a protected process is never closed
even if the server asked for it.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

WINDOWS = sys.platform.startswith("win")
MAC = sys.platform == "darwin"

# Never offered and never closed (lowercase names). Owned-by-another-user and
# OS-folder programs are skipped too, see protected().
PROTECTED = {
    # Windows core and shell
    "system", "system idle process", "registry", "secure system", "memory compression", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe", "fontdrvhost.exe",
    "dwm.exe", "explorer.exe", "sihost.exe", "taskhostw.exe", "ctfmon.exe", "runtimebroker.exe", "searchhost.exe",
    "searchindexer.exe", "searchapp.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "textinputhost.exe", "applicationframehost.exe", "lockapp.exe", "conhost.exe", "dllhost.exe", "wmiprvse.exe",
    "spoolsv.exe", "audiodg.exe", "wudfhost.exe", "dashost.exe", "sgrmbroker.exe", "userinit.exe", "taskmgr.exe",
    "openconsole.exe", "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "sshd.exe", "ssh.exe",
    # Security
    "msmpeng.exe", "nissrv.exe", "mpdefendercoreservice.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "smartscreen.exe", "mbamservice.exe", "avp.exe", "avastsvc.exe",
    # Harvest and its tunnel/storage helpers
    "harvest.exe", "harvest-setup.exe", "cloudflared.exe",
    # macOS
    "kernel_task", "launchd", "windowserver", "loginwindow", "dock", "finder", "systemuiserver", "controlcenter",
    "notificationcenter", "spotlight", "mds", "mds_stores", "coreaudiod", "harvest", "terminal", "iterm2",
    # Linux
    "systemd", "init", "xorg", "gnome-shell", "plasmashell", "kwin_x11", "kwin_wayland", "pipewire", "pulseaudio",
}
# Graphics, audio and input drivers' helpers: leave alone.
PROTECTED_PREFIXES = ("nvcontainer", "nvdisplay", "nvidia", "amd", "atieclxx", "atiesrxx", "igfx", "intel", "rtk",
                      "realtek", "logi", "synaptics", "elan", "wacom")

# Background helpers that are safe to close when they have no window: updaters,
# crash reporters, widgets. Pre-ticked in the dashboard (the user still confirms).
SAFE_TO_CLOSE = {
    "googleupdate.exe": "Google updater", "googlecrashhandler.exe": "crash reporter",
    "googlecrashhandler64.exe": "crash reporter", "microsoftedgeupdate.exe": "Edge updater",
    "adobearm.exe": "Adobe updater", "adobeipcbroker.exe": "Adobe background helper", "ccxprocess.exe": "Adobe background helper",
    "acrotray.exe": "Acrobat tray helper", "jusched.exe": "Java updater", "jucheck.exe": "Java updater",
    "onedrivestandaloneupdater.exe": "OneDrive updater", "ituneshelper.exe": "iTunes helper",
    "phoneexperiencehost.exe": "Phone Link in the background", "yourphone.exe": "Phone Link in the background",
    "widgets.exe": "Windows widgets", "widgetservice.exe": "Windows widgets",
    "gamebar.exe": "Xbox Game Bar in the background", "gamebarftserver.exe": "Xbox Game Bar in the background",
    "skypebackgroundhost.exe": "Skype in the background", "cortana.exe": "Cortana",
    "crashpad_handler.exe": "crash reporter", "spotifywebhelper.exe": "Spotify helper",
}

STATE_FILE = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".harvest") / "Harvest" / "cleanup.json"
              if WINDOWS else Path.home() / ".harvest" / "cleanup.json")


def _me() -> str:
    try:
        return psutil.Process().username()
    except Exception:
        return ""


def _harvest_pids() -> set[int]:
    """This agent, the apps that started it (tray / service) and everything it runs (tasks)."""
    keep: set[int] = set()
    try:
        me = psutil.Process()
        keep.add(me.pid)
        for p in me.parents():
            keep.add(p.pid)
        for c in me.children(recursive=True):
            keep.add(c.pid)
    except Exception:
        pass
    return keep


def _system_path(exe: str) -> bool:
    e = (exe or "").lower().replace("/", "\\") if WINDOWS else (exe or "")
    if WINDOWS:
        windir = (os.environ.get("WINDIR") or r"C:\Windows").lower()
        return e.startswith(windir + "\\") or "\\windowsapps\\microsoft." in e and "windows" in e
    if MAC:
        return e.startswith(("/System/", "/usr/", "/sbin/", "/bin/", "/Library/Apple/"))
    return e.startswith(("/usr/lib/", "/usr/libexec/", "/lib/", "/sbin/", "/bin/"))


def _work_paths() -> tuple[str, ...]:
    paths = [str(Path.home() / ".farm_work"), str(Path.home() / ".cache" / "harvest")]
    if WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            paths += [os.path.join(local, "Harvest"), os.path.join(local, "Programs", "Harvest")]
    return tuple(p.lower() for p in paths)


def protected(p, name: str, exe: str, user: str, me: str, keep: set[int]) -> bool:
    n = name.lower()
    if p.pid in keep or p.pid <= 4 or n in PROTECTED or n.startswith(PROTECTED_PREFIXES):
        return True
    if not user or (me and user != me):
        return True  # another user's or a system process (we could not close it anyway)
    if _system_path(exe):
        return True
    work = _work_paths()
    e = (exe or "").lower()
    if any(e.startswith(w) for w in work):
        return True
    # A Harvest task (e.g. node or python running in its task folder), even if the
    # app that started it has restarted since.
    try:
        cwd = (p.cwd() or "").lower()
    except Exception:
        cwd = ""
    return any(cwd.startswith(w) for w in work)


def _window_pids() -> set[int]:
    """Processes with a visible top-level window (Windows only)."""
    if not WINDOWS:
        return set()
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    pids: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
        return True

    user32.EnumWindows(cb, 0)
    return pids


# --- startup entries (Windows) -------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_RUN = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APPROVED_FOLDER = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"


def _startup_entries() -> list[dict]:
    """This user's startup programs (Run key and Startup folder), with on/off state."""
    if not WINDOWS:
        return []
    import winreg

    def approved(key_path, value):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                data, _ = winreg.QueryValueEx(k, value)
                return not (isinstance(data, bytes) and data[:1] in (b"\x03", b"\x01"))
        except OSError:
            return True

    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            i = 0
            while True:
                try:
                    name, cmd, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                out.append({"id": "run:" + name, "label": name, "command": str(cmd)[:300],
                            "enabled": approved(APPROVED_RUN, name)})
    except OSError:
        pass
    folder = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    if folder.is_dir():
        for f in folder.iterdir():
            if f.suffix.lower() in (".lnk", ".url", ".exe", ".bat", ".cmd"):
                out.append({"id": "folder:" + f.name, "label": f.stem, "command": f.name,
                            "enabled": approved(APPROVED_FOLDER, f.name)})
    return out


def _set_startup(entry_id: str, enabled: bool) -> str | None:
    """Turn a startup entry on/off the way Task Manager does. Returns an error or None."""
    if not WINDOWS:
        return "startup programs can only be changed on Windows for now"
    import winreg
    kind, _, name = entry_id.partition(":")
    key_path = {"run": APPROVED_RUN, "folder": APPROVED_FOLDER}.get(kind)
    if not key_path or not name or name not in {e["id"].partition(":")[2] for e in _startup_entries() if e["id"].startswith(kind + ":")}:
        return "no such startup entry"
    if "harvest" in name.lower() and not enabled:
        return "Harvest's own startup entry is left alone"
    # Task Manager's format: 0x02 = on, 0x03 = off, followed by an 8-byte FILETIME.
    ft = int((time.time() + 11644473600) * 10_000_000)
    data = (b"\x02\x00\x00\x00" + b"\x00" * 8) if enabled else (b"\x03\x00\x00\x00" + ft.to_bytes(8, "little"))
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, data)
    except OSError as e:
        return str(e)
    return None


def _match_startup(entries, exe: str, name: str):
    e, n = (exe or "").lower(), name.lower().rsplit(".", 1)[0]
    for s in entries:
        c = s["command"].lower()
        if (e and e in c) or (n and len(n) > 3 and (n in c or n in s["label"].lower())):
            return s
    return None


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass


# --- grouping processes into apps ---------------------------------------------

# Folders that hold several products: group one level deeper (Google\Chrome, Microsoft\Edge).
VENDOR_DIRS = {"google", "microsoft", "adobe", "common files", "mozilla", "apple", "apple computer", "nvidia corporation",
               "intel", "amd", "logitech", "razer", "corsair", "asus", "msi", "programs", "packages"}


def app_root(exe: str, name: str) -> tuple[str, str]:
    """(group key, display name): a Mac .app bundle, a Windows install folder, or the program itself."""
    if not exe:
        return name.lower(), name
    if MAC and ".app/" in exe:
        b = exe[: exe.index(".app/") + 4]
        return b.lower(), os.path.basename(b)[:-4]
    if WINDOWS:
        parts = exe.replace("/", "\\").split("\\")
        low = [x.lower() for x in parts]
        for anchor in ("program files", "program files (x86)", "local", "roaming"):
            if anchor in low[:-1]:
                i = low.index(anchor) + 1
                if i < len(parts) - 1 and low[i] in VENDOR_DIRS and i + 1 < len(parts) - 1:
                    i += 1
                if i < len(parts) - 1:
                    return "\\".join(low[: i + 1]), parts[i]
    return exe.lower(), name


# --- scan and clean ------------------------------------------------------------

def _app_groups():
    """{key: {...}} for every closable app, plus the number of protected processes."""
    me, keep = _me(), _harvest_pids()
    procs = []
    for p in psutil.process_iter(["name", "exe", "username", "memory_info"]):
        try:
            p.cpu_percent(None)  # first call sets the baseline
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.cpu_percent(None)  # baseline for the whole-PC reading in scan()
    time.sleep(1.5)
    windows = _window_pids()
    ncpu = psutil.cpu_count() or 1
    groups: dict[str, dict] = {}
    skipped = 0
    for p in procs:
        try:
            name = p.info.get("name") or ""
            exe = p.info.get("exe") or ""
            user = p.info.get("username") or ""
            if protected(p, name, exe, user, me, keep):
                skipped += 1
                continue
            mem = p.info["memory_info"].rss if p.info.get("memory_info") else 0
            cpu = p.cpu_percent(None) / ncpu
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        key, label = app_root(exe, name)
        g = groups.setdefault(key, {"key": key, "name": label, "exe": exe, "pids": [], "names": set(), "ram": 0, "cpu": 0.0,
                                    "window": False, "bundle": MAC and key.endswith(".app")})
        g["pids"].append(p.pid)
        g["names"].add(name.lower())
        g["ram"] += mem
        g["cpu"] += cpu
        g["window"] = g["window"] or p.pid in windows
    for g in groups.values():
        if g["bundle"] and "/applications/" in g["key"]:
            g["window"] = True  # a Mac app the user opened (quit it the normal way)
    return groups, skipped


def scan() -> dict:
    if psutil is None:
        return {"error": "psutil is not available in this Harvest install"}
    groups, skipped = _app_groups()
    entries = _startup_entries()
    apps = []
    for g in groups.values():
        s = _match_startup(entries, g["exe"], g["name"])
        # Safe only when every process in the group is a known background helper.
        reasons = [SAFE_TO_CLOSE.get(n) for n in g["names"]]
        reason = reasons[0] if reasons and all(reasons) else None
        safe = bool(reason) and not g["window"]
        apps.append({
            "key": g["key"], "name": g["name"], "exe": g["exe"], "count": len(g["pids"]),
            "processes": sorted(g["names"])[:6],
            "ram_mb": round(g["ram"] / 1048576), "cpu": round(g["cpu"], 1), "window": g["window"],
            "category": "safe" if safe else "ask",
            "reason": reason if safe else ("open app" if g["window"] else "running in the background"),
            "startup": {"id": s["id"], "label": s["label"], "enabled": s["enabled"]} if s else None,
        })
    apps.sort(key=lambda a: (a["category"] != "safe", -(a["ram_mb"] + 20 * a["cpu"])))
    vm = psutil.virtual_memory()
    st = _load_state()
    return {
        "platform": sys.platform, "apps": apps[:150], "protected": skipped,
        "ram_total_mb": round(vm.total / 1048576), "ram_used_mb": round((vm.total - vm.available) / 1048576),
        "cpu": psutil.cpu_percent(None),
        "startup_off": [e for e in entries if not e["enabled"] and e["id"] in st.get("disabled", {})],
        "startup_supported": WINDOWS,
    }


def _close_window_apps(pids: list[int]) -> None:
    """Ask windowed apps to close the normal way (they may ask to save)."""
    if not WINDOWS:
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    want = set(pids)
    WM_CLOSE = 0x0010

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in want and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    user32.EnumWindows(cb, 0)


def clean(close: list[str], startup_off: list[str], startup_on: list[str]) -> dict:
    if psutil is None:
        return {"error": "psutil is not available in this Harvest install"}
    results = []
    groups, _ = _app_groups()  # re-checked here: protected apps are never in this list
    for key in close[:100]:
        g = groups.get(str(key).lower())
        if not g:
            results.append({"key": key, "ok": False, "note": "not running (or protected)"})
            continue
        procs = []
        for pid in g["pids"]:
            try:
                procs.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                pass
        if g["window"]:
            if g.get("bundle"):  # macOS: quit like Cmd+Q, so the app can save or ask
                import subprocess
                subprocess.run(["osascript", "-e", f'tell application "{g["name"]}" to quit'], capture_output=True, timeout=10)
            else:
                _close_window_apps(g["pids"])  # polite first: the app can save or ask
            gone, alive = psutil.wait_procs(procs, timeout=6)
            if alive:
                results.append({"key": key, "name": g["name"], "ok": False,
                                "note": "asked it to close; it is still open (it may be waiting for you to save)"})
                continue
        else:
            for p in procs:
                try:
                    p.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            gone, alive = psutil.wait_procs(procs, timeout=4)
            for p in alive:
                try:
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            psutil.wait_procs(alive, timeout=3)
        freed = round(g["ram"] / 1048576)
        results.append({"key": key, "name": g["name"], "ok": True, "note": f"closed, about {freed} MB freed"})

    st = _load_state()
    disabled = st.setdefault("disabled", {})
    startup = []
    for sid in startup_off[:50]:
        err = _set_startup(str(sid), False)
        if not err:
            disabled[str(sid)] = int(time.time())
        startup.append({"id": sid, "ok": not err, "note": err or "won't start with Windows any more"})
    for sid in startup_on[:50]:
        err = _set_startup(str(sid), True)
        if not err:
            disabled.pop(str(sid), None)
        startup.append({"id": sid, "ok": not err, "note": err or "starts with Windows again"})
    _save_state(st)

    # Did anything come straight back (a launcher or service restarting it)?
    time.sleep(3)
    again, _ = _app_groups()
    for r in results:
        if r["ok"] and str(r["key"]).lower() in again:
            r["note"] += "; it started again by itself (something else launches it)"
    return {"closed": results, "startup": startup}


def run(command: dict) -> dict:
    """Entry point for a command from the server: {"kind": "scan"} or {"kind": "clean", ...}."""
    kind = command.get("kind")
    if kind == "scan":
        return scan()
    if kind == "clean":
        lists = lambda k: [str(x) for x in (command.get(k) or []) if isinstance(x, (str, int))]
        return clean(lists("close"), lists("startup_off"), lists("startup_on"))
    return {"error": f"unknown command {kind!r}"}
