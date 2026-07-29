// immune_probe.js — IMMUNE-DAMAGE SPIKE. Throwaway.
//
// The meter records a hit's `_amount` and nothing else, so damage against a
// boss in an immunity phase is counted as if it landed. st.skill.DamageResult
// carries three fields the hook ignores — `_block` (f64), `blocker` (String)
// and `effect` (i32) — and ent.Unit.isInvulnerable() answers directly. This
// says which of them actually distinguishes a nullified hit from a real one,
// before any of it is wired into the meter.
//
// The ground truth it prints alongside them is the TARGET'S HEALTH: if health
// isn't moving while amounts are being reported, the hit did nothing, whatever
// the flags say.
//
// Hooks ent.Unit.onInflictDamage (the shipping hook's own target) and reports
// a tally once a second rather than a line per hit.
//
// DATA + OFF + B are prepended by run_immune.py.

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=base.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return base;}}}return null;}
function hlStr(p){try{if(!p||p.isNull())return null;const b=p.add(OFF.String.bytes).readPointer();if(b.isNull())return null;return b.readUtf16String();}catch(e){return null;}}

const typeCache = {};
function typeName(p) {
    try {
        if (!p || p.isNull()) return null;
        const t = p.readPointer();
        const key = t.toString();
        const hit = typeCache[key];
        if (hit !== undefined) return hit;
        const k = t.readU32();
        let nm = null;
        if (k === 11 || k === 21)
            nm = t.add(8).readPointer().add(16).readPointer().readUtf16String();
        typeCache[key] = nm;
        return nm;
    } catch (e) { return null; }
}

let base = null, fnIsInvuln = null;
const rows = {};                  // signature -> tally
const ledger = {};                // target ptr -> reported damage vs hp lost
let total = 0, tick = 0;
// B.unitClasses is every class descending from ent.Unit, computed from
// hlboot.dat by the runner. Calling ent.Unit.isInvulnerable on something that
// is NOT a Unit would dispatch a method the object doesn't have.
const UNIT = {};
(B.unitClasses || []).forEach(function (c) { UNIT[c] = 1; });

function health(u) {
    try {
        const attr = u.add(B.Unit.attr).readPointer();
        if (!attr || attr.isNull() || attr.compare(ptr("0x10000")) <= 0) return null;
        return attr.add(B.UnitAttributes.health).readDouble();
    } catch (e) { return null; }
}

function onHit(dr) {
    try {
        const DR = OFF.DamageResult;
        const amount = dr.add(DR._amount).readDouble();
        const block = dr.add(B.DamageResult._block).readDouble();
        const blocker = hlStr(dr.add(B.DamageResult.blocker).readPointer());
        const effect = dr.add(B.DamageResult.effect).readS32();
        const kill = dr.add(DR._kill).readU8();
        const tgt = dr.add(DR.target).readPointer();

        let cls = null, kind = null, invuln = "?", hp = null;
        if (tgt && !tgt.isNull() && tgt.compare(ptr("0x10000")) > 0) {
            cls = typeName(tgt);
            if (cls && UNIT[cls]) {
                try { kind = hlStr(tgt.add(B.Unit.kind).readPointer()); } catch (e) {}
                hp = health(tgt);
                if (fnIsInvuln) {
                    try { invuln = fnIsInvuln(tgt) ? "YES" : "no"; }
                    catch (e) { invuln = "!"; }
                }
            }
        }
        total++;
        // ---- per-target ledger: is `_amount` pre- or post-mitigation? ----
        // Ground truth is the health the target actually loses. Summing every
        // reported _amount against that answers it directly:
        //   sum ~= hp lost          -> _amount is what landed (post-mitigation)
        //   sum >  hp lost          -> _amount is the pre-mitigation figure
        //   sum - block ~= hp lost  -> ...and _block is the reduction
        // Every dealer's hits go through this same function, so the sum is the
        // target's whole incoming damage, not just ours.
        if (hp !== null && tgt) {
            const tk = tgt.toString();
            let L = ledger[tk];
            if (!L || L.kind !== (kind || cls)) {
                // New target, or this address has been recycled for another
                // unit — either way start a fresh ledger rather than compare
                // one unit's damage against another's health.
                L = ledger[tk] = { kind: kind || cls || "?", hp0: hp, hp1: hp,
                                   sum: 0, blk: 0, n: 0, nulled: 0 };
            }
            // A rise in health is a heal or a respawn; it would make the target
            // look tankier than it is, so note it rather than silently absorb.
            if (hp > L.hp1 + 1) L.healed = (L.healed || 0) + (hp - L.hp1);
            L.hp1 = hp;
            L.sum += amount;
            L.blk += block;
            L.n++;
            if (blocker === "InvulnerableHit") L.nulled += amount;
        }
        // Bucket by everything that could distinguish a nullified hit, and keep
        // the target's health so a bucket where health never moves is visible.
        const sig = [kind || cls || "?", "invuln=" + invuln,
                     "blocker=" + (blocker === null ? "(null)" : JSON.stringify(blocker)),
                     "effect=" + effect,
                     "amount" + (amount > 0 ? ">0" : "=0"),
                     "block" + (block > 0 ? ">0" : "=0")].join("  ");
        let r = rows[sig];
        if (!r) r = rows[sig] = { n: 0, sum: 0, blk: 0, hp0: hp, hp1: hp, kills: 0 };
        r.n++; r.sum += amount; r.blk += block;
        if (hp !== null) { if (r.hp0 === null) r.hp0 = hp; r.hp1 = hp; }
        if (kill) r.kills++;
    } catch (e) {}
}

