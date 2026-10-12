"""Decide whether a machine is free to do farm work right now.

Modes:
    full   - nobody at the keyboard (or overnight window): run everything
    light  - someone is using the PC lightly: throttle, CPU only
    paused - a game / fullscreen app is running: do no work, pause jobs
    held   - the owner pressed Pause in the tray app: same as paused

Works on Windows, macOS and Linux. Fullscreen detection is Windows-only; on
macOS and Linux add your games to the list so they always pause tasks.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time

try:
    import psutil
except ImportError:  # psutil is required on workers; keep import soft for dev
    psutil = None  # type: ignore

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
NOBODY = 10_000_000.0  # "idle" for a machine nobody is sitting at


def _run(cmd: list[str], timeout: float = 3) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _mac_idle() -> float:
    m = re.search(r'"HIDIdleTime" = (\d+)', _run(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    return int(m.group(1)) / 1e9 if m else NOBODY


def _linux_sessions() -> list[dict]:
    """Active local login sessions (graphical or console), from systemd-logind.
    SSH sessions do not count: logging in remotely to a server is not using it."""
    out = []
    for line in _run(["loginctl", "list-sessions", "--no-legend"]).splitlines():
        sid = line.split()[0] if line.split() else None
        if not sid:
            continue
        props = dict(l.split("=", 1) for l in _run(["loginctl", "show-session", sid, "-p", "Type", "-p", "Active",
                     "-p", "Remote", "-p", "IdleHint", "-p", "IdleSinceHintMonotonic"]).splitlines() if "=" in l)
        if props.get("Active") == "yes" and props.get("Remote") != "yes" and props.get("Type") in ("x11", "wayland", "tty", "mir"):
            out.append(props)
    return out


def _linux_idle() -> float:
    sessions = _linux_sessions()
    if not sessions:
        return NOBODY  # a server, or nobody logged in at the machine
    # GNOME (Wayland or X11) reports exact idle time over D-Bus.
    m = re.search(r"uint64 (\d+)", _run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                                          "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                                          "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"]))
    if m:
        return int(m.group(1)) / 1000
    if shutil.which("xprintidle"):
        v = _run(["xprintidle"]).strip()
        if v.isdigit():
            return int(v) / 1000
    # Otherwise the desktop's idle hint (set when the screen locks or blanks).
    for s in sessions:
        if s.get("IdleHint") == "yes" and s.get("IdleSinceHintMonotonic", "0").isdigit():
            return max(0.0, time.monotonic() - int(s["IdleSinceHintMonotonic"]) / 1e6)
    return 0.0


_cache = {"t": 0.0, "v": 0.0}


# --- seconds since the user last touched keyboard/mouse ------------------

def idle_seconds() -> float:
    if IS_WINDOWS:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
            return millis / 1000.0
        return 0.0
    # macOS / Linux ask other programs, so remember the answer for a few seconds.
    now = time.monotonic()
    if now - _cache["t"] > 5:
        _cache["v"] = _mac_idle() if IS_MAC else _linux_idle() if sys.platform.startswith("linux") else NOBODY
        _cache["t"] = now
    return _cache["v"]


# --- is a game / fullscreen app in the foreground ------------------------

def running_games(game_exes: list[str]) -> list[str]:
    if psutil is None or not game_exes:
        return []
    wanted = {g.lower() for g in game_exes}
    hits = []
    for p in psutil.process_iter(["name"]):
        n = (p.info.get("name") or "").lower()
        if n in wanted:
            hits.append(n)
    return hits


def foreground_is_fullscreen() -> bool:
    """True if the focused window covers (roughly) the whole primary screen."""
    if not IS_WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    # The desktop/shell windows are not "a fullscreen app".
    shell = user32.GetShellWindow()
    if hwnd == shell or hwnd == user32.GetDesktopWindow():
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    sw = user32.GetSystemMetrics(0)  # SM_CXSCREEN
    sh = user32.GetSystemMetrics(1)  # SM_CYSCREEN
    w, h = rect.right - rect.left, rect.bottom - rect.top
    return w >= sw and h >= sh


def someone_signed_in() -> bool:
    """True if any user has an active Windows session (console or remote desktop).
    Used in background mode, which runs before sign-in and cannot see input."""
    if IS_MAC:
        return True  # the Mac agent runs inside the signed-in user's session
    if not IS_WINDOWS:
        return bool(_linux_sessions())
    import ctypes
    from ctypes import wintypes

    class WTS_SESSION_INFOW(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR),
                    ("State", ctypes.c_int)]

    wtsapi = ctypes.WinDLL("wtsapi32")
    info = ctypes.POINTER(WTS_SESSION_INFOW)()
    count = wintypes.DWORD()
    if not wtsapi.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(info), ctypes.byref(count)):
        return True  # cannot tell: assume someone is there and stay light
    try:
        return any(info[i].SessionId != 0 and info[i].State == 0  # WTSActive
                   for i in range(count.value))
    finally:
        wtsapi.WTSFreeMemory(info)


# --- the one call the agent uses -----------------------------------------

POWER_MODES = ("full", "light", "paused")


def get_mode(cfg: dict) -> str:
    """cfg keys: idle_threshold (s), games (list[str]), pause_on_fullscreen (bool),
    mode_override ("full" | "light" | "paused" | None = automatic; set from the
    tray app or the dashboard), user_paused (older Pause button)."""
    override = cfg.get("mode_override")
    if override == "paused" or cfg.get("user_paused"):
        return "held"  # like paused, but shown as "paused by you"
    auto = _auto_mode(cfg)
    if auto == "paused":
        return "paused"  # a game always wins, even over "full power"
    return override if override in ("full", "light") else auto


def _auto_mode(cfg: dict) -> str:
    if cfg.get("headless"):
        # Background mode (before sign-in) cannot see the user's keyboard or
        # windows, so: games by name pause, anyone signed in means light.
        if running_games(cfg.get("games", [])):
            return "paused"
        return "light" if someone_signed_in() else "full"
    games = running_games(cfg.get("games", []))
    if games:
        return "paused"
    if cfg.get("pause_on_fullscreen", True) and foreground_is_fullscreen():
        return "paused"
    if idle_seconds() >= cfg.get("idle_threshold", 300):
        return "full"
    return "light"
