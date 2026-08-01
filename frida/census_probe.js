// census_probe.js — EVERY SUMMON AT BIRTH, plus the damage it goes on to deal.
//
// The completeness question for summon attribution is NOT "which summon kinds
// exist in the CDB" — a list of kinds cannot tell you whether the ownership
// field is populated. It is "is ent.Foe.summonOwner always set, for every
// summon a hero actually produces". That is answerable by census rather than by
// sampling, because there is exactly one field and one writer:
//
//     ent.Foe.set_summonOwner   (the setter)
//     ent.Foe.initSummonedFoe   (the spawn path)
//
// Hooking those catches EVERY summon the moment it is created, whether or not
// it ever swings at anything. Cross-checked against the summon-shaped damage
// dealers from the same session, that gives:
//
//   * born-with-owner  and later dealt damage   -> attributable, the good case
//   * born WITHOUT an owner, then dealt damage  -> a hole in the ownership path
//   * dealt damage but never seen born          -> a hole in the census itself
//     (spawned before the probe attached, or created off this path entirely)
//
// The third bucket is the one that matters: it is the only way to discover a
// summon that arrives by a route nobody thought to look for.
//
// Also watches for the case most likely to ship a bug: a summon OUTLIVING its
// owner. summonOwner is a raw pointer to an ent.Hero, so if that hero is freed
// and its allocation recycled, reading a name off it yields garbage rather than
// null. Every census entry is re-validated on a timer by re-reading the owner's
// runtime type — a summon whose owner stops being an ent.Hero is exactly that
// failure, and it is reported loudly.
//
// DATA + OFF + B are prepended by run_census.py.

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

const FOE = {}, UNIT = {};
(B.foeClasses || []).forEach(function (c) { FOE[c] = 1; });
(B.unitClasses || []).forEach(function (c) { UNIT[c] = 1; });

let base = null, localHero = null, localName = null, armed = false, tick = 0;
const born = {};        // summon ptr -> { kind, ownerCls, ownerName, t, via }
const dealt = {};       // summon ptr -> { kind, hits, dmg, sawBirth }
const alarms = [];      // owner-pointer validity failures, the dangling case
let births = 0, ownerless = 0;

function describe(p) {
    if (!p || p.isNull()) return { cls: null, name: null };
    const cls = typeName(p);
    let nm = null;
    try {
        if (cls === "ent.Hero") nm = hlStr(p.add(B.Hero.name).readPointer());
        else if (UNIT[cls]) nm = hlStr(p.add(B.Unit.kind).readPointer());
    } catch (e) {}
    return { cls: cls, name: nm };
}

function noteBirth(foe, owner, via) {
    try {
        if (!foe || foe.isNull()) return;
        const cls = typeName(foe);
        if (!cls || !FOE[cls]) return;
        let kind = null;
        try { kind = hlStr(foe.add(B.Unit.kind).readPointer()); } catch (e) {}
        // The setter is called with the owner in rdx; initSummonedFoe may not
        // carry one, so fall back to reading the field straight off the foe.
        let o = owner;
        if ((!o || o.isNull()) && B.Foe.summonOwner != null) {
            try { o = foe.add(B.Foe.summonOwner).readPointer(); } catch (e) { o = null; }
        }
        const d = describe(o);
        const k = foe.toString();
        born[k] = { kind: kind || "?", ownerCls: d.cls, ownerName: d.name,
                    t: Date.now(), via: via };
        births++;
        if (!d.cls) ownerless++;
        log("  BIRTH  " + (kind || "?") + "   owner=" + (d.cls || "(none)")
            + (d.name ? "(" + d.name + ")" : "") + "   via " + via);
    } catch (e) {}
}

// ---- conservation ledger, per target ----
// The point of a training dummy: reported damage can be checked against the
// health the target actually loses. If counting summon damage is correct, then
//     (my damage + my summon's damage + everyone else's) ~= health lost
// and if we were double-counting it, the ratio would sit above 1. This is the
// same technique that settled pre- vs post-mitigation; the caveat from that
// work applies unchanged — health is sampled at onEnter, i.e. BEFORE the hit
// being recorded lands, so hpLost always misses the final hit and the ratio
// approaches 1 from above as the sample grows.
const cons = {};

function health(u) {
    try {
        const attr = u.add(B.Unit.attr).readPointer();
        if (!attr || attr.isNull() || attr.compare(ptr("0x10000")) <= 0) return null;
        return attr.add(B.UnitAttributes.health).readDouble();
    } catch (e) { return null; }
}

