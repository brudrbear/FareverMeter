"""Build the minimap background asset from the game's own map tiles.

Maintainer one-shot, not part of the meter: run it here when the world map
changes (rarely — a new zone, a reworked area), commit what it writes under
assets/maps/, and the meter ships a stable, locally-testable image instead of
touching the game or its files at runtime.

    python build_map_assets.py [--pak PATH] [--world w1_siagarta] [--scale 0.5]

What it writes, per world:
    assets/maps/<world>.webp   — all minimap tiles stitched, +y DOWNWARD
    assets/maps/<world>.json   — the world->pixel transform, self-describing

The transform is not guessed and not calibrated by eye; it comes from the
tile grid itself. The game names each tile <tx>_<ty>_1024.png where tile
(tx, ty) covers world x in [tx*576, (tx+1)*576), y in [ty*576, (ty+1)*576) —
576 world units per 1024 px tile. Cross-checked against questlog.gg's map
viewer config (gameMinX = -4*576 for tile x -4, lat = -y), which places
markers with raw in-game coordinates on these very tiles. The +y-down
orientation also agrees with the compass measurement that the world's +y is
SOUTH.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

from PIL import Image

from pak_extract import load, DEFAULT_PAK

MAP_PAK = Path(r"E:\SteamLibrary\steamapps\common\Farever\res.map.pak")
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "maps"
WORLD_UNITS_PER_TILE = 576.0
TILE_PX = 1024


def build(pak_path: Path, world: str, scale: float):
    data, entries, data_off = load(pak_path)
    pat = re.compile(rf"Level/World/{re.escape(world)}\.dat/minimap/"
                     rf"(-?\d+)_(-?\d+)_{TILE_PX}\.png$", re.I)
    tiles = {}
    for e in entries:
        m = pat.search(e.path)
        if m:
            tiles[(int(m.group(1)), int(m.group(2)))] = e
    if not tiles:
        sys.exit(f"[!] no minimap tiles for {world!r} in {pak_path.name}")
    xs = sorted(t[0] for t in tiles)
    ys = sorted(t[1] for t in tiles)
    x0, x1, y0, y1 = xs[0], xs[-1], ys[0], ys[-1]
    t_px = int(round(TILE_PX * scale))
    w, h = (x1 - x0 + 1) * t_px, (y1 - y0 + 1) * t_px
    print(f"[*] {world}: {len(tiles)} tiles, x {x0}..{x1}, y {y0}..{y1} "
          f"-> {w}x{h} at scale {scale}", file=sys.stderr)

    # +y DOWN: tile row = ty - y0. The game's +y is south (measured), so south
    # is down, i.e. an ordinary map.
    out = Image.new("RGB", (w, h), (16, 16, 16))
    for (tx, ty), e in sorted(tiles.items()):
        blob = data[data_off + e.pos: data_off + e.pos + e.size]
        im = Image.open(io.BytesIO(blob)).convert("RGB")
        if t_px != TILE_PX:
            im = im.resize((t_px, t_px), Image.LANCZOS)
        out.paste(im, ((tx - x0) * t_px, (ty - y0) * t_px))

    ASSETS.mkdir(parents=True, exist_ok=True)
    img_path = ASSETS / f"{world}.webp"
    out.save(img_path, "WEBP", quality=80, method=6)
    px_per_unit = t_px / WORLD_UNITS_PER_TILE
    meta = {
        # world coords of the image's top-left pixel, and the scale.
        # image_px = (world_x - origin_x) * px_per_unit
        # image_py = (world_y - origin_y) * px_per_unit   (+y is DOWN)
        "world": world,
        "origin_x": x0 * WORLD_UNITS_PER_TILE,
        "origin_y": y0 * WORLD_UNITS_PER_TILE,
        "px_per_unit": px_per_unit,
        "width": w, "height": h,
        "units_per_tile": WORLD_UNITS_PER_TILE,
        "tile_px": t_px,
        "tiles_x": [x0, x1], "tiles_y": [y0, y1],
        "y_down": True,
    }
    meta_path = ASSETS / f"{world}.json"
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"[+] {img_path}  ({img_path.stat().st_size:,} bytes)")
    print(f"[+] {meta_path}")
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pak", type=Path, default=MAP_PAK)
    ap.add_argument("--world", default="w1_siagarta")
    ap.add_argument("--scale", type=float, default=0.5,
                    help="tile downsample; 0.5 = 512px tiles = 0.89 px per "
                         "world unit, about the minimap's own render density")
    args = ap.parse_args()
    build(args.pak, args.world, args.scale)


if __name__ == "__main__":
    main()
