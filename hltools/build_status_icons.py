"""Build the buff tracker's icon sheet from the game's own ability atlases.

Maintainer one-shot, not part of the meter: run it here when the game's icons
change (a patch adding statuses), commit what it writes under assets/status/,
and the meter ships a stable, locally-testable sheet instead of reading an
857MB pak at startup. Same reasoning as build_map_assets.py.

    python build_status_icons.py [--pak PATH] [--cell 64]

What it writes:
    assets/status/icons.webp   — every distinct status icon, one grid cell each
    assets/status/icons.json   — {cell: N, cols: N, ids: {status id: cell}}

Why a sheet rather than 231 files: Tk wants one PhotoImage per icon anyway, and
one decode of one image at startup beats 231 file opens. The cells are indexed
by STATUS ID, so the tracker asks for "Mace_Benediction_Passive_Status" and gets
a cell number without knowing anything about atlases.

Where the coordinates come from: data.cdb's `skill` sheet gives each status a
`gfx` of {file, size, x, y, width?, height?}. `size` is the atlas's CELL size in
pixels and x/y/width/height are in CELLS — so the pixel rect is
(x*size, y*size, width*size, height*size), width/height defaulting to 1. The
same icon is addressed at 96/1x1 on one row and 48/2x2 on another, and both
resolve to the same 96px square; that is why the rect is computed rather than
assumed square-at-96.

Icons are deduplicated by that rect: several statuses legitimately share art
(Sword_Swarm_Skill1_Status and Sword_Swarm_Skill1_SelfBuff are the debuff and
the self-buff halves of one skill), and drawing them from one cell keeps the
sheet honest about how many distinct pictures there actually are.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from PIL import Image

from gamepath import find_hlboot
from pak_extract import load

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "status"
# 64px: the trays draw icons between about 24 and 48 pixels depending on the
# scale slider, so 64 leaves headroom to scale DOWN (which is clean) without
# ever scaling up (which is not). The 96px source would double the sheet for
# detail nothing renders.
CELL = 64
COLS = 16


def status_gfx(game_dir: Path):
    """[(status id, gfx dict)] for every status the cdb defines an icon for.

    Which rows are statuses is emit_offsets.py's call, not this script's — the
    two must agree or the sheet grows cells nothing asks for and, worse, misses
    cells something does. Imported rather than reimplemented for that reason.
    """
    from emit_offsets import _status_nature_index

    data, entries, off = load(game_dir / "res.light.pak")
    e = next(x for x in entries if x.path.endswith("data.cdb"))
    cdb = json.loads(data[off + e.pos: off + e.pos + e.size])
    skill = next(s for s in cdb["sheets"] if s["name"] == "skill")
    nature_status = _status_nature_index(skill)
    out = []
    for ln in skill["lines"]:
        sid = ln.get("id")
        if not isinstance(sid, str) or ln.get("nature") != nature_status:
            continue
        g = ln.get("gfx")
        if isinstance(g, dict) and isinstance(g.get("file"), str):
            out.append((sid, g))

    # Item statuses, keyed "item:<id>" to match what the tracker tracks them
    # by. They need their own entries because every st.skill.ItemStatus reports
    # the same kind at runtime, so there is no per-status row to hang an icon
    # on — a meal and a potion would share one picture, or more likely none.
    # The ITEM's own gfx is the right art anyway: what you want to recognise in
    # the tray is the food you ate.
    item = next(s for s in cdb["sheets"] if s["name"] == "item")
    for ln in item["lines"]:
        iid = ln.get("id")
        if not isinstance(iid, str):
            continue
        effects = (ln.get("props") or {}).get("effects")
        if not isinstance(effects, list):
            continue
        if not any(isinstance(b, dict) and b.get("status") for b in effects):
            continue
        g = ln.get("gfx")
        if isinstance(g, dict) and isinstance(g.get("file"), str):
            out.append((f"item:{iid}", g))
    return out


def rect(g):
    """The gfx entry's pixel rect in its atlas. See the module docstring."""
    s = int(g["size"])
    x, y = int(g["x"]) * s, int(g["y"]) * s
    w = int(g.get("width", 1)) * s
    h = int(g.get("height", 1)) * s
    return (x, y, x + w, y + h)


