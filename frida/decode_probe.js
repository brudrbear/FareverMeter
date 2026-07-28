// decode_probe.js — hook ent.Unit.onInflictDamage and decode each DamageResult
// into (hero, skill, element, amount, crit, hits). Validates offsets against
// live combat. DATA (anchors + count_targets) and OFF (meter_offsets) prepended.

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
function hlStr(p) {
    try {
        if (!p || p.isNull()) return null;
        const bytes = p.add(OFF.String.bytes).readPointer();
        if (bytes.isNull()) return null;
        return bytes.readUtf16String();
    } catch (e) { return null; }
}

let n = 0;
const MAX = 24;

function main() {
    const base = findTableBase(resolveAnchors());
    if (!base) { log("!! table not found"); return; }
    const findex = DATA.count_targets["ent.Unit.onInflictDamage"];
    const addr = base.add(findex * 8).readPointer();
    const DR = OFF.DamageResult, BS = OFF.BaseSkill, HERO = OFF.Hero;

    Interceptor.attach(addr, {
        onEnter(a) {
            if (n >= MAX) return;
            try {
                const hero = this.context.rcx;   // ent.Hero (dealer)
                const dr = this.context.rdx;      // st.skill.DamageResult
                const amount = dr.add(DR._amount).readDouble();
                const affinity = hlStr(dr.add(DR.affinity).readPointer());
                const crit = dr.add(DR._critical).readU8();
                const kill = dr.add(DR._kill).readU8();
                const hits = dr.add(DR._hitCount).readS32();
                let skill = null;
                const bs = dr.add(DR.baseSkill).readPointer();
                if (bs && !bs.isNull()) skill = hlStr(bs.add(BS.kind).readPointer());
                const heroName = hlStr(hero.add(HERO.name).readPointer());
                n++;
                log("#" + n + "  " + (heroName || "?") + "  " +
                    (skill || "?") + "  [" + (affinity || "?") + "]  " +
                    "amount=" + amount.toFixed(1) +
                    "  hits=" + hits + (crit ? "  CRIT" : "") + (kill ? "  KILL" : ""));
            } catch (e) { log("decode error: " + e); }
        }
    });
    log("hooked onInflictDamage @ " + addr + " — DEAL DAMAGE (decoding " + MAX + " hits)");
}
main();
