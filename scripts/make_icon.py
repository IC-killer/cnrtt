"""Generate the cnrtt application icon (multi-resolution .ico).

Design: a rounded dark terminal screen with a green "中" (Chinese-capable)
glyph and a small green signal wave at the bottom, evoking RTT real-time
transfer with Chinese support.

Run:  python scripts/make_icon.py
Output: src/cnrtt/assets/cnrtt.ico  (16, 32, 48, 256 px)
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.join("src", "cnrtt", "assets")
OUT_ICO = os.path.join(OUT_DIR, "cnrtt.ico")
SIZES = [16, 32, 48, 256]


def _font(size: int) -> ImageFont.FreeTypeFont:
    # Try a CJK-capable font available on Windows; fall back to default.
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc",   # Microsoft YaHei Bold
        r"C:\Windows\Fonts\msyh.ttc",     # Microsoft YaHei
        r"C:\Windows\Fonts\simhei.ttf",   # SimHei
        r"C:\Windows\Fonts\segoeuib.ttf", # Segoe UI Bold (no CJK, last resort)
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def render(size: int) -> Image.Image:
    """Render the icon at the given pixel size (square, RGBA)."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Geometry (all proportional to s)
    pad = max(1, s // 16)            # outer padding
    r_outer = s // 2                 # full-round outer rect radius
    r_inner = max(1, s // 10)        # screen corner radius

    # 1. Outer rounded plate (dark slate, subtle border)
    box = (pad, pad, s - pad, s - pad)
    d.rounded_rectangle(box, radius=r_outer, fill=(30, 30, 46, 255))
    border_w = max(1, s // 64)
    d.rounded_rectangle(box, radius=r_outer, outline=(86, 92, 110, 255),
                         width=border_w)

    # 2. Terminal screen (slightly inset, near-black)
    sp = max(2, s // 8)              # screen padding from plate
    sx0, sy0 = pad + sp, pad + sp
    sx1, sy1 = s - pad - sp, int(s * 0.70)
    d.rounded_rectangle((sx0, sy0, sx1, sy1), radius=r_inner,
                        fill=(20, 22, 30, 255))

    # 3. Title bar dots (red/yellow/green) - skip when too small
    if s >= 32:
        dot_r = max(1, s // 40)
        dy = sy0 + sp
        colors = [(220, 80, 80), (220, 180, 60), (90, 200, 110)]
        for i, c in enumerate(colors):
            cx = sx0 + sp + i * (dot_r * 3 + dot_r)
            d.ellipse((cx, dy, cx + dot_r * 2, dy + dot_r * 2), fill=c + (255,))

    # 4. Central "中" glyph in green (the Chinese-support signature)
    glyph_color = (90, 220, 130, 255)
    # Vertical extent of the glyph inside the screen
    gy0 = sy0 + sp + (s // 16 if s >= 32 else 0)
    gy1 = sy1 - sp
    font_size = max(8, int((gy1 - gy0) * 0.85))
    font = _font(font_size)
    text = "中"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (sx0 + sx1) // 2 - tw // 2 - bbox[0]
    ty = (gy0 + gy1) // 2 - th // 2 - bbox[1]
    d.text((tx, ty), text, font=font, fill=glyph_color)

    # 5. Signal wave below the screen (RTT real-time transfer)
    wave_y = int(s * 0.82)
    wave_h = max(1, s // 24)
    wave_x0 = sx0
    wave_x1 = sx1
    if s >= 24:
        steps = 24
        pts = []
        for i in range(steps + 1):
            x = wave_x0 + (wave_x1 - wave_x0) * i / steps
            # dampened sine, two humps
            y = wave_y - wave_h * (math.sin(i / steps * 2 * math.pi) + 1) / 2
            pts.append((x, y))
        for i in range(len(pts) - 1):
            d.line([pts[i], pts[i + 1]], fill=(90, 200, 130, 255),
                   width=max(1, s // 48))
    else:
        # Tiny: just a flat green line
        d.line([(wave_x0, wave_y), (wave_x1, wave_y)],
               fill=(90, 200, 130, 255), width=max(1, s // 32))

    return img


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    images = [render(sz) for sz in SIZES]
    # Largest first so Windows Explorer picks the best quality thumbnail.
    images.sort(key=lambda im: im.width, reverse=True)
    images[0].save(
        OUT_ICO,
        format="ICO",
        sizes=[(sz, sz) for sz in SIZES],
        append_images=images[1:],
    )
    print(f"Wrote {OUT_ICO}")
    for im in images:
        print(f"  - {im.width}x{im.height}")


if __name__ == "__main__":
    main()
