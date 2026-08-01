// ledger_probe.js — READ THE GAME'S OWN DAMAGE LEDGER.
//
// ent.Unit keeps two arrays the client maintains itself:
//     combatDamageHistory @872 (hl.types.ArrayObj)
//     combatDamages       @880 (hl.types.ArrayObj)
// gated by ent.Unit.recordsDamage / ent.Foe.recordsDamage, and read back by
// ui.hud.UnitCombatMeter.get_combatDamages and ui.hud.MeterList — i.e. this is
// what drives Farever's OWN damage meter.
//
// That makes it an oracle rather than another sample. The open question is not
// "which field links a summon to its owner" (measured: summonOwner, checked for
// ent.Hero) but "how does the GAME credit a summon's damage". If the bee's hits
// land in its target's ledger credited to Brodr, pet damage already belongs to
// its owner by the game's own accounting and the meter is simply wrong. If they
// are credited to Summon_Bee as a separate contributor, merging them is a
// product decision instead.
//
// This is deliberately a DECODE-FIRST probe. The element type of those arrays
// is erased in the bytecode (hl.types.ArrayObj), and guessing at it is exactly
// how the inventory spike read a shader path as an item name. So this reports
// the runtime typeName of the entries plus a raw hexdump, and the offsets get
// computed offline from hlboot.dat by name. One round trip, no guessing.
//
// Everything runs inside the damage hook — game thread, so the recordsDamage
// call is legal. See TESTING.md "Thread safety".
//
// DATA + OFF + B are prepended by run_ledger.py.

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=base.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return base;}}}return null;}
function hlStr(p){try{if(!p||p.isNull())return null;const b=p.add(OFF.String.bytes).readPointer();if(b.isNull())return null;return b.readUtf16String();}catch(e){return null;}}

const typeCache = {};
function typeInfo(p) {
    try {
        if (!p || p.isNull() || p.compare(ptr("0x10000")) <= 0) return null;
        const t = p.readPointer();
        const key = t.toString();
        const hit = typeCache[key];
        if (hit !== undefined) return hit;
        const k = t.readU32();
        let nm = null;
        if (k === 11 || k === 21)
            nm = t.add(8).readPointer().add(16).readPointer().readUtf16String();
        const info = { kind: k, name: nm };
        typeCache[key] = info;
        return info;
    } catch (e) { return null; }
}
function typeName(p) { const i = typeInfo(p); return i ? i.name : null; }

const FOE = {}, UNIT = {};
(B.foeClasses || []).forEach(function (c) { FOE[c] = 1; });
(B.unitClasses || []).forEach(function (c) { UNIT[c] = 1; });

let base = null, localHero = null, localName = null;
let fnRecordsDamage = null, fnRecordsDamageFoe = null;
let armed = false, tick = 0, hits = 0;

// Distinct entry shapes seen in either array — the thing this run exists to
// find out. Keyed by runtime type so one line per shape, not per entry.
const shapes = {};
// Per-target ledger snapshots, so growth is visible: does a summon's hit push a
// NEW contributor into the array, or add to the owner's existing one?
const tracked = {};
let dumped = 0;

function arrInfo(owner, off) {
    try {
        const a = owner.add(off).readPointer();
        if (!a || a.isNull()) return null;
        const len = a.add(OFF.ArrayObj.length).readS32();
        const varr = a.add(OFF.ArrayObj.array).readPointer();
        if (!varr || varr.isNull()) return { len: len, entries: [] };
        const out = [];
        for (let i = 0; i < Math.min(len, 12); i++) {
            try { out.push(varr.add(OFF.ArrayObj.data + i * 8).readPointer()); }
            catch (e) { break; }
        }
        return { len: len, entries: out };
    } catch (e) { return null; }
}