function report() {
    tick++;
    const keys = Object.keys(rows);
    if (!keys.length) {
        if (tick % 10 === 0) log("(no damage seen yet — hit something)");
        return;
    }
    log("");
    log("---- tick " + tick + "   " + total + " hits total ----");
    keys.sort();
    for (const k of keys) {
        const r = rows[k];
        // hp0 is the first health seen in this bucket, hp1 the latest. If they
        // are equal after many hits, nothing in this bucket is landing.
        let hp = "";
        if (r.hp0 !== null && r.hp1 !== null)
            hp = "   hp " + r.hp0.toFixed(0) + " -> " + r.hp1.toFixed(0) +
                 " (" + (r.hp1 - r.hp0).toFixed(0) + ")";
        log("  x" + String(r.n).padStart(4) + "  dmg=" + r.sum.toFixed(0).padStart(8) +
            "  blocked=" + r.blk.toFixed(0).padStart(8) +
            (r.kills ? "  kills=" + r.kills : "") + hp);
        log("        " + k);
    }

    // ---- pre- vs post-mitigation ----
    const lk = Object.keys(ledger).filter(function (k) { return ledger[k].n >= 5; });
    if (!lk.length) return;
    log("  -- reported damage vs actual health lost --");
    lk.sort(function (a, b) { return ledger[b].n - ledger[a].n; });
    for (const k of lk.slice(0, 6)) {
        const L = ledger[k];
        const lost = L.hp0 - L.hp1;
        // Immune damage is excluded: we already know that never lands, and
        // leaving it in would make every ratio look pre-mitigation.
        const landed = L.sum - L.nulled;
        const ratio = lost > 0 ? (landed / lost) : 0;
        let verdict = "?";
        if (lost > 0) {
            if (ratio > 0.95 && ratio < 1.05) verdict = "POST-mitigation (amount == what landed)";
            else if (ratio > 1.05) {
                const net = landed - L.blk;
                verdict = "PRE-mitigation by " + ((ratio - 1) * 100).toFixed(0) + "%"
                        + "   (amount-block)/lost=" + (net / lost).toFixed(3);
            } else verdict = "reports LESS than landed (other damage sources?)";
        }
        log("    " + L.kind.slice(0, 26).padEnd(26) + " x" + String(L.n).padStart(4) +
            "  reported=" + landed.toFixed(0).padStart(8) +
            "  blocked=" + L.blk.toFixed(0).padStart(7) +
            "  hpLost=" + lost.toFixed(0).padStart(8) +
            (L.healed ? "  (healed +" + L.healed.toFixed(0) + ")" : ""));
        log("        ratio=" + ratio.toFixed(3) + "  " + verdict);
    }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (B.fn.isInvulnerable != null) {
        try {
            fnIsInvuln = new NativeFunction(
                base.add(B.fn.isInvulnerable * 8).readPointer(), "uint8", ["pointer"]);
        } catch (e) { log("isInvulnerable unavailable: " + e); }
    }
    log("unit classes known: " + (B.unitClasses || []).length);

    const fi = DATA.count_targets && DATA.count_targets["ent.Unit.onInflictDamage"];
    if (fi == null) { log("!! onInflictDamage findex missing"); return; }
    Interceptor.attach(base.add(fi * 8).readPointer(), {
        onEnter: function () { onHit(this.context.rdx); }
    });
    log("hooked ent.Unit.onInflictDamage (findex " + fi + ")");
    log("Now: hit a normal mob, then hit a boss DURING its immune phase.");
    log("Compare the buckets — the one where hp never moves is the immune one.");
    setInterval(report, 1000);
}

// Deferred off script.load() on purpose. The functions_ptrs scan takes long
// enough to blow frida's load() handshake timeout when it runs synchronously —
// which is exactly how the first run of this probe failed, capturing nothing
// and never arming. meter_hook.js has carried the same setTimeout for the same
// reason; the probes just hadn't hit the limit before.
setTimeout(main, 150);
