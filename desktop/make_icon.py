"""Generate the Windows art from icon_art: harvest.ico (app and installer icon) and
the setup wizard's images (small logo, top right; side panel on the last page),
in every size Inno Setup picks from for the screen's scaling."""
from pathlib import Path

from PIL import Image

from icon_art import BG_BOTTOM, BG_TOP, _gradient, _leaf, app_icon

HERE = Path(__file__).parent
SMALL = [(55, 58), (64, 68), (83, 80), (92, 97), (110, 106), (119, 123), (138, 140)]
SIDE = [(164, 314), (192, 386), (246, 459), (273, 556), (328, 604), (355, 700), (410, 797)]


def small_image(w, h):
    img = Image.new("RGB", (w, h), (255, 255, 255))
    s = min(w, h) - 4
    icon = app_icon(s, "win")
    img.paste(icon, ((w - s) // 2, (h - s) // 2), icon)
    return img


def side_image(w, h):
    size = max(w, h)
    img = _gradient(size, BG_TOP, BG_BOTTOM, 90).crop((0, 0, w, h)).convert("RGBA")
    S = w * 4
    leaf = _leaf(S, S * 0.5, S * 0.5, S * 0.62, S * 0.36, 40).resize((w, w), Image.LANCZOS)
    img.alpha_composite(leaf, (0, int(h * 0.28)))
    return img.convert("RGB")


def main():
    app_icon(256, "win").save(HERE / "harvest.ico",
                              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    for i, (w, h) in enumerate(SMALL):
        small_image(w, h).save(HERE / f"wizard_small_{i}.bmp")
    for i, (w, h) in enumerate(SIDE):
        side_image(w, h).save(HERE / f"wizard_side_{i}.bmp")
    print("wrote harvest.ico and the setup wizard images")


if __name__ == "__main__":
    main()
