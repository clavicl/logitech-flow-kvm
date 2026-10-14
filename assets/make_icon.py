"""Draws the app icon: two hosts with an arc from one to the other, as Flow
moves between them. Writes icon.png (Linux) and icon.ico (Windows) next to
this script.

    uv run --no-project --with pillow python assets/make_icon.py
"""

import math
import os

from PIL import Image
from PIL import ImageDraw

SIZE = 1024
TOP = (30, 58, 95)
BOTTOM = (13, 26, 44)
SCREEN = (43, 76, 117)
FRAME = (236, 241, 247)
ACCENT = (46, 196, 182)
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

HERE = os.path.dirname(os.path.abspath(__file__))


def background() -> Image.Image:
    gradient = Image.new("RGB", (SIZE, SIZE))
    draw = ImageDraw.Draw(gradient)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        draw.line(
            [(0, y), (SIZE, y)],
            fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)),
        )
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (24, 24, SIZE - 24, SIZE - 24), radius=220, fill=255
    )
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(gradient, mask=mask)
    return icon


def monitor(draw: ImageDraw.ImageDraw, left: int) -> None:
    right = left + 330
    draw.rounded_rectangle(
        (left, 470, right, 700), radius=34, fill=SCREEN, outline=FRAME, width=40
    )
    middle = (left + right) // 2
    draw.rectangle((middle - 26, 700, middle + 26, 770), fill=FRAME)
    draw.rounded_rectangle((middle - 90, 760, middle + 90, 806), radius=20, fill=FRAME)


def arrow(draw: ImageDraw.ImageDraw) -> None:
    cx, cy, r = 512, 470, 250
    start, end = 205, 330
    draw.arc((cx - r, cy - r, cx + r, cy + r), start, end, fill=ACCENT, width=64)
    # Pillow draws an arc's width inside its box: this is the stroke's middle.
    r -= 32
    # Round the tail.
    sx = cx + r * math.cos(math.radians(start))
    sy = cy + r * math.sin(math.radians(start))
    draw.ellipse((sx - 32, sy - 32, sx + 32, sy + 32), fill=ACCENT)

    theta = math.radians(end)
    ex, ey = cx + r * math.cos(theta), cy + r * math.sin(theta)
    # Direction of travel at the end of a clockwise (on screen) arc.
    dx, dy = -math.sin(theta), math.cos(theta)
    px, py = -dy, dx
    tip = (ex + dx * 110, ey + dy * 110)
    back = (ex - dx * 20, ey - dy * 20)
    draw.polygon(
        [
            tip,
            (back[0] + px * 92, back[1] + py * 92),
            (back[0] - px * 92, back[1] - py * 92),
        ],
        fill=ACCENT,
    )


def main() -> None:
    icon = background()
    draw = ImageDraw.Draw(icon)
    monitor(draw, 118)
    monitor(draw, 576)
    arrow(draw)

    icon.resize((512, 512), Image.LANCZOS).save(os.path.join(HERE, "icon.png"))
    icon.resize((256, 256), Image.LANCZOS).save(
        os.path.join(HERE, "icon.ico"), sizes=[(s, s) for s in ICO_SIZES]
    )


if __name__ == "__main__":
    main()
