"""Harvest for Windows: a system-tray app that connects this PC to your Harvest
account and runs tasks while the machine is idle.

Click the tray icon and a flyout opens above the clock (like the Windows volume
or Wi-Fi panels) with the live status, Pause, the dashboard link and settings.
It is a plain, visible app: nothing runs hidden, and Quit stops everything.
It keeps itself up to date from GitHub Releases (see updater.py).

On macOS/Linux this opens as a normal window, which is handy for development;
the Linux tray version comes later.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

# Make the sibling `worker` package importable when run from source.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview
from PIL import Image, ImageDraw

import updater as upd
import winutil
from version import __version__
from worker.agent import run_agent
from worker.runner import NO_WINDOW, _python_exe

APP_NAME = "Harvest"
TITLE = "Harvest"
DEFAULT_COORDINATOR = "https://harvest.codevexx.com"
FLYOUT_W, FLYOUT_H = 380, 470
IS_WIN = winutil.IS_WIN
FROZEN = getattr(sys, "frozen", False)

CONFIG_DIR = Path(os.environ.get("APPDATA") or os.path.expanduser("~/.config")) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
SAVED_KEYS = ("coordinator", "token", "name", "poll_interval", "idle_threshold",
              "pause_on_fullscreen", "games", "auto_update")

DOT = {  # status -> badge color on the tray icon
    "full": (52, 168, 83), "light": (251, 188, 4), "busy": (66, 133, 244),
    "paused": (234, 67, 53), "held": (251, 188, 4), "offline": (128, 134, 139),
}


# --- config --------------------------------------------------------------

def load_config() -> dict:
    cfg = {
        "coordinator": DEFAULT_COORDINATOR, "token": "", "name": socket.gethostname(),
        "poll_interval": 3, "idle_threshold": 300, "pause_on_fullscreen": True,
        "games": [], "auto_update": True,
    }
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    cfg["user_paused"] = False  # a pause never survives a restart
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {k: cfg[k] for k in SAVED_KEYS if k in cfg}
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def ui_html() -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return (base / "ui" / "flyout.html").read_text(encoding="utf-8")


# --- tray icon image -----------------------------------------------------

def tray_image(status: str) -> Image.Image:
    """Amber leaf with a small status badge, drawn at 64px."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(32, 4), (54, 34), (32, 60), (10, 34)], fill=(240, 165, 59, 255))
    d.ellipse([12, 20, 52, 60], fill=(240, 165, 59, 255))
    color = DOT.get(status, DOT["offline"])
    d.ellipse([36, 36, 63, 63], fill=(255, 255, 255, 255))
    d.ellipse([40, 40, 59, 59], fill=color + (255,))
    return img


# --- the bridge the flyout's JavaScript calls ----------------------------

class Api:
    """Methods here are callable from the page as pywebview.api.<name>().
    Attributes start with _ so pywebview does not expose them."""

    def __init__(self, app: "App"):
        self._app = app

    def get_state(self):
        return self._app.state_dict()

    def save_settings(self, data):
        return self._app.save_settings(data or {})

    def set_paused(self, paused):
        self._app.set_paused(bool(paused))

    def open_dashboard(self):
        webbrowser.open(self._app.cfg["coordinator"].rstrip("/") + "/app")
        self._app.hide_flyout()

    def open_download(self):
        webbrowser.open(self._app.cfg["coordinator"].rstrip("/") + "/download")

    def check_updates(self):
        threading.Thread(target=self._app.updater.check, kwargs={"manual": True}, daemon=True).start()

    def download_update(self):
        threading.Thread(target=self._app.updater.download, daemon=True).start()

    def apply_update(self):
        if self._app.updater.apply():
            self._app.quit()

    def setup_website_tasks(self):
        threading.Thread(target=self._app.install_website_support, daemon=True).start()

    def hide(self):
        self._app.hide_flyout()

    def on_blur(self):
        if IS_WIN:  # a flyout closes when you click elsewhere
            self._app.hide_flyout()

    def quit(self):
        threading.Thread(target=self._app.quit, daemon=True).start()


# --- app -----------------------------------------------------------------

