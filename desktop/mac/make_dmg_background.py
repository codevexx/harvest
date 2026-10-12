"""The background of the Harvest-mac.dmg window: a title, a hint and an arrow from
the app to the Applications folder. Writes dmg_background.png and @2x (combined
into a Retina TIFF by the build)."""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
W, H = 660, 420
APP, APPS = (180, 220), (480, 220)  # icon centers (must match dmg_settings.py)


def font(size, bold=False):
    for path in [
            "/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Helvetica.ttc"]:
        try:
            return ImageFont.truetype(path, size, index=1 if bold and path.endswith(".ttc") else 0)
        except OSError:
            continue
    return ImageFont.load_default(size)


def render(scale):
    w, h = W * scale, H * scale
    img = Image.new("RGB", (w, h))
    top, bottom = (250, 251, 254), (236, 240, 247)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    # Soft amber glow behind the app icon.
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse([(APP[0] - 110) * scale, (APP[1] - 110) * scale, (APP[0] + 110) * scale, (APP[1] + 110) * scale], fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(40 * scale))
    img.paste((255, 205, 120), (0, 0), glow)
    d = ImageDraw.Draw(img)
    title, hint = "Install Harvest", "Drag Harvest into your Applications folder"
    f1, f2 = font(26 * scale, bold=True), font(15 * scale)
    d.text((w / 2, 52 * scale), title, font=f1, fill=(31, 31, 31), anchor="mm")
    d.text((w / 2, 86 * scale), hint, font=f2, fill=(90, 94, 102), anchor="mm")
    # Arrow from the app to Applications.
    x0, x1, y = (APP[0] + 80) * scale, (APPS[0] - 80) * scale, APP[1] * scale
    col = (11, 87, 208)
    d.line([(x0, y), (x1 - 10 * scale, y)], fill=col, width=4 * scale)
    d.polygon([(x1, y), (x1 - 18 * scale, y - 11 * scale), (x1 - 18 * scale, y + 11 * scale)], fill=col)
    d.text((w / 2, (H - 36) * scale), "Harvest runs in your menu bar. Click the leaf to connect this Mac.",
           font=font(12 * scale), fill=(120, 124, 132), anchor="mm")
    return img


if __name__ == "__main__":
    render(1).save(HERE / "dmg_background.png")
    render(2).save(HERE / "dmg_background@2x.png")
    print("wrote dmg_background.png and @2x")
