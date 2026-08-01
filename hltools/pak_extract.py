"""List and extract entries from Farever's Heaps .pak archives.

Maintainer tool, not part of the meter: run it here when the game's world map
changes (rarely), commit the extracted image, and the meter ships with a
stable asset instead of reading anything at runtime. See TESTING.md,
"The minimap's map background".

Format (hxd.fmt.pak.Data, verified against res.map.pak's own bytes — the
directory tree Level/World/W1_Siagarta.dat parses cleanly and file data lands
on the expected magics):

    "PAK" <version:u8> <headerSize:i32le> <dataSize:i32le>
    entry := <nameLen:u8> <name:utf8> <flags:u8>
             flags & 1 (directory): <count:i32le> entry*count
             else (file):           <pos> <size:i32le> <checksum:i32le>
                 where <pos> is f64le when flags & 2, else i32le — bit 2
                 exists because a pak past 2 GB (res.pak is 5.1) outgrows
                 an i32 position. Per hxd.fmt.pak.Reader, not guessed.

File positions are relative to the data section, which starts at headerSize.

Usage:
    python pak_extract.py list   [--pak PATH] [--filter SUBSTR] [--min-size N]
    python pak_extract.py extract PATTERN [--pak PATH] [--out DIR]

PATTERN is a case-insensitive substring of the full path; every match is
written out under --out (default: pak_out/ next to this script), flattened.
"""
from __future__ import annotations

import argparse
import fnmatch
import struct
import sys
from pathlib import Path

DEFAULT_PAK = Path(r"E:\SteamLibrary\steamapps\common\Farever\res.pak")


class Entry:
    __slots__ = ("path", "pos", "size", "checksum")

    def __init__(self, path, pos, size, checksum):
        self.path, self.pos, self.size, self.checksum = path, pos, size, checksum


def read_tree(buf: bytes, pak_name: str):
    """Parse the directory tree, returning (entries, data_offset)."""
    if buf[:3] != b"PAK":
        sys.exit(f"[!] {pak_name}: not a Heaps pak (magic {buf[:3]!r})")
    version = buf[3]
    header_size, data_size = struct.unpack_from("<ii", buf, 4)
    off = 12
    entries: list[Entry] = []

    def walk(prefix):
        nonlocal off
        name_len = buf[off]; off += 1
        name = buf[off:off + name_len].decode("utf-8", "replace"); off += name_len
        flags = buf[off]; off += 1
        path = f"{prefix}/{name}" if prefix and name else (name or prefix)
        # Any flag bit beyond directory (1) and 64-bit-position (2) means a
        # format revision this parser predates. Refusing loudly beats writing
        # out garbage that looks like a texture.
        if flags & ~3:
            sys.exit(f"[!] {pak_name}: unknown flags 0x{flags:02x} on "
                     f"{path!r} — pak format changed; update this parser.")
        if flags & 1:
            (count,) = struct.unpack_from("<i", buf, off); off += 4
            for _ in range(count):
                walk(path)
        else:
            if flags & 2:
                (pos,) = struct.unpack_from("<d", buf, off); off += 8
                pos = int(pos)
            else:
                (pos,) = struct.unpack_from("<i", buf, off); off += 4
            size, checksum = struct.unpack_from("<ii", buf, off)
            off += 8
            entries.append(Entry(path, pos, size, checksum))

    walk("")
    print(f"[*] {pak_name}: version {version}, header {header_size}, "
          f"data {data_size}, {len(entries)} files", file=sys.stderr)
    return entries, header_size


def load(pak_path: Path):
    data = pak_path.read_bytes()
    entries, data_off = read_tree(data, pak_path.name)
    return data, entries, data_off


def cmd_list(args):
    _data, entries, _off = load(args.pak)
    flt = (args.filter or "").lower()
    n = 0
    for e in sorted(entries, key=lambda e: -e.size):
        if flt and flt not in e.path.lower():
            continue
        if e.size < args.min_size:
            continue
        print(f"{e.size:>12,}  {e.path}")
        n += 1
    print(f"[*] {n} entries shown", file=sys.stderr)


def cmd_extract(args):
    data, entries, data_off = load(args.pak)
    args.out.mkdir(parents=True, exist_ok=True)
    pat = args.pattern.lower()
    hits = [e for e in entries
            if pat in e.path.lower() or fnmatch.fnmatch(e.path.lower(), pat)]
    if not hits:
        sys.exit(f"[!] nothing matches {args.pattern!r}")
    for e in hits:
        blob = data[data_off + e.pos: data_off + e.pos + e.size]
        # Flat filenames: the tree's slashes become underscores, so a whole
        # match set lands in one folder you can eyeball.
        out = args.out / e.path.replace("/", "_")
        out.write_bytes(blob)
        head = blob[:8]
        kind = ("png" if head.startswith(b"\x89PNG") else
                "jpg" if head.startswith(b"\xff\xd8") else
                "dds" if head.startswith(b"DDS ") else
                head.hex())
        print(f"[+] {e.path}  ->  {out.name}  ({e.size:,} bytes, {kind})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="list entries, largest first")
    p.add_argument("--pak", type=Path, default=DEFAULT_PAK)
    p.add_argument("--filter", help="case-insensitive substring of the path")
    p.add_argument("--min-size", type=int, default=0)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("extract", help="extract entries matching a pattern")
    p.add_argument("pattern")
    p.add_argument("--pak", type=Path, default=DEFAULT_PAK)
    p.add_argument("--out", type=Path,
                   default=Path(__file__).parent / "pak_out")
    p.set_defaults(fn=cmd_extract)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