class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode, self.state, self.detail = "offline", "idle", None
        self.farm = None
        self.runtimes: list[str] = []
        self.finished = 0
        self.website = {"installing": False, "log": None, "error": None}
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.quitting = threading.Event()
        self.loaded = False
        self.visible = False
        self.last_hide = 0.0
        self.styled = False
        self.updater = upd.Updater(__version__, self.push)
        self.icon = None
        self.window = webview.create_window(
            TITLE, html=ui_html(), js_api=Api(self),
            width=FLYOUT_W, height=FLYOUT_H, resizable=False,
            frameless=IS_WIN, easy_drag=False, on_top=IS_WIN, hidden=IS_WIN,
            background_color="#131314" if winutil.is_dark() else "#f8fafd",
        )
        self.window.events.loaded += self._on_loaded

    # --- state for the UI ---

    def state_dict(self) -> dict:
        c = self.cfg
        return {
            "version": __version__, "connected": bool(c.get("token")),
            "name": c.get("name"), "farm": self.farm, "server": c.get("coordinator"),
            "server_host": urlparse(c.get("coordinator", "")).netloc,
            "games": c.get("games", []), "idle_threshold": c.get("idle_threshold", 300),
            "auto_update": c.get("auto_update", True),
            "autostart": winutil.get_autostart(), "can_autostart": IS_WIN and FROZEN,
            "status": {"mode": self.mode, "state": self.state, "detail": self.detail},
            "user_paused": c.get("user_paused", False), "finished": self.finished,
            "runtimes": self.runtimes, "update": self.updater.snapshot(),
            "website": dict(self.website, ready="browser" in self.runtimes),
        }

    def push(self) -> None:
        """Send the latest state to the flyout and refresh the tray icon."""
        if self.icon is not None:
            try:
                key = "busy" if self.state == "busy" else ("held" if self.cfg.get("user_paused") else self.mode)
                self.icon.icon = tray_image(key)
                self.icon.title = f"{APP_NAME}: " + {
                    "busy": f"running {self.detail or 'a task'}", "full": "ready, idle",
                    "light": "ready, you are using the PC", "paused": "paused for a game",
                    "held": "paused by you", "offline": "connecting"}.get(key, key)
                self.icon.update_menu()
            except Exception:
                pass
        if self.loaded:
            try:
                self.window.evaluate_js("hv.update(%s)" % json.dumps(self.state_dict()))
            except Exception:
                pass

    def _on_loaded(self):
        self.loaded = True
        self.push()

    # --- agent ---

    def _on_status(self, mode, state, detail):
        if self.state == "busy" and state != "busy":
            self.finished += 1
        self.mode, self.state, self.detail = mode, state, detail
        self.push()

    def _on_info(self, info):
        self.farm = info.get("farm")
        self.runtimes = info.get("runtimes") or []
        self.push()

    def start_agent(self):
        if not self.cfg.get("token"):
            return
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=run_agent, args=(self.cfg, self.stop_event, self._on_status, self._on_info),
            daemon=True)
        self.thread.start()

    def stop_agent(self):
        if self.stop_event:
            self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        self.mode, self.state, self.detail = "offline", "idle", None

    def restart_agent(self):
        def go():
            self.stop_agent()
            self.start_agent()
            self.push()
        threading.Thread(target=go, daemon=True).start()

    # --- actions ---

    def set_paused(self, paused: bool):
        self.cfg["user_paused"] = paused  # the agent reads this live via idle.get_mode
        self.push()

    def save_settings(self, data: dict) -> str | None:
        c = self.cfg
        token = (data.get("token") or "").strip()
        if token and not token.startswith("hv_"):
            return "That does not look like a connect token. It starts with hv_"
        name = (data.get("name") or "").strip() or c.get("name") or socket.gethostname()
        server = (data.get("server") or c.get("coordinator") or DEFAULT_COORDINATOR).strip().rstrip("/")
        if not server.startswith(("https://", "http://")):
            return "Server must start with https://"
        restart = (token and token != c.get("token")) or name != c.get("name") or server != c.get("coordinator")
        if token:
            c["token"] = token
        c["name"], c["coordinator"] = name[:60], server
        if "games" in data:
            c["games"] = [g for g in data["games"] if g][:50]
        if "auto_update" in data:
            c["auto_update"] = bool(data["auto_update"])
        if "idle_minutes" in data:
            c["idle_threshold"] = max(1, min(240, int(data["idle_minutes"]))) * 60
        if "autostart" in data and IS_WIN and FROZEN:
            try:
                winutil.set_autostart(bool(data["autostart"]), sys.executable)
            except OSError as e:
                return f"Could not change start with Windows: {e}"
        save_config(c)
        if restart:
            self.restart_agent()
        self.push()
        return None

    def install_website_support(self):
        """pip install playwright + its Chromium into the Python that runs tasks."""
        w = self.website
        w.update(installing=True, error=None, log="Installing Playwright")
        self.push()
        py = _python_exe()
        steps = [([py, "-m", "pip", "install", "--upgrade", "playwright"], "Installing Playwright"),
                 ([py, "-m", "playwright", "install", "chromium"], "Downloading Chrome")]
        try:
            for cmd, label in steps:
                w["log"] = label
                self.push()
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, creationflags=NO_WINDOW)
                last = 0.0
                for line in p.stdout:
                    if time.monotonic() - last > 1 and line.strip():
                        w["log"] = f"{label}: {line.strip()[:60]}"
                        last = time.monotonic()
                        self.push()
                if p.wait() != 0:
                    raise RuntimeError(f"{label} failed")
            w.update(installing=False, log=None)
            self.restart_agent()  # re-detect what this PC can run
        except FileNotFoundError:
            w.update(installing=False, error="Install Python 3.11+ first (python.org), then try again")
        except Exception as e:
            w.update(installing=False, error=str(e))
        self.push()

    # --- flyout ---

    def show_flyout(self):
        if time.monotonic() - self.last_hide < 0.4:
            return  # this click is the one that just closed it
        if IS_WIN:
            hwnd = self._hwnd()
            if not self.styled:
                winutil.style_flyout(hwnd)
                self.styled = True
            winutil.place_bottom_right(hwnd)
        self.push()
        self.visible = True
        self.window.show()
        if IS_WIN:
            winutil.focus(self._hwnd())

    def hide_flyout(self):
        if not IS_WIN or not self.visible:
            return
        self.visible = False
        self.last_hide = time.monotonic()
        self.window.hide()

    def toggle_flyout(self, *_):
        if self.visible:
            self.hide_flyout()
        else:
            self.show_flyout()

    def _hwnd(self):
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return winutil.find_window(TITLE)

    # --- lifecycle ---

    def _menu(self):
        import pystray
        Item = pystray.MenuItem
        return pystray.Menu(
            Item("Open Harvest", self.toggle_flyout, default=True),
            Item("Open dashboard", lambda *_: webbrowser.open(self.cfg["coordinator"].rstrip("/") + "/app")),
            Item(lambda _: "Resume" if self.cfg.get("user_paused") else "Pause",
                 lambda *_: self.set_paused(not self.cfg.get("user_paused"))),
            pystray.Menu.SEPARATOR,
            Item("Quit", lambda *_: threading.Thread(target=self.quit, daemon=True).start()),
        )

    def quit(self):
        if self.quitting.is_set():
            return
        self.quitting.set()
        self.stop_agent()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        try:
            self.window.destroy()
        except Exception:
            pass

    def run(self, show_first: bool):
        if IS_WIN:
            import pystray
            self.icon = pystray.Icon(APP_NAME, tray_image("offline"), APP_NAME, self._menu())
            threading.Thread(target=self.icon.run, daemon=True).start()
            winutil.listen_for_show(self.show_flyout)
        self.start_agent()
        threading.Thread(
            target=self.updater.run,
            args=(self.quitting, lambda: self.cfg.get("auto_update", True),
                  lambda: self.state != "busy", self.quit),
            daemon=True).start()

        def first_show():
            if show_first or not self.cfg.get("token"):
                time.sleep(0.3)
                self.show_flyout()
        if IS_WIN:
            self.window.events.loaded += lambda: threading.Thread(target=first_show, daemon=True).start()
        webview.start()
        self.quit()


def _log_to_file():
    """A windowed build has no console: keep output in %LOCALAPPDATA%\\Harvest\\harvest.log."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        upd.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = upd.DATA_DIR / "harvest.log"
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix(".log.1"))
        sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)
        print(f"--- Harvest {__version__} started {time.strftime('%Y-%m-%d %H:%M:%S')}")
    except OSError:
        sys.stdout = sys.stderr = open(os.devnull, "w")


def main():
    _log_to_file()
    args = sys.argv[1:]
    if not winutil.acquire_single_instance(wait_s=20 if "--updated" in args else 0):
        winutil.signal_running_instance()  # already running: just open its flyout
        return
    threading.Thread(target=upd.cleanup_old, daemon=True).start()
    App(load_config()).run(show_first="--background" not in args)


if __name__ == "__main__":
    main()