def build(pak_path: Path, cell: int, cols: int):
    game_dir = pak_path.parent
    gfx = status_gfx(game_dir)
    if not gfx:
        sys.exit("[!] no status rows with a gfx in data.cdb — wrong pak?")
    print(f"[*] {len(gfx)} statuses with an icon", file=sys.stderr)

    data, entries, off = load(pak_path)
    by_path = {x.path: x for x in entries}

    # id -> cell, deduplicated by atlas rect.
    cells: dict[tuple, int] = {}
    ids: dict[str, int] = {}
    tiles: list[Image.Image] = []
    atlases: dict[str, Image.Image | None] = {}
    missing: dict[str, int] = {}

    for sid, g in gfx:
        key = (g["file"],) + rect(g)
        hit = cells.get(key)
        if hit is not None:
            ids[sid] = hit
            continue
        f = g["file"]
        if f not in atlases:
            ent = by_path.get(f)
            if ent is None:
                atlases[f] = None
            else:
                blob = data[off + ent.pos: off + ent.pos + ent.size]
                atlases[f] = Image.open(io.BytesIO(blob)).convert("RGBA")
        atlas = atlases[f]
        if atlas is None:
            # Named and counted, not fatal: a status whose atlas is absent
            # falls back to a coloured tile in the tracker, which is a cosmetic
            # loss on one buff rather than a failed build.
            missing[f] = missing.get(f, 0) + 1
            continue
        box = rect(g)
        if box[2] > atlas.width or box[3] > atlas.height:
            missing[f] = missing.get(f, 0) + 1
            print(f"[!] {sid}: rect {box} outside {f} {atlas.size}",
                  file=sys.stderr)
            continue
        tile = atlas.crop(box)
        if tile.size != (cell, cell):
            tile = tile.resize((cell, cell), Image.LANCZOS)
        cells[key] = len(tiles)
        ids[sid] = len(tiles)
        tiles.append(tile)

    if not tiles:
        sys.exit("[!] no icons could be cropped — nothing written.")
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % cols) * cell, (i // cols) * cell))

    ASSETS.mkdir(parents=True, exist_ok=True)
    img = ASSETS / "icons.webp"
    # q=90, measured against lossless rather than assumed. The art is painted,
    # not flat UI, so lossless costs 1.0MB against 278KB for a difference that
    # is invisible at 3x the size a tray draws these (compared at 40px, the
    # middle of the scale slider's range). What made the call safe is that webp
    # keeps ALPHA exact at every quality — peak alpha error 0 — so the icon
    # edges, which is where a compression artefact would actually show against
    # the overlay's dark background, are bit-identical either way.
    sheet.save(img, "WEBP", quality=90, method=6)
    meta = ASSETS / "icons.json"
    meta.write_text(json.dumps(
        {"cell": cell, "cols": cols, "count": len(tiles),
         "ids": dict(sorted(ids.items()))}, indent=0), encoding="utf-8")

    print(f"[+] {img}  {sheet.width}x{sheet.height}, {len(tiles)} distinct "
          f"icons for {len(ids)} statuses ({img.stat().st_size:,} bytes)",
          file=sys.stderr)
    print(f"[+] {meta}", file=sys.stderr)
    for f, n in sorted(missing.items(), key=lambda kv: -kv[1]):
        print(f"[!] atlas not in pak, {n} status(es) affected: {f}",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pak", type=Path, default=None,
                    help="res.pak; defaults to the detected game directory's")
    ap.add_argument("--cell", type=int, default=CELL)
    ap.add_argument("--cols", type=int, default=COLS)
    args = ap.parse_args()
    pak = args.pak or (Path(find_hlboot(argv_index=99)).parent / "res.pak")
    build(pak, args.cell, args.cols)


if __name__ == "__main__":
    main()
