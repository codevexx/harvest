"""Harvest for Windows — a small system-tray app that connects this PC to your
Harvest account and runs tasks while the machine is idle.

It is a plain, visible app: a tray icon shows the status (idle / running /
paused / offline), a Settings window lets you paste your connect token, and Quit
stops it. It only does work after you sign in on the website and paste the token
for a farm you own. Nothing runs hidden and nothing persists after you quit.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

# Make the sibling `worker` package importable when run from source.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pystray
from PIL import Image, ImageDraw

from worker.agent import run_agent

APP_NAME = "Harvest"
DEFAULT_COORDINATOR = "https://harvest.codevexx.com"

CONFIG_DIR = Path(os.environ.get("APPDATA") or os.path.expanduser("~/.config")) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"

COLORS = {  # status -> dot color
    "full": (116, 194, 116), "light": (227, 178, 60), "busy": (240, 165, 59),
    "paused": (235, 111, 88), "offline": (120, 120, 120),
}


# --- config --------------------------------------------------------------

def load_config() -> dict:
    cfg = {
        "coordinator": DEFAULT_COORDINATOR,
        "token": "",
        "name": socket.gethostname(),
        "poll_interval": 3,
        "idle_threshold": 300,
        "pause_on_fullscreen": True,
        "games": [],
    }
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --- settings window (Tkinter) ------------------------------------------

def show_settings(cfg: dict) -> dict | None:
    """Modal settings dialog. Returns the updated config, or None if cancelled."""
    import tkinter as tk
    from tkinter import ttk

    result = {}
    root = tk.Tk()
    root.title(f"{APP_NAME} — settings")
    root.resizable(False, False)
    try:
        root.configure(bg="#15130f")
    except Exception:
        pass

    frm = ttk.Frame(root, padding=16)
    frm.grid()
    rows = [
        ("Connect token", "token", cfg.get("token", "")),
        ("Server", "coordinator", cfg.get("coordinator", DEFAULT_COORDINATOR)),
        ("Machine name", "name", cfg.get("name", socket.gethostname())),
        ("Pause for games (comma-separated .exe)", "games", ", ".join(cfg.get("games", []))),
    ]
    vars_ = {}
    ttk.Label(frm, text="Paste the token from the Harvest dashboard\n(Connect a machine). Shown once.").grid(column=0, row=0, columnspan=2, sticky="w", pady=(0, 10))
    for i, (label, key, val) in enumerate(rows, start=1):
        ttk.Label(frm, text=label).grid(column=0, row=i, sticky="w", pady=4, padx=(0, 10))
        show = "*" if key == "token" else None
        e = ttk.Entry(frm, width=46, show=show)
        e.insert(0, val)
        e.grid(column=1, row=i, pady=4)
        vars_[key] = e

    def on_save():
        cfg["token"] = vars_["token"].get().strip()
        cfg["coordinator"] = vars_["coordinator"].get().strip() or DEFAULT_COORDINATOR
        cfg["name"] = vars_["name"].get().strip() or socket.gethostname()
        cfg["games"] = [g.strip() for g in vars_["games"].get().split(",") if g.strip()]
        save_config(cfg)
        result["cfg"] = cfg
        root.destroy()

    def on_cancel():
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=len(rows) + 1, columnspan=2, sticky="e", pady=(14, 0))
    ttk.Button(btns, text="Cancel", command=on_cancel).grid(column=0, row=0, padx=6)
    ttk.Button(btns, text="Save", command=on_save).grid(column=1, row=0)

    root.update_idletasks()
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return result.get("cfg")


# --- tray app ------------------------------------------------------------

class TrayApp:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = "offline"
        self.state = "idle"
        self.detail = None
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.icon = pystray.Icon(APP_NAME, self._image(), self._title(), self._menu())

    # status image: a dark rounded square with a status-colored dot
    def _image(self) -> Image.Image:
        color = COLORS.get("busy" if self.state == "busy" else self.mode, COLORS["offline"])
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(21, 19, 15, 255))
        d.ellipse([20, 20, 44, 44], fill=color + (255,))
        return img

    def _title(self) -> str:
        label = {"full": "idle, full power", "light": "in use, light",
                 "busy": f"running {self.detail or ''}".strip(),
                 "paused": "paused (in use/gaming)", "offline": "connecting…"}
        s = "busy" if self.state == "busy" else self.mode
        return f"{APP_NAME} — {label.get(s, s)}"

    def _menu(self):
        from pystray import MenuItem as Item, Menu
        return Menu(
            Item(lambda _: self._title().replace(APP_NAME + " — ", "Status: "), None, enabled=False),
            Menu.SEPARATOR,
            Item("Open dashboard", self._open_dashboard),
            Item("Settings…", self._settings),
            Menu.SEPARATOR,
            Item("Quit", self._quit),
        )

    def _refresh(self):
        try:
            self.icon.icon = self._image()
            self.icon.title = self._title()
            self.icon.update_menu()
        except Exception:
            pass

    def _on_status(self, mode, state, detail):
        self.mode, self.state, self.detail = mode, state, detail
        self._refresh()

    def _start_agent(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=run_agent, args=(self.cfg, self.stop_event, self._on_status), daemon=True)
        self.thread.start()

    def _stop_agent(self):
        if self.stop_event:
            self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)

    def _open_dashboard(self, _=None):
        import webbrowser
        webbrowser.open(self.cfg["coordinator"].rstrip("/") + "/app")

    def _settings(self, _=None):
        new_cfg = show_settings(dict(self.cfg))
        if new_cfg:
            self.cfg = new_cfg
            self._stop_agent()
            self._start_agent()

    def _quit(self, _=None):
        self._stop_agent()
        self.icon.stop()

    def run(self):
        self._start_agent()
        self.icon.run()


def main():
    cfg = load_config()
    if not cfg.get("token"):
        cfg = show_settings(cfg)
        if not cfg or not cfg.get("token"):
            return  # user cancelled without entering a token
    TrayApp(cfg).run()


if __name__ == "__main__":
    main()
