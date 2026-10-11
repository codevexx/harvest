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

import service
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
              "pause_on_fullscreen", "games", "auto_update", "python")

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
        self.service_on = service.installed()
        self.service_busy = False
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
            "service": self.service_on, "service_busy": self.service_busy,
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
        stop = self.stop_event

        def go():
            # If the background copy was working, let it hand its task back first
            # so the same task never runs twice on this PC.
            deadline = time.monotonic() + 60
            while winutil.mutex_exists(winutil.HEADLESS_AGENT) and time.monotonic() < deadline:
                if stop.wait(1):
                    return
            run_agent(self.cfg, stop, self._on_status, self._on_info)

        self.thread = threading.Thread(target=go, daemon=True)
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
            threading.Thread(target=service.restart, daemon=True).start()
        if "service" in data and IS_WIN and FROZEN and bool(data["service"]) != self.service_on:
            threading.Thread(target=self.toggle_service, args=(bool(data["service"]),), daemon=True).start()
        self.push()
        return None

    def toggle_service(self, enable: bool):
        """Register or remove the boot task. Windows shows a UAC prompt."""
        self.service_busy = True
        self.push()
        service.run_elevated(sys.executable, "--install-service" if enable else "--uninstall-service")
        self.service_on = service.installed()
        self.service_busy = False
        self.push()

    def install_website_support(self):
        """pip install playwright + its Chromium into the Python that runs tasks."""
        w = self.website
        w.update(installing=True, error=None, log="Installing Playwright")
        self.push()
        py = _python_exe()
        if not py:
            w.update(installing=False, error="Install Python 3.11+ first (python.org), then try again")
            self.push()
            return
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
        self._tray_mutex, _ = winutil.hold_mutex(winutil.TRAY_ACTIVE)  # background copy steps aside
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


def _log_to_file(name: str = "harvest.log"):
    """The packaged app has no console: keep output in %LOCALAPPDATA%\\Harvest."""
    if not FROZEN and sys.stdout is not None:
        return
    try:
        upd.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = upd.DATA_DIR / name
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix(".log.1"))
        sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        sys.stdout = sys.stderr = open(os.devnull, "w")
    print(f"--- Harvest {__version__} {' '.join(sys.argv[1:])} started {time.strftime('%Y-%m-%d %H:%M:%S')}")


def _arg(args: list[str], flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def connect_cli(args: list[str]) -> int:
    """Harvest.exe --connect hv_... [--name NAME] [--server URL]: save the token
    without opening any window (for PCs nobody signs in to)."""
    import requests
    cfg = load_config()
    token = (_arg(args, "--connect") or "").strip()
    if not token.startswith("hv_"):
        print("connect: that is not a connect token (it starts with hv_)")
        return 2
    server = (_arg(args, "--server") or cfg["coordinator"]).rstrip("/")
    try:
        r = requests.get(f"{server}/api/agent/job", params={"job_id": "-"},
                         headers={"authorization": f"Bearer {token}"}, timeout=20)
        if "invalid farm token" in r.text:
            print("connect: the server rejected this token. Copy a fresh one from the dashboard.")
            return 3
    except requests.RequestException as e:
        print(f"connect: could not reach {server} ({type(e).__name__}); saving anyway")
    cfg.update(token=token, coordinator=server, name=(_arg(args, "--name") or cfg["name"])[:60])
    save_config(cfg)
    service.restart()
    print(f"connect: saved. This PC is '{cfg['name']}' on {server}.")
    return 0


def install_service_cli() -> int:
    """Harvest.exe --install-service (run as admin): start at boot, before sign-in."""
    cfg = load_config()
    py = _python_exe()
    if py and os.path.isabs(py):
        cfg["python"] = py  # the boot task may not see the user's PATH
    save_config(cfg)
    err = service.install(sys.executable, str(CONFIG_PATH))
    print(f"install-service: {err or 'ok'}")
    return 1 if err else 0


def run_headless() -> int:
    """Background mode: started by Task Scheduler at boot, no window. Works while
    nobody is using the tray app, and steps aside whenever the tray app runs."""
    me, existed = winutil.hold_mutex(winutil.HEADLESS_RUNNING)
    if existed:
        return 0
    exe_mtime = os.path.getmtime(sys.executable) if FROZEN else None
    state = {"stop": None, "thread": None, "key": None, "mutex": None}
    tray_up = lambda: winutil.mutex_exists(winutil.TRAY_ACTIVE)

    def stop_agent():
        if state["stop"]:
            state["stop"].set()
            state["thread"].join(timeout=20)
        winutil.release(state["mutex"])
        state.update(stop=None, thread=None, mutex=None)
        print("background: stopped")

    def on_status(mode, st, detail, last={}):
        if (mode, st, detail) != last.get("v"):
            last["v"] = (mode, st, detail)
            print(f"background: {mode}/{st}" + (f" {detail}" if detail else ""))

    quit_flag = threading.Event()
    updater = upd.Updater(__version__, lambda: None, relaunch=False)
    threading.Thread(target=updater.run, daemon=True, args=(
        quit_flag, lambda: load_config().get("auto_update", True) and not tray_up(),
        lambda: state["thread"] is None or not state["thread"].is_alive(), quit_flag.set)).start()

    while not quit_flag.is_set():
        cfg = load_config()
        cfg["headless"] = True
        if cfg.get("python"):
            os.environ["HARVEST_PYTHON"] = cfg["python"]
        key = (cfg.get("token"), cfg.get("name"), cfg.get("coordinator"), tuple(cfg.get("games", [])))
        want = bool(cfg.get("token")) and not tray_up()
        if state["thread"] and (not want or key != state["key"]):
            stop_agent()
        if want and not state["thread"]:
            state["mutex"], _ = winutil.hold_mutex(winutil.HEADLESS_AGENT)
            state["stop"], state["key"] = threading.Event(), key
            state["thread"] = threading.Thread(target=run_agent, args=(cfg, state["stop"], on_status), daemon=True)
            state["thread"].start()
            print(f"background: working as '{cfg.get('name')}'")
        if exe_mtime and os.path.exists(sys.executable) and os.path.getmtime(sys.executable) != exe_mtime:
            quit_flag.set()  # a new version was installed
        quit_flag.wait(5)

    stop_agent()
    print("background: restarting for an update")
    service.queue_run()
    winutil.release(me)
    return 0


def main():
    global CONFIG_PATH, CONFIG_DIR
    args = sys.argv[1:]
    if _arg(args, "--config"):
        CONFIG_PATH = Path(_arg(args, "--config"))
        CONFIG_DIR = CONFIG_PATH.parent
    if "--headless" in args:
        _log_to_file("harvest-background.log")
        sys.exit(run_headless())
    _log_to_file()
    if "--connect" in args:
        sys.exit(connect_cli(args))
    if "--install-service" in args:
        sys.exit(install_service_cli())
    if "--uninstall-service" in args:
        err = service.uninstall()
        print(f"uninstall-service: {err or 'ok'}")
        sys.exit(1 if err else 0)
    if not winutil.acquire_single_instance(wait_s=20 if "--updated" in args else 0):
        winutil.signal_running_instance()  # already running: just open its flyout
        return
    threading.Thread(target=upd.cleanup_old, daemon=True).start()
    App(load_config()).run(show_first="--background" not in args)


if __name__ == "__main__":
    main()
