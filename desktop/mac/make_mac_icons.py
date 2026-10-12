"""Icons for the Mac app: menubar.png (a template image macOS tints for light and
dark menu bars) and Harvest.icns (the app icon, from the same leaf as Windows)."""
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from make_icon import render  # noqa: E402


def menubar():
    s = 4  # draw big, then shrink for smooth edges
    img = Image.new("RGBA", (44 * s, 44 * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(22 * s, 3 * s), (38 * s, 24 * s), (22 * s, 41 * s), (6 * s, 24 * s)], fill=(0, 0, 0, 255))
    d.ellipse([8 * s, 14 * s, 36 * s, 41 * s], fill=(0, 0, 0, 255))
    d.line([(22 * s, 16 * s), (22 * s, 38 * s)], fill=(0, 0, 0, 0), width=3 * s)  # the leaf's vein
    img.resize((44, 44), Image.LANCZOS).save(HERE / "menubar.png")


def icns():
    iconset = HERE / "Harvest.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        render(size).save(iconset / f"icon_{size}x{size}.png")
        render(size * 2).save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "Harvest.icns")], check=True)
    shutil.rmtree(iconset)


if __name__ == "__main__":
    menubar()
    icns()
    print("wrote menubar.png and Harvest.icns")
