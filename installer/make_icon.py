"""Generate installer/maistro.ico — an outlined flat-top hexagon in brand orange.

The shape mirrors the Unicode hexagon (U+2B61) used as the rail logo in the
app. The orange (#e6a117) matches the brand accent on the `i` and `o` in
the "mAistro" wordmark. Outline-only (no fill) matches the look of the
in-app rail logo. Stroke width is proportional to canvas size so the ring
stays visible at 16x16 in the taskbar and tray.

Run:  python installer/make_icon.py
Output: installer/maistro.ico (multi-resolution: 16, 24, 32, 48, 64, 128, 256)
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

BRAND_ORANGE = (0xE6, 0xA1, 0x17, 0xFF)
SIZES = [16, 24, 32, 48, 64, 128, 256]
STROKE_RATIO = 0.10   # fraction of canvas size; 10% keeps it readable at 16px
MARGIN_RATIO = 0.10   # breathing room from canvas edge
OUT = Path(__file__).parent / "maistro.ico"


def hexagon_points(size: int) -> list[tuple[float, float]]:
    """Flat-top hexagon vertices inscribed in a square of given size.

    Flat-top means two horizontal edges (top and bottom). Vertices are at
    +/-30deg and +/-150deg from the right, which for a unit hex give the
    six points below. MARGIN_RATIO keeps a small breathing room from the
    canvas edge so the shape doesn't kiss the icon border.
    """
    cx = cy = size / 2
    r = (size / 2) * (1 - MARGIN_RATIO)
    pts = []
    for k in range(6):
        angle = math.radians(60 * k)  # 0, 60, 120, ... degrees
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return pts


def render(size: int) -> Image.Image:
    # Render at 4x for small sizes (sharper antialias on downsample) and
    # 2x for large. Stroke width scales with the supersampled canvas so
    # the final image preserves the intended proportional thickness.
    scale = 4 if size <= 64 else 2
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    stroke = max(2, int(round(big * STROKE_RATIO)))
    draw.polygon(hexagon_points(big), outline=BRAND_ORANGE, width=stroke)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    frames = [render(s) for s in SIZES]
    frames[0].save(
        OUT,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=frames[1:],
    )
    print(f"wrote {OUT}  ({len(frames)} sizes: {', '.join(str(s) for s in SIZES)})")


if __name__ == "__main__":
    main()