// Record the shape of an entry once per distinct runtime type, with a raw dump
// so the offline decode can be sanity-checked against real bytes rather than
// against a hopeful reading of the class definition.
function noteShape(e, whichArray) {
    if (!e || e.isNull()) return;
    const ti = typeInfo(e);
    const key = whichArray + ":" + (ti ? (ti.name || ("kind" + ti.kind)) : "unreadable");
    if (shapes[key]) { shapes[key].n++; return; }
    let hex = "";
    if (dumped < 6) {
        try { hex = hexdump(e, { length: 96, header: false, ansi: false }); }
        catch (x) { hex = "(unreadable)"; }
        dumped++;
    }
    shapes[key] = { n: 1, kind: ti ? ti.kind : -1, name: ti ? ti.name : null, hex: hex };
    log("  NEW ENTRY SHAPE in " + whichArray + ": typeKind="
        + (ti ? ti.kind : "?") + " typeName=" + (ti ? ti.name : "?"));
    if (hex) log(hex);
}

function onInflict(dealer, dr) {
    try {
        const dcls = typeName(dealer);
        if (!dcls) return;

        // Only two dealers are interesting: the local hero, and a summon the
        // local hero owns. Everything else is other players' and mobs' damage,
        // which would bury the comparison.
        let role = null;
        if (localHero && dealer.equals(localHero)) role = "ME";
        else if (FOE[dcls] && B.Foe.summonOwner != null) {
            try {
                const so = dealer.add(B.Foe.summonOwner).readPointer();
                if (so && !so.isNull() && localHero && so.equals(localHero)) role = "MY_SUMMON";
            } catch (e) {}
        }
        if (!role) return;
        hits++;

        const tgt = dr.add(OFF.DamageResult.target).readPointer();
        if (!tgt || tgt.isNull()) return;
        const tcls = typeName(tgt);
        if (!tcls || !UNIT[tcls]) return;
        const tkind = hlStr(tgt.add(B.Unit.kind).readPointer()) || "?";
        const amount = dr.add(OFF.DamageResult._amount).readDouble();

        const cd = arrInfo(tgt, B.Unit.combatDamages);
        const ch = arrInfo(tgt, B.Unit.combatDamageHistory);
        if (cd) cd.entries.forEach(function (e) { noteShape(e, "combatDamages"); });
        if (ch) ch.entries.forEach(function (e) { noteShape(e, "combatDamageHistory"); });

        const k = tgt.toString();
        let T = tracked[k];
        if (!T || T.kind !== tkind) {
            T = tracked[k] = { kind: tkind, me: 0, summon: 0, meDmg: 0, summonDmg: 0,
                               cdLen: -1, chLen: -1, records: "?" };
            // Dispatch by class. A raw findex call has no vtable behind it, so
            // calling ent.Unit's implementation on a Foe silently runs the
            // wrong body — that is what made the first run report "no" for
            // every rift demon.
            const fnRec = (FOE[tcls] && fnRecordsDamageFoe) ? fnRecordsDamageFoe
                                                            : fnRecordsDamage;
            if (fnRec) {
                try { T.records = (fnRec(tgt) ? "YES" : "no")
                                  + (fnRec === fnRecordsDamageFoe ? "(Foe)" : "(Unit)"); }
                catch (x) { T.records = "THREW"; }
            }
        }
        if (role === "ME") { T.me++; T.meDmg += amount; }
        else { T.summon++; T.summonDmg += amount; }
        if (cd) T.cdLen = cd.len;
        if (ch) T.chLen = ch.len;
    } catch (e) {}
}

