// resolve_functions.js — locate the HashLink module's functions_ptrs table and
// resolve target HL function indices to their live JIT addresses.
//
// Strategy: functions_ptrs is an array indexed by findex (functions + natives
// share the index space). Native entries point at known hdll exports. We
// resolve a few native addresses independently, scan memory for one of them,
// and for each candidate location L treat base = L - findex*8, then cross-check
// other natives at base + findex*8. Once several agree, base is the array.
//
// DATA (anchors, targets, counts) is prepended by the Python runner.

function log(o) { send({ kind: "log", msg: o }); }

function ptrPattern(addr) {
    // little-endian 8-byte hex pattern for Memory.scanSync
    const bytes = [];
    let v = uint64(addr.toString());
    for (let i = 0; i < 8; i++) {
        bytes.push(("0" + (v.and(0xff)).toNumber().toString(16)).slice(-2));
        v = v.shr(8);
    }
    return bytes.join(" ");
}

function resolveAnchors() {
    const resolved = [];
    const modCache = {};
    for (const a of DATA.anchors) {
        try {
            if (!(a.module in modCache))
                modCache[a.module] = Process.findModuleByName(a.module);
            const m = modCache[a.module];
            if (!m) continue;
            const addr = m.findExportByName(a.symbol);
            if (addr && !addr.isNull())
                resolved.push({ findex: a.findex, addr: addr, sym: a.symbol });
        } catch (e) { /* not found in that module — skip */ }
    }
    return resolved;
}

function rwRanges() {
    return Process.enumerateRanges("rw-").filter(r => r.size < 0x8000000);
}

function execRanges() {
    return Process.enumerateRanges("r-x");
}

function findTableBase(resolved) {
    const N = DATA.nfunctions + DATA.nnatives;
    const ranges = rwRanges();
    // Use the first few resolved anchors as the scan seed / validators.
    for (let s = 0; s < Math.min(resolved.length, 6); s++) {
        const seed = resolved[s];
        const pat = ptrPattern(seed.addr);
        for (const r of ranges) {
            let matches;
            try { matches = Memory.scanSync(r.base, r.size, pat); }
            catch (e) { continue; }
            for (const m of matches) {
                const base = m.address.sub(seed.findex * 8);
                // base must be inside a readable range and large enough
                if (base.compare(r.base) < 0) continue;
                // cross-validate other anchors
                let agree = 0, checked = 0;
                for (const other of resolved) {
                    if (other === seed) continue;
                    const slot = base.add(other.findex * 8);
                    // slot must be within same range bounds
                    if (slot.compare(r.base) < 0 ||
                        slot.add(8).compare(r.base.add(r.size)) > 0) continue;
                    checked++;
                    let val;
                    try { val = slot.readPointer(); } catch (e) { continue; }
                    if (val.equals(other.addr)) agree++;
                    if (checked >= 8) break;
                }
                if (agree >= 3) {
                    return { base: base, seed: seed, agree: agree, N: N,
                             range: r };
                }
            }
        }
    }
    return null;
}

function main() {
    log("resolved anchors: attempting " + DATA.anchors.length);
    const resolved = resolveAnchors();
    log("anchors resolved to live addresses: " + resolved.length);
    resolved.slice(0, 5).forEach(a =>
        log("   findex=" + a.findex + " " + a.sym + " @ " + a.addr));
    if (resolved.length < 4) {
        log("!! too few anchors resolved; cannot triangulate table");
        send({ kind: "result", ok: false });
        return;
    }

    const found = findTableBase(resolved);
    if (!found) {
        log("!! functions_ptrs table not found");
        send({ kind: "result", ok: false });
        return;
    }
    log("functions_ptrs base = " + found.base + "  (validated " +
        found.agree + " anchors)  in range " + found.range.base);

    const xr = execRanges();
    function inExec(a) {
        return xr.some(r => a.compare(r.base) >= 0 &&
                            a.compare(r.base.add(r.size)) < 0);
    }

    const out = {};
    for (const name in DATA.targets) {
        const findex = DATA.targets[name];
        if (findex == null) { out[name] = null; continue; }
        const slot = found.base.add(findex * 8);
        let addr = null;
        try { addr = slot.readPointer(); } catch (e) {}
        const ok = addr && !addr.isNull() && inExec(addr);
        out[name] = { findex: findex, addr: addr ? addr.toString() : null,
                      inExec: ok };
        log("target " + name + " findex=" + findex + " -> " +
            (addr ? addr : "null") + (ok ? "  [JIT ok]" : "  [NOT in exec!]"));
    }
    send({ kind: "result", ok: true, base: found.base.toString(),
           targets: out });
}

main();
