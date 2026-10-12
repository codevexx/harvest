"""Harvest's icons, drawn with Pillow (no other dependencies, so CI can build them).

One leaf design used everywhere:
  app_icon(size, "mac")   macOS app icon: rounded-square backplate on Apple's icon grid
  app_icon(size, "win")   Windows app icon: the same artwork, filling more of the square
  tray_leaf(size)         the leaf alone, for the Windows tray (a status dot goes on top)
  menubar(size)           a black template silhouette that macOS tints for the menu bar

Everything is drawn 4x larger and scaled down, for smooth edges.
"""
from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SS = 4  # supersampling

# Deep slate backplate, amber-to-green leaf (the brand's harvest colors).
BG_TOP, BG_BOTTOM = (46, 60, 96), (13, 19, 36)
LEAF_LIGHT, LEAF_DARK = (255, 189, 64), (64, 172, 98)
VEIN = (255, 248, 222)
STEM = (52, 128, 74)


def _gradient(size: int, c1, c2, angle_deg: float = 90) -> Image.Image:
    """c1 to c2 across the square; 90 = top to bottom, 0 = left to right."""
    g = Image.linear_gradient("L").rotate(90 - angle_deg, resample=Image.BICUBIC, expand=False)
    g = g.resize((size, size), Image.BICUBIC)
    return Image.composite(Image.new("RGB", (size, size), c2), Image.new("RGB", (size, size), c1), g)


def _squircle(size: int, inset: float, n: float = 5.0) -> list[tuple[float, float]]:
    """Superellipse outline (Apple's icon shape) inside a size x size square."""
    c, r = size / 2, size / 2 - inset
    pts = []
    for i in range(720):
        t = 2 * math.pi * i / 720
        ct, st = math.cos(t), math.sin(t)
        pts.append((c + r * math.copysign(abs(ct) ** (2 / n), ct), c + r * math.copysign(abs(st) ** (2 / n), st)))
    return pts


def _rot(cx, cy, x, y, angle_deg):
    a = math.radians(angle_deg)
    return (cx + x * math.cos(a) - y * math.sin(a), cy + x * math.sin(a) + y * math.cos(a))


def _leaf_points(cx, cy, length, width, angle_deg, steps=200):
    """A leaf: pointed tip (local -y), rounded belly, tapering base (local +y); rotated by angle_deg."""
    side = []
    for i in range(steps + 1):
        t = i / steps  # 0 = tip, 1 = base
        y = -length / 2 + length * t
        # Ovate: a fine pointed tip, fullest past the middle, rounding into the stem.
        w = width / 2 * math.sin(math.pi * t) ** 1.25 * (0.7 + 0.45 * t)
        side.append((w, y))
    outline = side + [(-x, y) for x, y in reversed(side)]
    return [_rot(cx, cy, x, y, angle_deg) for x, y in outline]


