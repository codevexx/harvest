"""Small Win32 helpers for the tray app: flyout placement, single instance,
start-with-Windows. Every function is a safe no-op on other systems."""
from __future__ import annotations

import sys
import threading

IS_WIN = sys.platform.startswith("win")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SHOW_EVENT = "Local\\HarvestShowFlyout"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    user32.FindWindowW.restype = wintypes.HWND
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    for fn in ("CreateMutexW", "CreateEventW", "OpenEventW", "OpenMutexW"):
        getattr(kernel32, fn).restype = wintypes.HANDLE

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def find_window(title: str):
    return user32.FindWindowW(None, title) if IS_WIN else None


def style_flyout(hwnd) -> None:
    """Keep the flyout out of the taskbar and Alt+Tab, with Windows 11 rounded corners."""
    if not (IS_WIN and hwnd):
        return
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))


def place_bottom_right(hwnd, margin_dip: int = 12) -> None:
    """Move the window to the bottom-right of the work area (above the taskbar)
    on the monitor under the mouse, like the Windows tray flyouts."""
    if not (IS_WIN and hwnd):
        return
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    mon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW(mon, ctypes.byref(info))
    rc = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    try:
        scale = user32.GetDpiForWindow(hwnd) / 96
    except Exception:
        scale = 1.0
    m = int(margin_dip * scale)
    work = info.rcWork
    x, y = work.right - w - m, work.bottom - h - m
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
    user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


def focus(hwnd) -> None:
    if IS_WIN and hwnd:
        user32.SetForegroundWindow(hwnd)


# --- named mutexes (tray app <-> background mode handoff) -----------------

TRAY_ACTIVE = "Global\\HarvestTrayActive"        # held while the tray app runs
HEADLESS_RUNNING = "Global\\HarvestHeadless"     # one background copy at a time
HEADLESS_AGENT = "Global\\HarvestHeadlessAgent"  # held while background mode is working


def hold_mutex(name: str):
    """Create and keep a named mutex. Returns (handle, already_existed)."""
    if not IS_WIN:
        return None, False
    h = kernel32.CreateMutexW(None, False, name)
    return h, ctypes.get_last_error() == 183


def release(handle) -> None:
    if IS_WIN and handle:
        kernel32.CloseHandle(handle)


def mutex_exists(name: str) -> bool:
    if not IS_WIN:
        return False
    h = kernel32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


# --- single instance -----------------------------------------------------

_mutex = None


def acquire_single_instance(wait_s: float = 0) -> bool:
    """True if this is the only running copy. wait_s lets an updated build wait
    for the old process to exit."""
    global _mutex
    if not IS_WIN:
        return True
    import time
    deadline = time.monotonic() + wait_s
    while True:
        _mutex = kernel32.CreateMutexW(None, False, "Local\\HarvestTrayApp")
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return True
        kernel32.CloseHandle(_mutex)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def signal_running_instance() -> None:
    """Ask the copy that is already running to open its flyout."""
    if not IS_WIN:
        return
    ev = kernel32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
    if ev:
        kernel32.SetEvent(ev)
        kernel32.CloseHandle(ev)


def listen_for_show(callback) -> None:
    if not IS_WIN:
        return
    ev = kernel32.CreateEventW(None, False, False, SHOW_EVENT)

    def loop():
        while True:
            if kernel32.WaitForSingleObject(ev, 0xFFFFFFFF) == 0:
                callback()

    threading.Thread(target=loop, daemon=True).start()


# --- start with Windows --------------------------------------------------

def get_autostart() -> bool:
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "Harvest")
            return True
    except OSError:
        return False


def set_autostart(enabled: bool, exe: str) -> None:
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, "Harvest", 0, winreg.REG_SZ, f'"{exe}" --background')
        else:
            try:
                winreg.DeleteValue(k, "Harvest")
            except OSError:
                pass


def is_dark() -> bool:
    """Windows app theme is dark (so the flyout does not flash white)."""
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False
