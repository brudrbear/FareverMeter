// summon_probe.js — SUMMON/PET DAMAGE ATTRIBUTION SPIKE.
//
// The meter drops every hit whose dealer is not an ent.Hero (meter_hook.js:
// `if (typeName(dealer) !== "ent.Hero") return;`), so a summoned imp, a totem
// or a pet contributes nothing to its owner's parse. This probe answers, in
// order, the two questions that decide whether that is fixable:
//
//   Q1  Does ent.Unit.onInflictDamage fire on the CLIENT at all when the
//       dealer is a summon? Summons are server-simulated, and heals already
//       proved that a whole damage pipeline can be invisible client-side. So
//       this hooks the same function the meter does but tallies EVERY dealer
//       class instead of filtering to ent.Hero. If no ent.Foe dealer ever
//       appears, no amount of ownership plumbing will help and the fix has to
//       move to the receive side.
//
//   Q2  If it does fire, which field links the summon back to its owner?
//       hlboot.dat offers six candidates and they are not interchangeable:
//         A  ent.Foe.summonOwner        (ent.GameObject)
//         B  ent.GameObject.ownerPlayer (st.Player, @16 — hxbit net owner)
//         C  ent.Foe.summonSourceSkill  -> BaseSkill.owner / .ownerPlayer
//         D  DamageResult.baseSkill     -> BaseSkill.owner / .ownerPlayer
//         E  DamageResult.serverSource  (ent.GameObject)
//         F  ent.Foe.get_summonHero()   (findex call, ent.Hero)
//       All six are read per distinct dealer and printed side by side. The one
//       that names the right hero on EVERY summon is the one to ship.
//
//   Q3  Is the damage already counted somewhere? If a summon's skill also
//       arrives with the hero as dealer, attributing the summon hit too would
//       double-count. Buckets are keyed by (dealer class, skill), so a skill
//       appearing under both is visible at a glance.
//
// A second hook on ent.Unit.onReceiveDamage (victim side) is the fallback
// measurement for Q1: if the inflict side is silent for summons but the
// receive side sees the hits, that is where the feature has to be built.
//
// HL calls (isSummon / get_summonHero) happen ONLY inside the damage hooks and
// postUpdate — all game thread: the same calls from a setInterval killed the
// game.
//
// DATA + OFF + B are prepended by run_summon.py.

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=base.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return base;}}}return null;}
function hlStr(p){try{if(!p||p.isNull())return null;const b=p.add(OFF.String.bytes).readPointer();if(b.isNull())return null;return b.readUtf16String();}catch(e){return null;}}

const typeCache = {};
function typeName(p) {
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
        typeCache[key] = nm;
        return nm;
    } catch (e) { return null; }
}

const FOE = {};   // classes descending from ent.Foe — where Foe.* offsets are valid
const UNIT = {};  // classes descending from ent.Unit — where Unit.kind is valid
(B.foeClasses || []).forEach(function (c) { FOE[c] = 1; });
(B.unitClasses || []).forEach(function (c) { UNIT[c] = 1; });

let base = null, localHero = null, localName = null;
let fnIsSummon = null, fnSummonHero = null;
let armed = false, tick = 0;
let inflictTotal = 0, receiveTotal = 0, inflictNonHero = 0;
const inflictRows = {};    // "dealerClass | dealerKind | skill" -> tally
const receiveRows = {};    // "sourceClass | skill" -> tally
const dealers = {};        // dealer ptr -> resolved ownership evidence (cached)
let dealerCount = 0;

// ---- ownership candidates, read once per distinct dealer pointer ----
// Cached because the damage hook has to stay near-free, and because the HL
// calls below should happen a handful of times, not thousands.
function describeOwner(p) {
    if (!p) return "-";
    const cls = typeName(p);
    if (!cls) return "(null)";
    let nm = null;
    if (cls === "ent.Hero") nm = hlStr(p.add(B.Hero.name).readPointer());
    else if (cls === "st.Player") nm = hlStr(p.add(B.Player.name).readPointer());
    else if (UNIT[cls]) nm = hlStr(p.add(B.Unit.kind).readPointer());
    return cls + (nm ? "(" + nm + ")" : "");
}

