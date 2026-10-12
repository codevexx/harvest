"""Icons for the Mac app, from icon_art: menubar.png (a template image macOS tints
for light and dark menu bars) and Harvest.icns (the app icon)."""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from icon_art import app_icon, menubar  # noqa: E402


def icns():
    iconset = HERE / "Harvest.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    big = app_icon(1024, "mac")
    for size in (16, 32, 128, 256, 512):
        big.resize((size, size), resample=3).save(iconset / f"icon_{size}x{size}.png")
        big.resize((size * 2, size * 2), resample=3).save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "Harvest.icns")], check=True)
    shutil.rmtree(iconset)


if __name__ == "__main__":
    menubar(44).save(HERE / "menubar.png")
    icns()
    print("wrote menubar.png and Harvest.icns")
