"""Harvest for Mac: a menu bar app (no Dock icon) around the same agent the
Windows app and the Linux service use.

Menu: status, power mode (Auto / Full power / Light), Pause, the dashboard,
connecting this Mac, start at login, games to pause for, and updates. The Mac's
GPU is reported to the dashboard (Metal), so tasks can ask for it; they get
HARVEST_DEVICE=mps for PyTorch.

Settings live in ~/Library/Application Support/Harvest/config.json (shared with
the command-line install, which this app replaces).
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

import requests
import rumps

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[1])]
from version import __version__  # noqa: E402  (desktop/version.py, stamped by CI)
from worker.agent import run_agent  # noqa: E402

APP_NAME = "Harvest"
REPO = "codevexx/harvest"
DEFAULT_SERVER = "https://harvest.codevexx.com"
HOME = Path(os.environ.get("HARVEST_HOME") or Path.home() / "Library" / "Application Support" / "Harvest")
CONFIG = HOME / "config.json"
AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGIN_PLIST = AGENTS / "com.codevexx.harvest.app.plist"
OLD_SERVICE = AGENTS / "com.codevexx.harvest.plist"  # from install.sh
FROZEN = getattr(sys, "frozen", False)
SAVED = ("coordinator", "token", "name", "idle_threshold", "games", "auto_update", "python")


def resource(name: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return str(base / name)


def bundle_path() -> Path | None:
    """/Applications/Harvest.app when running from the packaged app."""
    exe = Path(sys.executable).resolve()
    for p in exe.parents:
        if p.suffix == ".app":
            return p
    return None


def load_config() -> dict:
    cfg = {"coordinator": DEFAULT_SERVER, "token": "", "name": socket.gethostname().split(".")[0],
           "idle_threshold": 300, "games": [], "auto_update": True}
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    cfg["app_version"] = __version__
    return cfg


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        data = {}
    data.update({k: cfg[k] for k in SAVED if k in cfg})
    CONFIG.write_text(json.dumps(data, indent=2))
    CONFIG.chmod(0o600)


def newer(a: str, b: str) -> bool:
    key = lambda v: tuple(int(x) for x in v.lstrip("v").split(".")[:3] if x.isdigit())
    try:
        return key(a) > key(b)
    except ValueError:
        return False


class HarvestApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, icon=resource("menubar.png"), template=True, quit_button=None)
        self.cfg = load_config()
        self.mode, self.state, self.detail = "offline", "idle", "starting"
        self.farm = None
        self.stop_event = None
        self.thread = None
        self.update_note = None

        self.status_item = rumps.MenuItem("Starting")
        self.machine_item = rumps.MenuItem("")
        self.power = {k: rumps.MenuItem(label, callback=self.choose_power)
                      for k, label in (("auto", "Auto"), ("full", "Full power"), ("light", "Light"))}
        for k, item in self.power.items():
            item.power_key = k
        power_menu = rumps.MenuItem("Power")
        for item in self.power.values():
            power_menu.add(item)
        self.pause_item = rumps.MenuItem("Pause", callback=self.toggle_pause)
        self.connect_item = rumps.MenuItem("Connect this Mac…", callback=self.connect)
        self.login_item = rumps.MenuItem("Start at login", callback=self.toggle_login)
        self.update_item = rumps.MenuItem("Update automatically", callback=self.toggle_auto_update)
        settings = rumps.MenuItem("Settings")
        for item in (self.login_item, self.update_item,
                     rumps.MenuItem("Games to pause for…", callback=self.edit_games),
                     rumps.MenuItem("Rename this Mac…", callback=self.rename)):
            settings.add(item)
        self.check_item = rumps.MenuItem("Check for updates", callback=lambda _: self.check_updates(manual=True))
        self.menu = [self.status_item, self.machine_item, None, power_menu, self.pause_item, None,
                     rumps.MenuItem("Open dashboard", callback=self.open_dashboard), self.connect_item, settings,
                     self.check_item, None, rumps.MenuItem(f"Version {__version__}"),
                     rumps.MenuItem("Quit Harvest", callback=self.quit_app)]

        self.retire_old_service()
        if FROZEN and not LOGIN_PLIST.exists() and not (HOME / ".login-set").exists():
            self.set_login(True)  # on by default, once; the user can turn it off
            (HOME / ".login-set").write_text("1")
        self.start_agent()
        rumps.Timer(self.refresh, 1).start()
        threading.Thread(target=self.update_loop, daemon=True).start()

    # --- agent -------------------------------------------------------------

    def start_agent(self):
        if not self.cfg.get("token"):
            self.mode, self.state, self.detail = "offline", "idle", "not connected"
            return
        self.stop_event = threading.Event()

        def status(mode, state, detail):
            self.mode, self.state, self.detail = mode, state, detail

        def info(kw):
            if "farm" in kw:
                self.farm = kw["farm"]
            if kw.get("override") in ("full", "light"):
                self.cfg["last_power"] = kw["override"]

        def go(stop=self.stop_event):
            try:
                run_agent(self.cfg, stop, status, info)
            except Exception as e:
                self.mode, self.state, self.detail = "offline", "idle", f"error: {e}"

        self.thread = threading.Thread(target=go, daemon=True)
        self.thread.start()

    def restart_agent(self):
        if self.stop_event:
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=20)
        self.start_agent()

    # --- menu state ---------------------------------------------------------

    def power_choice(self) -> str:
        o = self.cfg.get("mode_override")
        if o in ("full", "light"):
            return o
        return self.cfg.get("last_power", "auto") if o == "paused" else "auto"

    def refresh(self, _=None):
        paused = self.cfg.get("mode_override") == "paused"
        if not self.cfg.get("token"):
            text = "Not connected"
        elif paused:
            text = "Paused by you"
        elif self.mode == "offline":
            text = "Connecting" if not (self.detail or "").startswith("error") else self.detail[:60]
        elif self.state == "busy":
            text = f"Running {self.detail or 'a task'}" + (" (light)" if self.mode == "light" else "")
        elif self.mode == "paused":
            text = "Paused for a game"
        else:
            text = "Ready: " + ("idle, full power" if self.mode == "full" else "you are using the Mac, light")
        self.status_item.title = text
        self.machine_item.title = f"{self.cfg.get('name')}" + (f" · {self.farm}" if self.farm else "")
        self.title = "●" if self.state == "busy" else None  # a dot next to the leaf while a task runs
        choice = self.power_choice()
        for k, item in self.power.items():
            item.state = 1 if k == choice else 0
        self.pause_item.title = "Resume" if paused else "Pause"
        self.connect_item.title = "Change connect token…" if self.cfg.get("token") else "Connect this Mac…"
        self.login_item.state = 1 if LOGIN_PLIST.exists() else 0
        self.update_item.state = 1 if self.cfg.get("auto_update", True) else 0
        self.check_item.title = self.update_note or "Check for updates"

    # --- actions ---------------------------------------------------------------

    def _set_override(self, value):
        # The agent reads cfg live and sends the change with its next check-in.
        self.cfg["mode_override"] = value
        self.cfg["override_dirty"] = True
        self.refresh()

    def choose_power(self, item):
        key = item.power_key
        self.cfg["last_power"] = key
        if self.cfg.get("mode_override") != "paused":
            self._set_override(None if key == "auto" else key)

    def toggle_pause(self, _):
        if self.cfg.get("mode_override") == "paused":
            last = self.cfg.get("last_power", "auto")
            self._set_override(None if last == "auto" else last)
        else:
            self._set_override("paused")

    def open_dashboard(self, _):
        webbrowser.open(self.cfg["coordinator"].rstrip("/") + "/app")

    def connect(self, _):
        w = rumps.Window("Paste the connect token from your dashboard (Machines > Connect a machine). It starts with hv_.",
                         "Connect this Mac", default_text="", ok="Connect", cancel="Cancel", dimensions=(320, 24))
        r = w.run()
        token = (r.text or "").strip()
        if not r.clicked or not token:
            return
        if not token.startswith("hv_"):
            rumps.alert("That is not a connect token", "Connect tokens start with hv_. Copy it again from the dashboard.")
            return
        self.cfg["token"] = token
        save_config(self.cfg)
        self.restart_agent()

    def rename(self, _):
        r = rumps.Window("The name this Mac shows on your dashboard.", "Rename this Mac",
                         default_text=self.cfg.get("name", ""), ok="Save", cancel="Cancel", dimensions=(320, 24)).run()
        name = (r.text or "").strip()
        if r.clicked and name:
            self.cfg["name"] = name[:60]
            save_config(self.cfg)
            self.restart_agent()

    def edit_games(self, _):
        r = rumps.Window("Harvest pauses tasks while these apps run. Use the app or process names, separated by commas "
                         "(for example: Minecraft, java, steam_osx).", "Games to pause for",
                         default_text=", ".join(self.cfg.get("games", [])), ok="Save", cancel="Cancel",
                         dimensions=(320, 48)).run()
        if r.clicked:
            self.cfg["games"] = [g.strip() for g in (r.text or "").split(",") if g.strip()]
            save_config(self.cfg)

    def toggle_auto_update(self, _):
        self.cfg["auto_update"] = not self.cfg.get("auto_update", True)
        save_config(self.cfg)

    def toggle_login(self, _):
        self.set_login(not LOGIN_PLIST.exists())

    def set_login(self, on: bool):
        app = bundle_path()
        if on and app:
            AGENTS.mkdir(parents=True, exist_ok=True)
            with open(LOGIN_PLIST, "wb") as f:
                plistlib.dump({"Label": "com.codevexx.harvest.app", "RunAtLoad": True,
                               "ProgramArguments": ["/usr/bin/open", "-g", "-a", str(app)],
                               "LimitLoadToSessionType": "Aqua"}, f)
        elif not on:
            LOGIN_PLIST.unlink(missing_ok=True)

    def retire_old_service(self):
        """The command-line install ran Harvest as a background service; this app
        replaces it, so turn that off (two copies would compete for tasks)."""
        if OLD_SERVICE.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.codevexx.harvest"],
                           capture_output=True)
            OLD_SERVICE.rename(OLD_SERVICE.with_suffix(".plist.disabled"))
            print("turned off the command-line Harvest service; this app takes over", flush=True)

    def quit_app(self, _):
        if self.stop_event:
            self.stop_event.set()  # a running task goes back in the queue
            if self.thread:
                self.thread.join(timeout=20)
        rumps.quit_application()

    # --- updates -------------------------------------------------------------------

    def update_loop(self):
        time.sleep(120)
        while True:
            if self.cfg.get("auto_update", True) and self.state != "busy":
                self.check_updates()
            time.sleep(6 * 3600)

    def check_updates(self, manual=False):
        try:
            r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=30)
            r.raise_for_status()
            tag = r.json().get("tag_name", "")
            if not newer(tag, __version__):
                self.update_note = "Harvest is up to date" if manual else None
                return
            app = bundle_path()
            if not app or not os.access(app.parent, os.W_OK):
                self.update_note = f"Version {tag.lstrip('v')} is available: download it from the website"
                return
            self.update_note = f"Updating to {tag.lstrip('v')}…"
            self.install_update(tag, app)
        except Exception as e:
            self.update_note = f"Update failed: {e}"[:70] if manual else None

    def install_update(self, tag: str, app: Path):
        base = f"https://github.com/{REPO}/releases/download/{tag}/Harvest-mac.zip"
        want = requests.get(base + ".sha256", timeout=30).text.split()[0].lower()
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / "Harvest-mac.zip"
            with requests.get(base, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(z, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            if hashlib.sha256(z.read_bytes()).hexdigest() != want:
                raise RuntimeError("the download did not match its published checksum")
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(z), tmp], check=True)
            new = Path(tmp) / "Harvest.app"
            if not (new / "Contents" / "MacOS").is_dir():
                raise RuntimeError("the download is not a Harvest app")
            old = app.with_name("Harvest.old.app")
            shutil.rmtree(old, ignore_errors=True)
            os.rename(app, old)
            shutil.move(str(new), str(app))
            shutil.rmtree(old, ignore_errors=True)
        if self.stop_event:
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=20)
        subprocess.Popen(["/usr/bin/open", "-n", str(app)])
        rumps.quit_application()


def main():
    HOME.mkdir(parents=True, exist_ok=True)
    if FROZEN:  # no terminal: keep a log
        log = open(HOME / "app.log", "a", buffering=1)
        sys.stdout = sys.stderr = log
        print(f"--- Harvest {__version__} started {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    # Apps opened from Finder get a minimal PATH; add the usual places for Python and Node.js.
    os.environ["PATH"] = ":".join(["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "/usr/bin:/bin")])
    HarvestApp().run()


if __name__ == "__main__":
    main()