function skillOwners(bs, tag) {
    const out = [];
    if (!bs || bs.isNull()) return [tag + ".owner=-", tag + ".ownerPlayer=-"];
    try { out.push(tag + ".owner=" + describeOwner(bs.add(B.BaseSkill.owner).readPointer())); }
    catch (e) { out.push(tag + ".owner=!"); }
    try { out.push(tag + ".ownerPlayer=" + describeOwner(bs.add(B.BaseSkill.ownerPlayer).readPointer())); }
    catch (e) { out.push(tag + ".ownerPlayer=!"); }
    return out;
}

function evidence(dealer, dr, cls) {
    const k = dealer.toString();
    let e = dealers[k];
    if (e && e.cls === cls) return e;
    e = dealers[k] = { cls: cls, n: 0, dmg: 0, lines: [], kind: null,
                       summonish: false };
    dealerCount++;
    const L = e.lines;
    try { if (UNIT[cls]) e.kind = hlStr(dealer.add(B.Unit.kind).readPointer()); } catch (x) {}

    // A — the field the world sweep already uses to tell a pet from a mob
    if (FOE[cls] && B.Foe.summonOwner != null) {
        try {
            const so = dealer.add(B.Foe.summonOwner).readPointer();
            if (so && !so.isNull()) e.summonish = true;
            L.push("A summonOwner=" + describeOwner(so));
        } catch (x) { L.push("A summonOwner=!"); }
        try { L.push("  persistantSummon=" + dealer.add(B.Foe.persistantSummon).readU8()); }
        catch (x) {}
    } else L.push("A summonOwner=(not an ent.Foe)");

    // B — hxbit network owner, present on EVERY st.BaseState
    try { L.push("B ownerPlayer=" + describeOwner(dealer.add(B.GameObject.ownerPlayer).readPointer())); }
    catch (x) { L.push("B ownerPlayer=!"); }

    // C — the skill that spawned this summon, and who owned that
    if (FOE[cls] && B.Foe.summonSourceSkill != null) {
        try {
            const ss = dealer.add(B.Foe.summonSourceSkill).readPointer();
            if (ss && !ss.isNull()) {
                L.push("C summonSourceSkill=" + (hlStr(ss.add(B.BaseSkill.kind).readPointer()) || "?"));
                skillOwners(ss, "C  src").forEach(function (s) { L.push("  " + s); });
            } else L.push("C summonSourceSkill=(null)");
        } catch (x) { L.push("C summonSourceSkill=!"); }
    }

    // D — the skill of THIS hit
    try {
        const bs = dr.add(OFF.DamageResult.baseSkill).readPointer();
        if (bs && !bs.isNull()) {
            L.push("D hitSkill=" + (hlStr(bs.add(B.BaseSkill.kind).readPointer()) || "?"));
            skillOwners(bs, "D  hit").forEach(function (s) { L.push("  " + s); });
        } else L.push("D hitSkill=(null)");
    } catch (x) { L.push("D hitSkill=!"); }

    // E — DamageResult's own idea of where the hit came from
    try { L.push("E serverSource=" + describeOwner(dr.add(OFF.DamageResult.serverSource).readPointer())); }
    catch (x) { L.push("E serverSource=!"); }

    // F — the game's own accessors. Guarded: ent.Foe.shouldShowBossInfo threw
    // when called as (foe)->bool because it takes more than `this`, so a throw
    // here is a result, not a bug (it is reported, not swallowed).
    if (FOE[cls]) {
        if (fnIsSummon) {
            try {
                const is = fnIsSummon(dealer) ? 1 : 0;
                if (is) e.summonish = true;
                L.push("F isSummon()=" + (is ? "YES" : "no"));
            } catch (x) { L.push("F isSummon() THREW: " + x); }
        }
        if (fnSummonHero) {
            try { L.push("F get_summonHero()=" + describeOwner(fnSummonHero(dealer))); }
            catch (x) { L.push("F get_summonHero() THREW: " + x); }
        }
    }
    return e;
}

