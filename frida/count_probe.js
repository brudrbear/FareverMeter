// count_probe.js — hook the curated shortlist and count how often each fires,
// plus grab a one-time arg snapshot for whichever fire. DATA prepended by runner.

function log(m) { send({ kind: "log", msg: m }); }

function ptrPattern(addr) {
    const b = []; let v = uint64(addr.toString());
    for (let i = 0; i < 8; i++) { b.push(("0" + v.and(0xff).toNumber().toString(16)).slice(-2)); v = v.shr(8); }
    return b.join(" ");
}
function resolveAnchors() {
    const out = [], cache = {};
    for (const a of DATA.anchors) {
        try {
            if (!(a.module in cache)) cache[a.module] = Process.findModuleByName(a.module);
            const m = cache[a.module]; if (!m) continue;
            const addr = m.findExportByName(a.symbol);
            if (addr && !addr.isNull()) out.push({ findex: a.findex, addr: addr });
        } catch (e) {}
    }
    return out;
}
function findTableBase(resolved) {
    const ranges = Process.enumerateRanges("rw-").filter(r => r.size < 0x8000000);
    for (let s = 0; s < Math.min(resolved.length, 6); s++) {
        const seed = resolved[s], pat = ptrPattern(seed.addr);
        for (const r of ranges) {
            let mm; try { mm = Memory.scanSync(r.base, r.size, pat); } catch (e) { continue; }
            for (const m of mm) {
                const base = m.address.sub(seed.findex * 8);
                if (base.compare(r.base) < 0) continue;
                let agree = 0, checked = 0;
                for (const o of resolved) {
                    if (o === seed) continue;
                    const slot = base.add(o.findex * 8);
                    if (slot.compare(r.base) < 0 || slot.add(8).compare(r.base.add(r.size)) > 0) continue;
                    checked++;
                    let v; try { v = slot.readPointer(); } catch (e) { continue; }
                    if (v.equals(o.addr)) agree++;
                    if (checked >= 8) break;
                }
                if (agree >= 3) return base;
            }
        }
    }
    return null;
}
function typeName(p) {
    try {
        if (!p || p.isNull()) return null;
        const t = p.readPointer(); const kind = t.readU32();
        if (kind === 11 || kind === 21) {
            const od = t.add(8).readPointer();
            return od.add(16).readPointer().readUtf16String();
        }
        return "kind" + kind;
    } catch (e) { return null; }
}

const counts = {};
const snap = {};

function main() {
    const resolved = resolveAnchors();
    const base = findTableBase(resolved);
    if (!base) { log("!! table not found"); return; }
    log("base=" + base + "; hooking " + Object.keys(DATA.count_targets).length + " functions");
    for (const name in DATA.count_targets) {
        const findex = DATA.count_targets[name];
        let addr; try { addr = base.add(findex * 8).readPointer(); } catch (e) { continue; }
        counts[name] = 0;
        Interceptor.attach(addr, {
            onEnter(a) {
                counts[name]++;
                if (!snap[name]) {
                    const c = this.context;
                    snap[name] = {
                        rcx: c.rcx.toString(), rcxT: typeName(c.rcx),
                        rdx: c.rdx.toString(), rdxT: typeName(c.rdx), rdxI: c.rdx.toInt32(),
                        r8: c.r8.toString(), r8T: typeName(c.r8), r8I: c.r8.toInt32(),
                        r9: c.r9.toString(), r9T: typeName(c.r9), r9I: c.r9.toInt32(),
                    };
                }
            }
        });
    }
    log(">>> DEAL DAMAGE NOW <<<");
    const iv = setInterval(() => {
        const nz = Object.keys(counts).filter(k => counts[k] > 0)
                         .map(k => k + "=" + counts[k]);
        send({ kind: "counts", nz: nz, snap: snap });
    }, 3000);
}
main();
