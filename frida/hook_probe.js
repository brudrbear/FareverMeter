// hook_probe.js — resolve ent.Unit.applyDamage (+ Hero override) and dump the
// arguments of the first N calls so we can learn the signature empirically.
// DATA (from resolver_data.json) is prepended by the Python runner.

function log(msg) { send({ kind: "log", msg: msg }); }

// ---- functions_ptrs resolver (same as resolve_functions.js) ----
function ptrPattern(addr) {
    const bytes = []; let v = uint64(addr.toString());
    for (let i = 0; i < 8; i++) {
        bytes.push(("0" + v.and(0xff).toNumber().toString(16)).slice(-2));
        v = v.shr(8);
    }
    return bytes.join(" ");
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
            let matches; try { matches = Memory.scanSync(r.base, r.size, pat); } catch (e) { continue; }
            for (const m of matches) {
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

// ---- HL object introspection (pure memory reads, fault-safe) ----
function typeName(objPtr) {
    try {
        if (!objPtr || objPtr.isNull()) return null;
        const t = objPtr.readPointer();
        const kind = t.readU32();
        if (kind === 11 || kind === 21) {           // HOBJ / HSTRUCT
            const objDef = t.add(8).readPointer();
            const namePtr = objDef.add(16).readPointer();
            const nm = namePtr.readUtf16String();
            return { kind: kind, name: nm };
        }
        return { kind: kind, name: null };
    } catch (e) { return null; }
}

function looksPtr(np) {
    // crude: canonical user-space pointer, 8-aligned
    try {
        const s = np.toString();
        return np.compare(ptr("0x10000")) > 0 && np.and(0x7).equals(ptr(0));
    } catch (e) { return false; }
}

let count = 0;
const MAX = 12;

function dumpCall(label, ctx) {
    if (count >= MAX) return;
    count++;
    const regs = { rcx: ctx.rcx, rdx: ctx.rdx, r8: ctx.r8, r9: ctx.r9 };
    let lines = ["", "── " + label + " call #" + count + " ──"];
    for (const rn in regs) {
        const v = regs[rn];
        let desc = v.toString();
        const tn = typeName(v);
        if (tn) desc += "  type=" + (tn.name || ("kind" + tn.kind));
        // also interpret as int32 / double-in-int
        try { desc += "  i32=" + v.toInt32(); } catch (e) {}
        lines.push("   " + rn + " = " + desc);
    }
    // stack args (5th, 6th) at rsp+0x28, +0x30
    try {
        const sp = ctx.rsp;
        for (const off of [0x28, 0x30, 0x38]) {
            const v = sp.add(off).readPointer();
            let desc = v.toString();
            const tn = typeName(v);
            if (tn) desc += "  type=" + (tn.name || ("kind" + tn.kind));
            lines.push("   [rsp+0x" + off.toString(16) + "] = " + desc);
        }
    } catch (e) {}
    // float args in xmm0..3 if Frida exposes them
    for (const xn of ["xmm0", "xmm1", "xmm2", "xmm3"]) {
        try {
            if (ctx[xn] !== undefined) {
                const buf = ctx[xn];  // may be a byte array
                lines.push("   " + xn + " raw=" + JSON.stringify(buf).slice(0, 40));
            }
        } catch (e) {}
    }
    log(lines.join("\n"));
}

function main() {
    const resolved = resolveAnchors();
    log("anchors resolved: " + resolved.length);
    const base = findTableBase(resolved);
    if (!base) { log("!! table not found"); send({ kind: "ready", ok: false }); return; }
    log("functions_ptrs base = " + base);

    const hooks = [["ent.Unit.applyDamage", DATA.targets["ent.Unit.applyDamage"]],
                   ["ent.Hero.applyDamage", DATA.targets["ent.Hero.applyDamage"]]];
    for (const [name, findex] of hooks) {
        if (findex == null) continue;
        let addr; try { addr = base.add(findex * 8).readPointer(); } catch (e) { continue; }
        Interceptor.attach(addr, {
            onEnter(args) { dumpCall(name, this.context); }
        });
        log("hooked " + name + " @ " + addr);
    }
    log(">>> ATTACK A MONSTER — dumping first " + MAX + " applyDamage calls <<<");
    send({ kind: "ready", ok: true });
}
main();