function onInflict(dealer, dr) {
    try {
        const cls = typeName(dealer);
        if (!cls) return;
        const amount = dr.add(OFF.DamageResult._amount).readDouble();
        let skill = "?";
        try {
            const bs = dr.add(OFF.DamageResult.baseSkill).readPointer();
            if (bs && !bs.isNull()) skill = hlStr(bs.add(B.BaseSkill.kind).readPointer()) || "?";
        } catch (e) {}
        inflictTotal++;

        let kind = null;
        if (cls !== "ent.Hero") {
            inflictNonHero++;
            const e = evidence(dealer, dr, cls);
            e.n++; e.dmg += amount;
            kind = e.kind;
        } else if (UNIT[cls]) {
            try { kind = hlStr(dealer.add(B.Hero.name).readPointer()); } catch (x) {}
        }
        const sig = cls + "  " + (kind || "-") + "  " + skill;
        let r = inflictRows[sig];
        if (!r) r = inflictRows[sig] = { n: 0, sum: 0 };
        r.n++; r.sum += amount;
    } catch (e) {}
}

// Victim side. Only a tally — this exists to answer "if the inflict hook is
// blind to summons, does the receive hook see them?", so it buckets by who the
// DamageResult says the hit came from rather than by the victim.
function onReceive(victim, dr) {
    try {
        const amount = dr.add(OFF.DamageResult._amount).readDouble();
        let src = "-", skill = "?", owner = "-";
        try { src = typeName(dr.add(OFF.DamageResult.serverSource).readPointer()) || "(null)"; } catch (e) {}
        try {
            const bs = dr.add(OFF.DamageResult.baseSkill).readPointer();
            if (bs && !bs.isNull()) {
                skill = hlStr(bs.add(B.BaseSkill.kind).readPointer()) || "?";
                owner = typeName(bs.add(B.BaseSkill.owner).readPointer()) || "(null)";
            }
        } catch (e) {}
        receiveTotal++;
        const sig = "src=" + src + "  skillOwner=" + owner + "  " + skill;
        let r = receiveRows[sig];
        if (!r) r = receiveRows[sig] = { n: 0, sum: 0 };
        r.n++; r.sum += amount;
    } catch (e) {}
}

function report() {
    tick++;
    if (!armed) {
        if (tick % 5 === 0)
            log("[waiting] localHero not latched yet (postUpdate hasn't run, or "
                + "you're not in a zone). Nothing is being recorded.");
        return;
    }
    log("");
    log("---- tick " + tick + "   inflict=" + inflictTotal + " (non-hero "
        + inflictNonHero + ")   receive=" + receiveTotal
        + "   me=" + (localName || "?") + " ----");

    const ik = Object.keys(inflictRows).sort();
    if (!ik.length) log("  onInflictDamage: NOTHING YET — hit something.");
    else {
        log("  -- onInflictDamage, by dealer class / kind / skill --");
        for (const k of ik) {
            const r = inflictRows[k];
            log("   x" + String(r.n).padStart(4) + "  dmg=" + r.sum.toFixed(0).padStart(9) + "   " + k);
        }
    }

    if (dealerCount) {
        // SUMMONS ARE NEVER TRUNCATED. The first version of this report showed
        // the 8 busiest non-hero dealers, and a crowded zone full of wolves
        // pushed a potion-summoned imp — the whole point of the round — clean
        // off the bottom. Ordinary mobs are the noise here, so they collapse to
        // one line and anything summon-shaped prints in full, however rare.
        const dk = Object.keys(dealers);
        const sum = dk.filter(function (k) { return dealers[k].summonish; });
        const mob = dk.filter(function (k) { return !dealers[k].summonish; });
        sum.sort(function (a, b) { return dealers[b].n - dealers[a].n; });
        log("  -- SUMMON-SHAPED DEALERS (" + sum.length + "): ownership candidates --");
        if (!sum.length) log("     none yet — nothing with a summonOwner or isSummon()=YES has dealt damage");
        for (const k of sum) {
            const e = dealers[k];
            log("   [" + e.cls + " " + (e.kind || "?") + "]  x" + e.n
                + "  dmg=" + e.dmg.toFixed(0));
            for (const l of e.lines) log("      " + l);
        }
        const kinds = {};
        mob.forEach(function (k) { kinds[dealers[k].kind || dealers[k].cls] = 1; });
        log("  -- plain (non-summon) dealers: " + mob.length + " objects, kinds: "
            + Object.keys(kinds).sort().join(", "));
    }

    const rk = Object.keys(receiveRows).sort();
    if (rk.length) {
        log("  -- onReceiveDamage (fallback path), by source --");
        for (const k of rk.slice(0, 12)) {
            const r = receiveRows[k];
            log("   x" + String(r.n).padStart(4) + "  dmg=" + r.sum.toFixed(0).padStart(9) + "   " + k);
        }
    }
}