function noteConservation(dealer, dcls, dr) {
    try {
        const tgt = dr.add(OFF.DamageResult.target).readPointer();
        if (!tgt || tgt.isNull()) return;
        const tcls = typeName(tgt);
        if (!tcls || !UNIT[tcls]) return;
        const hp = health(tgt);
        if (hp === null) return;
        const amount = dr.add(OFF.DamageResult._amount).readDouble();
        // InvulnerableHit is damage the target never takes — counting it here
        // would make every ratio look inflated. Measured on Ratsar's immune
        // phase; see TESTING.md.
        let nulled = false;
        try {
            const blk = hlStr(dr.add(OFF.DamageResult.blocker).readPointer());
            nulled = (blk === "InvulnerableHit");
        } catch (e) {}

        let role = "other";
        if (localHero && dealer.equals(localHero)) role = "me";
        else if (FOE[dcls] && B.Foe.summonOwner != null) {
            try {
                const so = dealer.add(B.Foe.summonOwner).readPointer();
                if (so && !so.isNull() && localHero && so.equals(localHero)) role = "summon";
            } catch (e) {}
        }

        let kind = null;
        try { kind = hlStr(tgt.add(B.Unit.kind).readPointer()); } catch (e) {}
        const tk = tgt.toString();
        let C = cons[tk];
        if (!C || C.kind !== (kind || tcls)) {
            C = cons[tk] = { kind: kind || tcls, hp0: hp, hp1: hp, healed: 0,
                             me: 0, summon: 0, other: 0, nulled: 0, n: 0 };
        }
        // A rise is a heal or a dummy resetting — record it rather than let it
        // silently make the target look tankier than it is.
        if (hp > C.hp1 + 1) C.healed += (hp - C.hp1);
        C.hp1 = hp;
        C.n++;
        if (nulled) { C.nulled += amount; return; }
        if (role === "me") C.me += amount;
        else if (role === "summon") C.summon += amount;
        else C.other += amount;
    } catch (e) {}
}

function onInflict(dealer, dr) {
    try {
        const cls = typeName(dealer);
        if (!cls) return;
        noteConservation(dealer, cls, dr);
        if (!FOE[cls]) return;
        if (B.Foe.summonOwner == null) return;
        const so = dealer.add(B.Foe.summonOwner).readPointer();
        if (!so || so.isNull()) return;          // an ordinary mob
        const amount = dr.add(OFF.DamageResult._amount).readDouble();
        const k = dealer.toString();
        let D = dealt[k];
        if (!D) {
            let kind = null;
            try { kind = hlStr(dealer.add(B.Unit.kind).readPointer()); } catch (e) {}
            D = dealt[k] = { kind: kind || "?", hits: 0, dmg: 0,
                             sawBirth: !!born[k],
                             ownerCls: typeName(so) };
            if (!D.sawBirth)
                log("  !! DEALT WITHOUT A RECORDED BIRTH: " + D.kind
                    + "  owner=" + D.ownerCls
                    + "  (spawned before the probe attached, or created off "
                    + "set_summonOwner/initSummonedFoe entirely)");
        }
        D.hits++; D.dmg += amount;
    } catch (e) {}
}

// The dangling-owner check. Plain pointer reads only — safe off the game
// thread. A census entry whose owner STOPS being an ent.Hero means the pointer
// went stale, which is the failure that would put a garbage name on the meter.
function validate() {
    for (const k in born) {
        const b = born[k];
        if (b.ownerCls !== "ent.Hero" || b.dead) continue;
        try {
            const foe = ptr(k);
            const o = foe.add(B.Foe.summonOwner).readPointer();
            const nowCls = typeName(o);
            if (nowCls !== "ent.Hero") {
                b.dead = true;
                const msg = "OWNER POINTER WENT STALE: " + b.kind
                          + "  was ent.Hero(" + b.ownerName + "), now " + nowCls;
                alarms.push(msg);
                log("  !! " + msg);
                continue;
            }
            const nm = hlStr(o.add(B.Hero.name).readPointer());
            if (nm !== b.ownerName) {
                b.dead = true;
                const msg = "OWNER NAME CHANGED (recycled allocation?): " + b.kind
                          + "  " + b.ownerName + " -> " + nm;
                alarms.push(msg);
                log("  !! " + msg);
            }
        } catch (e) {
            b.dead = true;
            alarms.push("owner unreadable for " + b.kind + " (freed)");
        }
    }
}

