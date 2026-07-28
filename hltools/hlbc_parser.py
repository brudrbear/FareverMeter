"""
hlbc_parser.py — self-contained HashLink bytecode reader (format v4/v5).

Parses hlboot.dat up through the *natives* section, which is everything we need
to build a memory-reading meter:

  * String pool
  * Full type table — every HOBJ/HSTRUCT class with its field names+types,
    method prototypes (name -> function index), and bindings.
  * Globals and natives (native name -> function index + owning hdll).

We deliberately do NOT parse function bodies (opcodes). Their only use would be
argument-type inference, which we do live instead by reading each hooked
object's runtime hl_type header. Skipping opcodes avoids depending on the
version-specific opcode arg tables — the fragile part of the format.

Reference: hashlink/src/code.c hl_code_read.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# hl_type_kind (hashlink hl.h)
HVOID, HUI8, HUI16, HI32, HI64, HF32, HF64, HBOOL, HBYTES, HDYN = range(10)
HFUN, HOBJ, HARRAY, HTYPE, HREF, HVIRTUAL, HDYNOBJ, HABSTRACT, HENUM, HNULL = range(10, 20)
HMETHOD, HSTRUCT, HPACKED = 20, 21, 22
HGUID = 23           # primitive GUID type, added in newer HashLink (no payload)
HLAST = 24

KIND_NAMES = {
    HVOID: "void", HUI8: "u8", HUI16: "u16", HI32: "i32", HI64: "i64",
    HF32: "f32", HF64: "f64", HBOOL: "bool", HBYTES: "bytes", HDYN: "dyn",
    HFUN: "fun", HOBJ: "obj", HARRAY: "array", HTYPE: "type", HREF: "ref",
    HVIRTUAL: "virtual", HDYNOBJ: "dynobj", HABSTRACT: "abstract",
    HENUM: "enum", HNULL: "null", HMETHOD: "method", HSTRUCT: "struct",
    HPACKED: "packed", HGUID: "guid",
}


@dataclass
class ObjField:
    name: str
    type_index: int


@dataclass
class ObjProto:
    name: str
    findex: int
    pindex: int


@dataclass
class HType:
    index: int
    kind: int
    # obj/struct
    name: str | None = None
    super_index: int = -1
    global_index: int = 0
    fields: list[ObjField] = field(default_factory=list)
    protos: list[ObjProto] = field(default_factory=list)
    bindings: list[tuple[int, int]] = field(default_factory=list)  # (field_id, findex)
    # fun/method
    args: list[int] = field(default_factory=list)
    ret: int = -1
    # ref/null/packed
    tparam: int = -1
    # virtual
    vfields: list[ObjField] = field(default_factory=list)
    # abstract
    abs_name: str | None = None
    # enum
    constructs: list[tuple[str, list[int]]] = field(default_factory=list)


@dataclass
class Native:
    lib: str
    name: str
    type_index: int
    findex: int


class HLReader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def byte(self) -> int:
        b = self.d[self.p]
        self.p += 1
        return b

    def i32(self) -> int:
        v = int.from_bytes(self.d[self.p:self.p + 4], "little", signed=True)
        self.p += 4
        return v

    def f64(self) -> float:
        import struct
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v

    def index(self) -> int:
        b = self.byte()
        if (b & 0x80) == 0:
            return b & 0x7F
        if (b & 0x40) == 0:
            v = self.byte() | ((b & 0x1F) << 8)
            return -v if (b & 0x20) else v
        c = self.byte(); dd = self.byte(); e = self.byte()
        v = ((b & 0x1F) << 24) | (c << 16) | (dd << 8) | e
        return -v if (b & 0x20) else v

    uindex = index  # identical byte encoding; semantic non-negativity only


class HLCode:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.version = 0
        self.has_debug = False
        self.ints: list[int] = []
        self.floats: list[float] = []
        self.strings: list[str] = []
        self.debug_files: list[str] = []
        self.types: list[HType] = []
        self.globals: list[int] = []
        self.natives: list[Native] = []
        self.counts: dict[str, int] = {}

    # ---- string pool ----
    def _read_strings(self, r: HLReader, n: int) -> list[str]:
        size = r.i32()
        sdata = r.d[r.p:r.p + size]
        r.p += size
        out = []
        pos = 0
        for _ in range(n):
            ln = r.index()
            out.append(sdata[pos:pos + ln].decode("utf-8", "replace"))
            pos += ln + 1
        return out

    def _read_type(self, r: HLReader, idx: int) -> HType:
        kind = r.byte()
        t = HType(index=idx, kind=kind)
        if kind in (HFUN, HMETHOD):
            nargs = r.byte()
            t.args = [r.index() for _ in range(nargs)]
            t.ret = r.index()
        elif kind in (HOBJ, HSTRUCT):
            name_i = r.index()
            t.name = self.strings[name_i]
            t.super_index = r.index()
            t.global_index = r.index()
            nfields = r.index()
            nproto = r.index()
            nbindings = r.index()
            for _ in range(nfields):
                fn = self.strings[r.index()]
                ft = r.index()
                t.fields.append(ObjField(fn, ft))
            for _ in range(nproto):
                pn = self.strings[r.index()]
                pf = r.index()
                pp = r.index()
                t.protos.append(ObjProto(pn, pf, pp))
            for _ in range(nbindings):
                fid = r.index()
                mid = r.index()
                t.bindings.append((fid, mid))
        elif kind in (HREF, HNULL, HPACKED):
            t.tparam = r.index()
        elif kind == HVIRTUAL:
            nfields = r.index()
            for _ in range(nfields):
                fn = self.strings[r.index()]
                ft = r.index()
                t.vfields.append(ObjField(fn, ft))
        elif kind == HABSTRACT:
            t.abs_name = self.strings[r.index()]
        elif kind == HENUM:
            name_i = r.index()
            t.name = self.strings[name_i]
            t.global_index = r.index()
            nconstructs = r.index()
            for _ in range(nconstructs):
                cn = self.strings[r.index()]
                nparams = r.index()
                params = [r.index() for _ in range(nparams)]
                t.constructs.append((cn, params))
        elif kind >= HLAST:
            raise ValueError(f"invalid type kind {kind} at type {idx} pos {r.p}")
        # else: primitive kinds carry no extra payload
        return t

    def parse(self) -> "HLCode":
        r = HLReader(self.data)
        if r.d[:3] != b"HLB":
            raise ValueError("not a HashLink bytecode file (missing HLB magic)")
        r.p = 3
        self.version = r.byte()
        flags = r.index()
        self.has_debug = bool(flags & 1)
        nints = r.index()
        nfloats = r.index()
        nstrings = r.index()
        nbytes = r.index() if self.version >= 5 else 0
        ntypes = r.index()
        nglobals = r.index()
        nnatives = r.index()
        nfunctions = r.index()
        nconstants = r.index() if self.version >= 4 else 0
        entrypoint = r.index()
        self.counts = dict(nints=nints, nfloats=nfloats, nstrings=nstrings,
                           nbytes=nbytes, ntypes=ntypes, nglobals=nglobals,
                           nnatives=nnatives, nfunctions=nfunctions,
                           nconstants=nconstants, entrypoint=entrypoint)

        self.ints = [r.i32() for _ in range(nints)]
        self.floats = [r.f64() for _ in range(nfloats)]
        self.strings = self._read_strings(r, nstrings)

        if self.version >= 5:
            # bytes section: i32 size, raw bytes, then nbytes position uindexes
            size = r.i32()
            r.p += size
            for _ in range(nbytes):
                r.index()

        if self.has_debug:
            ndebug = r.index()
            self.debug_files = self._read_strings(r, ndebug)

        self.types = [self._read_type(r, i) for i in range(ntypes)]
        self.globals = [r.index() for _ in range(nglobals)]
        for _ in range(nnatives):
            lib = self.strings[r.index()]
            name = self.strings[r.index()]
            ti = r.index()
            findex = r.index()
            self.natives.append(Native(lib, name, ti, findex))
        # Stop here — functions/constants intentionally not parsed.
        return self

    # ---- convenience views ----
    def type_str(self, ti: int) -> str:
        if ti < 0 or ti >= len(self.types):
            return f"?{ti}"
        t = self.types[ti]
        if t.name:
            return t.name
        if t.kind in (HREF, HNULL, HPACKED):
            return f"{KIND_NAMES[t.kind]}<{self.type_str(t.tparam)}>"
        if t.kind in (HFUN, HMETHOD):
            return f"({','.join(self.type_str(a) for a in t.args)})->{self.type_str(t.ret)}"
        return KIND_NAMES.get(t.kind, f"kind{t.kind}")

    def findex_names(self) -> dict[int, str]:
        """Map function index -> 'ClassName.method' from all obj protos/bindings."""
        out: dict[int, str] = {}
        for t in self.types:
            if t.kind not in (HOBJ, HSTRUCT):
                continue
            for p in t.protos:
                out[p.findex] = f"{t.name}.{p.name}"
            for fid, findex in t.bindings:
                fname = t.fields[fid].name if 0 <= fid < len(t.fields) else f"f{fid}"
                out.setdefault(findex, f"{t.name}.{fname}")
        for n in self.natives:
            out[n.findex] = f"${n.lib}.{n.name}"
        return out

    def obj_types(self) -> list[HType]:
        return [t for t in self.types if t.kind in (HOBJ, HSTRUCT) and t.name]

    # ---- runtime object field layout (mirrors hashlink hl_get_obj_rt) ----
    @staticmethod
    def _type_size(kind: int) -> int:
        if kind == HVOID:
            return 0
        if kind in (HUI8, HBOOL):
            return 1
        if kind == HUI16:
            return 2
        if kind in (HI32, HF32):
            return 4
        # HI64, HF64, HGUID and all pointer/reference kinds are 8 bytes on x64.
        return 8

    def _super_chain(self, tindex: int) -> list[HType]:
        """Return [root_super, ..., this] so fields lay out base-first."""
        chain = []
        i = tindex
        seen = set()
        while 0 <= i < len(self.types) and i not in seen:
            seen.add(i)
            t = self.types[i]
            chain.append(t)
            i = t.super_index
        chain.reverse()
        return chain

    def field_offsets(self, tindex: int) -> dict[str, tuple[int, int, str]]:
        """Map field name -> (byte_offset, kind, type_str) for a runtime HL obj.

        Object memory begins with an 8-byte hl_type* header, then fields laid
        out base-class-first, each aligned to its own size (min 1). Matches
        hashlink's hl_get_obj_rt so offsets are valid for direct memory reads.
        """
        size = 8  # HL_WSIZE header (the hl_type* pointer)
        out: dict[str, tuple[int, int, str]] = {}
        for t in self._super_chain(tindex):
            if t.kind not in (HOBJ, HSTRUCT):
                continue
            for f in t.fields:
                ft = self.types[f.type_index] if 0 <= f.type_index < len(self.types) else None
                fsize = self._type_size(ft.kind) if ft else 8
                if fsize > 0:
                    align = min(fsize, 8)
                    if size % align:
                        size += align - (size % align)
                out[f.name] = (size, ft.kind if ft else -1, self.type_str(f.type_index))
                size += fsize
        return out


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"E:\SteamLibrary\steamapps\common\Farever\hlboot.dat"
    code = HLCode(path).parse()
    print(f"version={code.version} has_debug={code.has_debug}")
    print("counts:", json.dumps(code.counts))
    print(f"parsed types={len(code.types)} natives={len(code.natives)} "
          f"obj_types={len(code.obj_types())}")
