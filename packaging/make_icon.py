"""Draw assets/farevermeter.ico — the tray, executable and installer icon.

Generated rather than committed as an opaque binary: the palette is the meter's
own, so if the overlay is ever re-skinned the icon can follow by editing three
constants here instead of by opening an image editor.

Each size is drawn at its own resolution rather than downscaled from one big
one. The mark is three bars, and at 16x16 a bar is two pixels tall — scaling
those down from 256 turns them to mush.

    py packaging/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "farevermeter.ico"

# Straight out of farever_meter.py: the header teal, the border brown, and the
# damage/healing bar colours.
BORDER = "#2C1A0E"
HEADER = "#54A4A9"
BARS = ("#F2E1CB", "#5279B5", "#5E9C4A")

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    edge = max(1, round(size / 16))

    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=round(size * 0.22),
                        fill=HEADER, outline=BORDER, width=edge)

    # Three left-aligned bars of descending length — a damage meter at a glance,
    # and still legible when Windows renders it 16 pixels wide.
    pad = round(size * 0.22)
    inner = size - pad * 2
    bar_h = max(1, round(size * 0.13))
    gap = max(1, round(size * 0.09))
    total = bar_h * 3 + gap * 2
    y = (size - total) / 2
    for frac, colour in zip((1.0, 0.68, 0.4), BARS):
        w = max(2, round(inner * frac))
        d.rectangle((pad, round(y), pad + w - 1, round(y) + bar_h - 1),
                    fill=colour)
        y += bar_h + gap
    return img


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw(s) for s in SIZES]
    # Pillow writes every append_images frame at its own size, so the .ico ends
    # up with a real image per resolution instead of one Windows has to rescale.
    frames[-1].save(OUT, format="ICO",
                    sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    print(f"[written] {OUT}  ({', '.join(f'{s}x{s}' for s in SIZES)})")


if __name__ == "__main__":
    main()