def _leaf(size: int, cx: float, cy: float, length: float, width: float, angle: float,
          shadow: bool = True, detail: bool = True) -> Image.Image:
    """RGBA leaf (gradient, highlight, veins, stem, optional drop shadow) on a transparent canvas."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(_leaf_points(cx, cy, length, width, angle), fill=255)

    # Stem first (the leaf covers its top end).
    d = ImageDraw.Draw(out)
    stem = [_rot(cx, cy, -length * 0.06 * (k / 10) ** 1.8, length * 0.44 + length * 0.17 * k / 10, angle) for k in range(11)]
    d.line(stem, fill=STEM + (255,), width=max(1, int(width * 0.075)), joint="curve")

    if shadow:
        sh = mask.filter(ImageFilter.GaussianBlur(size * 0.022))
        sh = ImageChops.offset(sh, int(size * 0.01), int(size * 0.028)).point(lambda v: int(v * 0.5))
        shadow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        shadow_layer.putalpha(sh)
        out = Image.alpha_composite(shadow_layer, out)

    # Body: gradient along the leaf, lighter at the tip.
    body = _gradient(size, LEAF_LIGHT, LEAF_DARK, angle + 90).convert("RGBA")
    body.putalpha(mask)
    out = Image.alpha_composite(out, body)

    if detail:
        # Soft highlight on one half for a rounded, lit look.
        hl = Image.new("L", (size, size), 0)
        off = _rot(0, 0, -width * 0.16, 0, angle)
        ImageDraw.Draw(hl).polygon(_leaf_points(cx + off[0], cy + off[1], length * 0.8, width * 0.42, angle), fill=90)
        hl = ImageChops.multiply(hl.filter(ImageFilter.GaussianBlur(size * 0.018)), mask)
        lit = Image.new("RGBA", (size, size), (255, 255, 236, 0))
        lit.putalpha(hl)
        out = Image.alpha_composite(out, lit)

        # Veins, clipped to the leaf.
        veins = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        v = ImageDraw.Draw(veins)
        vw = max(1, int(width * 0.028))
        # Midrib, slightly curved, fading toward the tip.
        rib = [_rot(cx, cy, width * 0.03 * math.sin(math.pi * k / 20), -length * 0.4 + length * 0.86 * k / 20, angle) for k in range(21)]
        v.line(rib, fill=VEIN + (175,), width=vw, joint="curve")
        for f in (-0.22, -0.04, 0.14, 0.3):  # side veins curving out toward the tip
            for s in (1, -1):
                reach = width * (0.3 - 0.12 * abs(f))
                pts = [_rot(cx, cy, s * reach * (k / 8), length * (f - 0.13 * (k / 8) ** 1.4), angle) for k in range(9)]
                v.line(pts, fill=VEIN + (95,), width=max(1, int(vw * 0.6)), joint="curve")
        veins.putalpha(ImageChops.multiply(veins.getchannel("A"), mask))
        out = Image.alpha_composite(out, veins)
    return out


def app_icon(size: int = 1024, platform: str = "mac") -> Image.Image:
    S = size * SS
    inset = S * (100 / 1024) if platform == "mac" else S * 0.035  # Apple's grid leaves room for the shadow
    shape = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shape).polygon(_squircle(S, inset), fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    if platform == "mac":  # the soft shadow macOS icons sit on
        sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sh.putalpha(ImageChops.offset(shape, 0, int(S * 0.012)).filter(ImageFilter.GaussianBlur(S * 0.016)).point(lambda v: int(v * 0.4)))
        img = Image.alpha_composite(img, sh)

    plate = _gradient(S, BG_TOP, BG_BOTTOM, 90).convert("RGBA")
    # Warm glow behind the leaf.
    glow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glow).ellipse([S * 0.26, S * 0.24, S * 0.74, S * 0.72], fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.1)).point(lambda v: int(v * 0.38))
    warm = Image.new("RGBA", (S, S), (255, 190, 90, 0))
    warm.putalpha(glow)
    plate = Image.alpha_composite(plate, warm)
    # Thin light rim along the top edge.
    rim = ImageChops.subtract(shape, ImageChops.offset(shape, 0, int(S * 0.005)))
    rim_layer = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    rim_layer.putalpha(rim.point(lambda v: int(v * 0.35)))
    plate = Image.alpha_composite(plate, rim_layer)
    plate.putalpha(shape)
    img = Image.alpha_composite(img, plate)

    leaf = _leaf(S, S * 0.5, S * 0.47, S * 0.6, S * 0.34, 40)
    img = Image.alpha_composite(img, leaf)
    return img.resize((size, size), Image.LANCZOS)


def tray_leaf(size: int = 64) -> Image.Image:
    """The leaf alone (no backplate), for small sizes like the Windows tray."""
    S = size * SS
    leaf = _leaf(S, S * 0.5, S * 0.46, S * 0.8, S * 0.48, 40, shadow=False, detail=size >= 48)
    return leaf.resize((size, size), Image.LANCZOS)


def menubar(size: int = 44) -> Image.Image:
    """Black leaf silhouette with a cut-out midrib: a macOS template image."""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy, length, width, ang = S / 2, S * 0.47, S * 0.78, S * 0.46, 40
    stem = [_rot(cx, cy, -length * 0.06 * (k / 8) ** 1.8, length * 0.44 + length * 0.18 * k / 8, ang) for k in range(9)]
    d.line(stem, fill=(0, 0, 0, 255), width=int(S * 0.075), joint="curve")
    d.polygon(_leaf_points(cx, cy, length, width, ang), fill=(0, 0, 0, 255))
    d.line([_rot(cx, cy, 0, -length * 0.28, ang), _rot(cx, cy, 0, length * 0.36, ang)], fill=(0, 0, 0, 0), width=int(S * 0.065))
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":  # previews: python icon_art.py out_dir
    import sys
    from pathlib import Path
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    app_icon(1024, "mac").save(out / "preview_mac.png")
    app_icon(256, "win").save(out / "preview_win.png")
    tray_leaf(64).save(out / "preview_tray.png")
    menubar(44).save(out / "preview_menubar.png")
    print("previews written to", out)
