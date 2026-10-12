"""Decide whether a machine is free to do farm work right now.

Modes:
    full   - nobody at the keyboard (or overnight window): run everything
    light  - someone is using the PC lightly: throttle, CPU only
    paused - a game / fullscreen app is running: do no work, pause jobs
    held   - the owner pressed Pause in the tray app: same as paused

Windows is the primary target. The module still imports on macOS/Linux so
you can develop the farm on your Mac; there idle/fullscreen detection is
limited and it falls back to sensible defaults.
"""
from __future__ import annotations

import sys

try:
    import psutil
except ImportError:  # psutil is required on workers; keep import soft for dev
    psutil = None  # type: ignore

IS_WINDOWS = sys.platform.startswith("win")


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
    # Non-Windows dev fallback: pretend the machine is idle so jobs still run.
    return 10_000.0


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
    if not IS_WINDOWS:
        return False
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