function report() {
    tick++;
    if (!armed) {
        if (tick % 4 === 0) log("[waiting] localHero not latched yet — nothing recorded.");
        return;
    }
    log("");
    log("---- tick " + tick + "   my hits (me+summon)=" + hits + "   me=" + (localName || "?") + " ----");

    // THE HERO'S OWN LEDGER. The first run sampled the VICTIM's arrays, which
    // read length 0 everywhere. But ui.hud.MeterList and ui.hud.UnitCombatMeter
    // both expose get_hero alongside get_combatDamages, and ui.hud.MeterLine —
    // the row the game's meter draws — carries {count, amount, skill, target,
    // targetUnit, hero:ent.Hero}. So the game reads this off the HERO, and a
    // row has nowhere to name a summon: only `hero`. If these arrays are
    // populated, they are the oracle; if the hero's are empty too, the client
    // genuinely has no ledger and summonOwner remains the only path.
    if (localHero) {
        const hcd = arrInfo(localHero, B.Unit.combatDamages);
        const hch = arrInfo(localHero, B.Unit.combatDamageHistory);
        log("  -- MY OWN ledger: combatDamages.len="
            + (hcd ? hcd.len : "(null array)")
            + "  combatDamageHistory.len=" + (hch ? hch.len : "(null array)"));
        if (hcd) hcd.entries.forEach(function (e) { noteShape(e, "hero.combatDamages"); });
        if (hch) hch.entries.forEach(function (e) { noteShape(e, "hero.combatDamageHistory"); });
    }

    const sk = Object.keys(shapes);
    if (!sk.length) {
        log("  ledger arrays EMPTY or unreadable so far. Hit a foe, with your summon up.");
    } else {
        log("  -- entry shapes seen (this is the decode target) --");
        for (const k of sk.sort())
            log("     " + k + "   x" + shapes[k].n + "  (typeKind " + shapes[k].kind + ")");
    }

    const tk = Object.keys(tracked);
    if (tk.length) {
        log("  -- per-target: my damage vs my summon's, and the ledger's length --");
        tk.sort(function (a, b) {
            return (tracked[b].me + tracked[b].summon) - (tracked[a].me + tracked[a].summon);
        });
        for (const k of tk.slice(0, 8)) {
            const T = tracked[k];
            log("     " + T.kind.slice(0, 28).padEnd(28)
                + " recordsDamage=" + T.records
                + "  meHits=" + T.me + "(" + T.meDmg.toFixed(0) + ")"
                + "  summonHits=" + T.summon + "(" + T.summonDmg.toFixed(0) + ")"
                + "  combatDamages.len=" + T.cdLen
                + "  history.len=" + T.chLen);
        }
        log("  NOTE: if combatDamages.len stays 1 while BOTH me and my summon are");
        log("        hitting, the game is crediting us as ONE contributor.");
        log("        If it goes to 2, the game counts the summon separately.");
    }
}

function refreshLocalHero() {
    if (!base) return;
    for (const nm in DATA.funcs) {
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") {
                localHero = h;
                localName = hlStr(h.add(B.Hero.name).readPointer());
                return;
            }
        } catch (e) {}
    }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    log("combatDamages@" + B.Unit.combatDamages
        + "  combatDamageHistory@" + B.Unit.combatDamageHistory);

    if (B.fn.recordsDamage != null) {
        try {
            fnRecordsDamage = new NativeFunction(
                base.add(B.fn.recordsDamage * 8).readPointer(), "uint8", ["pointer"]);
        } catch (e) { log("recordsDamage unavailable: " + e); }
    }
    if (B.fn.recordsDamageFoe != null) {
        try {
            fnRecordsDamageFoe = new NativeFunction(
                base.add(B.fn.recordsDamageFoe * 8).readPointer(), "uint8", ["pointer"]);
        } catch (e) { log("ent.Foe.recordsDamage unavailable: " + e); }
    }

    const inf = DATA.count_targets["ent.Unit.onInflictDamage"];
    if (inf == null) { log("!! onInflictDamage findex missing"); return; }
    Interceptor.attach(base.add(inf * 8).readPointer(), {
        onEnter: function () { onInflict(this.context.rcx, this.context.rdx); }
    });
    log("hooked ent.Unit.onInflictDamage (findex " + inf + ")");

    const camFi = DATA.cam_targets && DATA.cam_targets["client.BaseCamera.postUpdate"];
    if (camFi == null) { log("!! postUpdate findex missing — cannot latch hero"); return; }
    let due = true;
    setInterval(function () { due = true; }, 3000);
    Interceptor.attach(base.add(camFi * 8).readPointer(), {
        onEnter: function () {
            if (!due) return;
            due = false;
            refreshLocalHero();
            if (localHero && !armed) {
                armed = true;
                log(">>> PROBE ARMED <<< hero=" + (localName || "?")
                    + "  reading the game's own damage ledger off every foe you hit.");
            }
        }
    });
    setInterval(report, 5000);
}

setTimeout(main, 150);
