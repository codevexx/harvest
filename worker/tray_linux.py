"""Harvest's tray icon for Linux desktops.

A remote control for the Harvest background service (worker/daemon.py, a systemd
user service): it shows what the service is doing (from <HARVEST_HOME>/status.json)
and sets the power mode or pauses by editing config.json, which the service picks
up within seconds. Quitting the icon never stops the work.

install.sh sets it up on desktops (autostart at login). Needs pystray and Pillow;
on GNOME it uses AppIndicator (the system's python3-gi), elsewhere the X11 tray.

    python -m worker.tray_linux
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .daemon import home, load_config, version

STALE_S = 90  # no status update for this long: the service is not running
AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "harvest-tray.desktop"
DOT = {"busy": (66, 133, 244), "full": (52, 168, 83), "light": (251, 188, 4), "paused": (234, 67, 53),
       "held": (234, 67, 53), "offline": (154, 160, 166)}


def read_status() -> dict:
    try:
        s = json.loads((home() / "status.json").read_text())
    except (OSError, ValueError):
        return {"running": False}
    s["running"] = time.time() - float(s.get("ts") or 0) < STALE_S
    return s


def read_config() -> dict:
    try:
        return json.loads((home() / "config.json").read_text())
    except (OSError, ValueError):
        return {}


def write_override(value: str | None, last_power: str | None = None) -> None:
    """Ask the service to change the power mode (it sends it to the server with its next check-in)."""
    cfg = read_config()
    cfg["mode_override"] = value
    cfg["override_dirty"] = True
    if last_power:
        cfg["last_power"] = last_power
    tmp = home() / "config.json.tmp"
    tmp.write_text(json.dumps(cfg, indent=1))
    os.replace(tmp, home() / "config.json")


def power_choice(override, last_power) -> str:
    if override in ("full", "light"):
        return override
    return (last_power or "auto") if override == "paused" else "auto"


def status_text(s: dict) -> str:
    if not s.get("running"):
        return "Harvest is not running"
    o, mode, st = s.get("override"), s.get("mode"), s.get("state")
    if o == "paused" or mode == "held":
        return "Paused by you"
    if mode == "offline":
        return "Connecting"
    if st == "busy":
        return f"Running {s.get('detail') or 'a task'}" + (" (light)" if mode == "light" else "")
    if mode == "paused":
        return "Paused for a game"
    return "Ready: " + ("idle, full power" if mode == "full" else "you are using this PC, light")


def state_key(s: dict) -> str:
    if not s.get("running"):
        return "offline"
    if s.get("override") == "paused":
        return "held"
    return "busy" if s.get("state") == "busy" else (s.get("mode") or "offline")


def systemctl(*args) -> bool:
    if not shutil.which("systemctl"):
        return False
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20).returncode == 0


def autostart_entry() -> str:
    app = home() / "app"
    py = home() / "venv" / "bin" / "python"
    return ("[Desktop Entry]\nType=Application\nName=Harvest\nComment=Harvest status and controls\n"
            f"Exec=sh -c 'cd \"{app}\" && HARVEST_HOME=\"{home()}\" exec \"{py}\" -m worker.tray_linux'\n"
            "Icon=applications-system\nX-GNOME-Autostart-enabled=true\nNoDisplay=false\n")


def set_autostart(on: bool) -> None:
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(autostart_entry())
    else:
        AUTOSTART.unlink(missing_ok=True)


def icon_image(key: str, size: int = 64):
    from PIL import ImageDraw
    try:
        from .icon_art import tray_leaf
        img = tray_leaf(size)
    except Exception:  # the icon art is not shipped: a plain leaf-coloured disc
        from PIL import Image
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([6, 6, size - 6, size - 6], fill=(64, 172, 98, 255))
    d = ImageDraw.Draw(img)
    r = size // 5
    d.ellipse([size - 2 * r - 1, size - 2 * r - 1, size - 1, size - 1], fill=DOT.get(key, DOT["offline"]) + (255,),
              outline=(20, 20, 20, 255), width=max(1, size // 32))
    return img


def main() -> None:
    try:
        import pystray
    except ImportError:
        sys.exit("The tray icon needs pystray and Pillow: run install.sh again, or pip install pystray Pillow.")
    server = (load_config().get("coordinator") or "https://harvest.codevexx.com").rstrip("/")
    s = {"value": read_status()}
    Menu, Item = pystray.Menu, pystray.MenuItem

    def cur():
        return s["value"]

    def choose(key):
        def act(icon, item):
            cfg = read_config()
            if cfg.get("mode_override") == "paused":  # stays paused; remembered for Resume
                write_override("paused", key)
            else:
                write_override(None if key == "auto" else key, key)
            refresh(icon)
        return act

    def checked(key):
        return lambda item: power_choice(cur().get("override"), read_config().get("last_power")) == key

    def toggle_pause(icon, item):
        cfg = read_config()
        if cfg.get("mode_override") == "paused":
            last = cfg.get("last_power", "auto")
            write_override(None if last == "auto" else last)
        else:
            write_override("paused")
        refresh(icon)

    def restart(icon, item):
        if not systemctl("restart", "harvest.service"):
            print("could not restart the service (is systemd's user session running?)", flush=True)

    menu = Menu(
        Item(lambda item: status_text(cur()), None, enabled=False),
        Item(lambda item: (cur().get("name") or "this PC") + (f" · {cur()['farm']}" if cur().get("farm") else ""), None, enabled=False),
        Menu.SEPARATOR,
        Item("Power", Menu(Item("Auto", choose("auto"), checked=checked("auto"), radio=True),
                           Item("Full power", choose("full"), checked=checked("full"), radio=True),
                           Item("Light", choose("light"), checked=checked("light"), radio=True))),
        Item(lambda item: "Resume" if cur().get("override") == "paused" else "Pause", toggle_pause),
        Item("Open dashboard", lambda icon, item: webbrowser.open(server + "/app")),
        Menu.SEPARATOR,
        Item("Show at login", lambda icon, item: (set_autostart(not AUTOSTART.exists()), icon.update_menu()),
             checked=lambda item: AUTOSTART.exists()),
        Item("Restart Harvest", restart),
        Item(lambda item: f"Version {version()}", None, enabled=False),
        Item("Hide this icon (Harvest keeps working)", lambda icon, item: icon.stop()),
    )
    icon = pystray.Icon("harvest", icon_image(state_key(s["value"])), "Harvest", menu)

    def refresh(ic=icon):
        s["value"] = read_status()
        ic.icon = icon_image(state_key(s["value"]))
        ic.title = "Harvest: " + status_text(s["value"])
        try:
            ic.update_menu()
        except Exception:
            pass

    def loop():
        while True:
            time.sleep(3)
            refresh()

    def setup(ic):
        ic.visible = True
        threading.Thread(target=loop, daemon=True).start()

    icon.run(setup=setup)


if __name__ == "__main__":
    main()