// Runs on the GAME thread (inside client.BaseCamera.postUpdate) — the only
// legal place for the getHero calls.
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
    log("table base " + base + "   foeClasses=" + (B.foeClasses || []).length
        + "  unitClasses=" + (B.unitClasses || []).length);

    if (B.fn.isSummon != null) {
        try {
            fnIsSummon = new NativeFunction(
                base.add(B.fn.isSummon * 8).readPointer(), "uint8", ["pointer"]);
        } catch (e) { log("isSummon unavailable: " + e); }
    }
    if (B.fn.get_summonHero != null) {
        try {
            fnSummonHero = new NativeFunction(
                base.add(B.fn.get_summonHero * 8).readPointer(), "pointer", ["pointer"]);
        } catch (e) { log("get_summonHero unavailable: " + e); }
    }

    const inf = DATA.count_targets["ent.Unit.onInflictDamage"];
    const rcv = DATA.count_targets["ent.Unit.onReceiveDamage"];
    if (inf == null) { log("!! onInflictDamage findex missing"); return; }
    Interceptor.attach(base.add(inf * 8).readPointer(), {
        onEnter: function () { onInflict(this.context.rcx, this.context.rdx); }
    });
    log("hooked ent.Unit.onInflictDamage (findex " + inf + ")");
    if (rcv != null) {
        Interceptor.attach(base.add(rcv * 8).readPointer(), {
            onEnter: function () { onReceive(this.context.rcx, this.context.rdx); }
        });
        log("hooked ent.Unit.onReceiveDamage (findex " + rcv + ")");
    }

    // Arming. A probe that attached but never latched the hero produces the
    // same empty log as a probe watching a fight nobody started — so the
    // readiness line waits for the hero, and says so in a form the host greps.
    const camFi = DATA.cam_targets && DATA.cam_targets["client.BaseCamera.postUpdate"];
    if (camFi == null) { log("!! postUpdate findex missing — cannot latch hero"); return; }
    let due = true;
    setInterval(function () { due = true; }, 3000);   // flag only; no HL calls here
    Interceptor.attach(base.add(camFi * 8).readPointer(), {
        onEnter: function () {
            if (!due) return;
            due = false;
            refreshLocalHero();
            if (localHero && !armed) {
                armed = true;
                log(">>> PROBE ARMED <<< hero=" + (localName || "?")
                    + "  recording every damage dealer, not just heroes.");
            }
        }
    });
    // 5s, not 2s: a busy world zone puts ~15 other players and ~30 mob kinds in
    // the inflict table, and the first run wrote 21k lines in four minutes.
    setInterval(report, 5000);
}

// Deferred off script.load() — the functions_ptrs scan is slower than frida's
// load() handshake timeout when run synchronously.
setTimeout(main, 150);