function report() {
    tick++;
    if (!armed) {
        if (tick % 4 === 0) log("[waiting] localHero not latched yet.");
        return;
    }
    validate();
    log("");
    log("---- tick " + tick + "   births=" + births + " (ownerless " + ownerless
        + ")   damage-dealing summons=" + Object.keys(dealt).length
        + "   me=" + (localName || "?") + " ----");

    const bk = Object.keys(born);
    if (bk.length) {
        const byKind = {};
        for (const k of bk) {
            const b = born[k];
            const sig = b.kind + "  owner=" + (b.ownerCls || "(none)")
                      + (b.ownerName ? "(" + b.ownerName + ")" : "")
                      + "  via=" + b.via;
            byKind[sig] = (byKind[sig] || 0) + 1;
        }
        log("  -- summons born this session --");
        for (const s of Object.keys(byKind).sort())
            log("     x" + byKind[s] + "  " + s);
    }

    const dk = Object.keys(dealt);
    if (dk.length) {
        log("  -- summons that DEALT damage --");
        dk.sort(function (a, b) { return dealt[b].dmg - dealt[a].dmg; });
        for (const k of dk.slice(0, 12)) {
            const D = dealt[k];
            log("     " + D.kind.padEnd(24) + " x" + String(D.hits).padStart(4)
                + "  dmg=" + D.dmg.toFixed(0).padStart(8)
                + "  owner=" + D.ownerCls
                + (D.sawBirth ? "  [birth recorded]" : "  [NO BIRTH RECORD]"));
        }
        const missing = dk.filter(function (k) { return !dealt[k].sawBirth; }).length;
        log("  COVERAGE: " + (dk.length - missing) + "/" + dk.length
            + " damage-dealing summons had their birth recorded."
            + (missing ? "  <-- investigate the " + missing + " that did not."
                       : "  Complete for this session."));
    }

    // The hero's own ledger. ui.hud.MeterList/UnitCombatMeter expose get_hero
    // beside get_combatDamages, so the game reads these off the HERO — the
    // first ledger run sampled the victim's and found them empty everywhere.
    if (localHero) {
        try {
            const a = localHero.add(B.Unit.combatDamages).readPointer();
            const h = localHero.add(B.Unit.combatDamageHistory).readPointer();
            const al = (a && !a.isNull()) ? a.add(OFF.ArrayObj.length).readS32() : "(null)";
            const hl = (h && !h.isNull()) ? h.add(OFF.ArrayObj.length).readS32() : "(null)";
            log("  -- MY OWN ledger: combatDamages.len=" + al
                + "  combatDamageHistory.len=" + hl);
        } catch (e) { log("  -- MY OWN ledger: unreadable (" + e + ")"); }
    }

    // ---- conservation ----
    const ck = Object.keys(cons).filter(function (k) { return cons[k].n >= 8; });
    if (ck.length) {
        log("  -- reported damage vs health actually lost --");
        ck.sort(function (a, b) { return cons[b].n - cons[a].n; });
        for (const k of ck.slice(0, 5)) {
            const C = cons[k];
            const lost = (C.hp0 - C.hp1) + C.healed;
            const total = C.me + C.summon + C.other;
            const r = lost > 0 ? (total / lost) : 0;
            const rNoSummon = lost > 0 ? ((C.me + C.other) / lost) : 0;
            log("     " + C.kind.slice(0, 24).padEnd(24) + " x" + String(C.n).padStart(4)
                + "  me=" + C.me.toFixed(0) + "  summon=" + C.summon.toFixed(0)
                + "  other=" + C.other.toFixed(0)
                + "  hpLost=" + lost.toFixed(0)
                + (C.nulled ? "  (immune " + C.nulled.toFixed(0) + ")" : ""));
            log("        ratio WITH summon=" + r.toFixed(3)
                + "   WITHOUT summon=" + rNoSummon.toFixed(3)
                + "   -> " + (C.summon > 0
                    ? (Math.abs(r - 1) < Math.abs(rNoSummon - 1)
                        ? "counting the summon fits health loss BETTER"
                        : "counting the summon OVERSHOOTS - double count?")
                    : "no summon damage on this target yet"));
        }
    }

    if (alarms.length)
        log("  -- STALE-OWNER ALARMS (" + alarms.length + ") -- " + alarms.slice(-3).join(" | "));
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

    // set_summonOwner(this=foe, owner) — rcx/rdx.
    if (B.fn.set_summonOwner != null) {
        Interceptor.attach(base.add(B.fn.set_summonOwner * 8).readPointer(), {
            onEnter: function () { noteBirth(this.context.rcx, this.context.rdx, "set_summonOwner"); }
        });
        log("hooked ent.Foe.set_summonOwner (findex " + B.fn.set_summonOwner + ")");
    }
    // initSummonedFoe — the spawn path. Its argument list is not known from the
    // bytecode (bodies are not parsed), so the owner is read off the field
    // rather than guessed out of a register.
    if (B.fn.initSummonedFoe != null) {
        Interceptor.attach(base.add(B.fn.initSummonedFoe * 8).readPointer(), {
            onEnter: function () { noteBirth(this.context.rcx, null, "initSummonedFoe"); }
        });
        log("hooked ent.Foe.initSummonedFoe (findex " + B.fn.initSummonedFoe + ")");
    }

    const inf = DATA.count_targets["ent.Unit.onInflictDamage"];
    if (inf == null) { log("!! onInflictDamage findex missing"); return; }
    Interceptor.attach(base.add(inf * 8).readPointer(), {
        onEnter: function () { onInflict(this.context.rcx, this.context.rdx); }
    });
    log("hooked ent.Unit.onInflictDamage (findex " + inf + ")");

    const camFi = DATA.cam_targets && DATA.cam_targets["client.BaseCamera.postUpdate"];
    if (camFi == null) { log("!! postUpdate findex missing"); return; }
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
                    + "  censusing every summon at birth. Summon things.");
            }
        }
    });
    setInterval(report, 5000);
}

setTimeout(main, 150);
