"""Generate desktop/harvest.ico for the Windows app icon (a droplet mark)."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).with_name("harvest.ico")


def render(size: int) -> Image.Image:
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, s - 8, s - 8], radius=52, fill=(12, 11, 10, 255))
    # droplet / leaf shape in harvest amber -> green gradient (approximated solid)
    pts = [(s * 0.5, s * 0.20), (s * 0.78, s * 0.56), (s * 0.5, s * 0.84), (s * 0.22, s * 0.56)]
    d.polygon(pts, fill=(240, 165, 59, 255))
    d.ellipse([s * 0.30, s * 0.46, s * 0.70, s * 0.80], fill=(131, 194, 103, 255))
    return img.resize((size, size), Image.LANCZOS)


def main():
    base = render(256)
    base.save(OUT, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
