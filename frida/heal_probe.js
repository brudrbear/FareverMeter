// heal_probe.js — RAW HEAL / OVERHEAL DISCOVERY. Throwaway.
//
// The shipping meter infers healing from a RISE in the target's replicated
// health (meter_hook.js "---- healing ----"), so a heal on a target that is
// already at full health counts as zero. That measures who healed fastest,
// not who healed hardest — useless for tuning a healing build.
//
// This probe answers two questions, in order:
//
//   1. Does ANY client-side function see the heal AMOUNT? The heal pipeline
//      has a dozen entry points (receiveHeal / computeHeal / evalHeal /
//      *HealEval / applyHeal / rpcDisplayHeal / EffectsFeed.displayHeal).
//      The previous session's note says the server-side ones never run on a
//      client and HitData.amount reads 0 — this re-checks all of them and
//      prints what each one actually carries.
//
//   2. If no amount is reachable, HOW BIG is the blind spot? Every heal FX on
//      a hero is tallied against whether the target was already at full health
//      and whether any health rise followed, so "N% of heal events land on a
//      full-health target" becomes a measured number instead of a guess.
//
// Float arguments are the hard part: HL passes f64 in xmm1..xmm3 and frida's
// x64 CpuContext does not expose xmm. So each hook also dumps the win64 shadow
// store (rsp+8..rsp+48) — if the JIT spills its register args there, the amount
// is readable without touching the game. The context's own key list is printed
// once so a frida that DOES expose xmm is not missed.
//
// DATA + OFF + B are prepended by run_heal.py.

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
        if (t.isNull() || t.compare(ptr("0x10000")) <= 0) return null;
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

const UNIT = {};
(B.unitClasses || []).forEach(function (c) { UNIT[c] = 1; });

let base = null, fnHasMaxHealth = null;
let ctxKeysLogged = false;
const fired = {};       // hook name -> call count
const samples = {};     // hook name -> samples printed so far
const SAMPLE_MAX = 3;

// ---- generic argument description -------------------------------------------
// A register either holds a pointer into an HL object (then its type name is the
// most useful thing about it) or a small integer. Floats never arrive here — the
// point of printing them anyway is to prove that.
function describe(v) {
    try {
        if (v === undefined || v === null) return "-";
        if (v.isNull()) return "null";
        const tn = typeName(v);
        if (tn) return v.toString() + "<" + tn + ">";
        const u = uint64(v.toString());
        if (u.compare(uint64("0x100000")) < 0) return v.toString(10);
        return v.toString();
    } catch (e) { return "?"; }
}

// Win64 shadow store + the first stack args. If the JIT spills xmm1 here, a
// heal amount shows up as a plausible double at +16.
function stackDump(rsp) {
    const out = [];
    for (let i = 1; i <= 6; i++) {
        try {
            const q = rsp.add(i * 8);
            const d = q.readDouble();
            const p = q.readPointer();
            const plausible = isFinite(d) && d !== 0 &&
                              Math.abs(d) > 1e-4 && Math.abs(d) < 1e9;
            out.push("+" + (i * 8) + "=" +
                     (plausible ? d.toFixed(3) : describe(p)));
        } catch (e) { out.push("+" + (i * 8) + "=?"); }
    }
    return out.join("  ");
}

function unitName(u) {
    try {
        const tn = typeName(u);
        if (tn === "ent.Hero") return hlStr(u.add(OFF.Hero.name).readPointer()) || "?hero";
        return hlStr(u.add(OFF.Unit.kind).readPointer()) || (tn || "?");
    } catch (e) { return "?"; }
}

function unitHp(u) {
    try {
        const attr = u.add(OFF.Unit.attr).readPointer();
        if (!attr || attr.isNull() || attr.compare(ptr("0x10000")) <= 0) return null;
        return { hp: attr.add(B.UnitAttributes.health).readDouble(),
                 max: attr.add(B.UnitAttributes.maxHealth).readDouble() };
    } catch (e) { return null; }
}

function atFull(u) {
    // The game's own answer, when it is safe to ask (ent.Unit subclasses only).
    if (fnHasMaxHealth) {
        const tn = typeName(u);
        if (tn && UNIT[tn]) { try { return !!fnHasMaxHealth(u); } catch (e) {} }
    }
    const h = unitHp(u);
    if (!h || !(h.max > 0)) return null;
    return h.hp >= h.max - 0.5;
}

// ---- HitData: every numeric field, so a non-zero amount cannot hide ----------
function dumpHitData(hd, tag) {
    try {
        if (!hd || hd.isNull()) { log("    " + tag + ": null"); return; }
        const H = B.HitData, parts = [];
        ["amount", "dmgMult", "dmgAdd", "healMult", "block", "critChance",
         "critDmgMult", "threatMultiplier"].forEach(function (f) {
            if (H[f] == null) return;
            try { parts.push(f + "=" + hd.add(H[f]).readDouble().toFixed(3)); }
            catch (e) {}
        });
        if (H.effectKind != null)
            parts.push("effectKind=" + hd.add(H.effectKind).readS32());
        if (H.effectiveScalingLevel != null)
            parts.push("scaleLvl=" + hd.add(H.effectiveScalingLevel).readS32());
        if (H.result != null) {
            try {
                const res = hd.add(H.result).readPointer();
                parts.push("result=" + (res.isNull() ? "null"
                           : "enum#" + res.add(8).readS32()));
            } catch (e) {}
        }
        log("    " + tag + ": " + parts.join("  "));
        if (H.target != null) {
            const t = hd.add(H.target).readPointer();
            const h = (t && !t.isNull()) ? unitHp(t) : null;
            log("      target=" + describe(t) + " " + (t && !t.isNull() ? unitName(t) : "") +
                (h ? "  hp=" + h.hp.toFixed(0) + "/" + h.max.toFixed(0) : ""));
        }
        if (H.step != null) {
            const st = hd.add(H.step).readPointer();
            log("      step=" + describe(st));
        }
    } catch (e) { log("    " + tag + ": !" + e); }
}

function skillOf(bs) {
    try {
        if (!bs || bs.isNull()) return "?";
        return hlStr(bs.add(OFF.BaseSkill.kind).readPointer()) || "?";
    } catch (e) { return "?"; }
}

function ownerOf(bs) {
    try {
        const o = bs.add(OFF.BaseSkill.owner).readPointer();
        if (typeName(o) === "ent.Hero")
            return hlStr(o.add(OFF.Hero.name).readPointer()) || "?hero";
        const pl = bs.add(OFF.BaseSkill.ownerPlayer).readPointer();
        if (typeName(pl) === "st.Player")
            return hlStr(pl.add(OFF.Player.name).readPointer()) || "?player";
        return typeName(o) || "?";
    } catch (e) { return "?"; }
}

// ---- generic hook -----------------------------------------------------------
function attachGeneric(name, fi) {
    let addr;
    try { addr = base.add(fi * 8).readPointer(); }
    catch (e) { log("  [" + name + "] slot unreadable"); return false; }
    if (!addr || addr.isNull()) { log("  [" + name + "] null slot"); return false; }
    Interceptor.attach(addr, {
        onEnter: function () {
            fired[name] = (fired[name] || 0) + 1;
            const n = samples[name] = (samples[name] || 0) + 1;
            if (n > SAMPLE_MAX) return;
            try {
                const c = this.context;
                if (!ctxKeysLogged) {
                    ctxKeysLogged = true;
                    log("cpu context keys: " + Object.keys(c).join(","));
                }
                log("[" + name + "] #" + n +
                    "  rcx=" + describe(c.rcx) + "  rdx=" + describe(c.rdx) +
                    "  r8=" + describe(c.r8) + "  r9=" + describe(c.r9));
                log("    stack " + stackDump(c.rsp));
                // Any HL object arg that happens to be a HitData is worth
                // unpacking wherever it turns up.
                [c.rdx, c.r8, c.r9].forEach(function (v, i) {
                    if (typeName(v) === "st.skill.HitData")
                        dumpHitData(v, "arg" + (i + 1) + " HitData");
                });
                const tn = typeName(c.rcx);
                if (tn && UNIT[tn]) {
                    const h = unitHp(c.rcx);
                    log("    this=" + unitName(c.rcx) +
                        (h ? " hp=" + h.hp.toFixed(0) + "/" + h.max.toFixed(0) : "") +
                        " full=" + atFull(c.rcx));
                }
            } catch (e) {}
        },
        onLeave: function (ret) {
            const n = samples[name] || 0;
            if (n > SAMPLE_MAX) return;
            try {
                log("    -> ret=" + describe(ret) +
                    " (rax as double=" + (function () {
                        try {
                            const b = Memory.alloc(8); b.writePointer(ret);
                            const d = b.readDouble();
                            return isFinite(d) ? d.toFixed(3) : "-";
                        } catch (e) { return "-"; }
                    })() + ")");
            } catch (e) {}
        }
    });
    return true;
}

// ---- the blind-spot ledger --------------------------------------------------
// Every heal FX on a hero, bucketed by whether the target could take it. The
// meter counts the "landed" column and nothing else.
const ledger = {};          // "healer|skill" -> tallies
const pendingFx = {};       // target ptr -> {t, key}
let fxTotal = 0, fxFull = 0, fxNoRise = 0, landedTotal = 0;

function ledgerRow(key) {
    let r = ledger[key];
    if (!r) r = ledger[key] = { fx: 0, full: 0, noRise: 0, landed: 0, rises: 0 };
    return r;
}

function onHealFx(target, hitData) {
    try {
        if (typeName(target) !== "ent.Hero") return;
        if (typeName(hitData) !== "st.skill.HitData") return;
        const bs = hitData.add(B.HitData.baseSkill).readPointer();
        if (!bs || bs.isNull()) return;
        const key = ownerOf(bs) + "|" + skillOf(bs);
        const full = atFull(target);
        const r = ledgerRow(key);
        r.fx++; fxTotal++;
        if (full === true) { r.full++; fxFull++; }
        pendingFx[target.toString()] = { t: Date.now(), key: key, full: full };
        // The amount, if it is anywhere, is here.
        const amt = B.HitData.amount != null
                  ? hitData.add(B.HitData.amount).readDouble() : 0;
        if (amt !== 0) {
            r.amtSeen = (r.amtSeen || 0) + 1;
            if (r.amtSamples === undefined) r.amtSamples = [];
            if (r.amtSamples.length < 4) r.amtSamples.push(amt.toFixed(1));
        }
    } catch (e) {}
}

function onSetHealth(attrs) {
    // Attribute a health RISE to the most recent heal FX on that unit — the
    // shipping meter's own rule, reproduced so the two columns are comparable.
    try {
        const unit = attrs.add(B.UnitAttributes.unit).readPointer();
        if (typeName(unit) !== "ent.Hero") return null;
        return unit;
    } catch (e) { return null; }
}

function sweepPending() {
    const now = Date.now();
    for (const k in pendingFx) {
        const p = pendingFx[k];
        if (now - p.t <= 1500) continue;
        // No health rise followed this FX inside the meter's own window.
        const r = ledgerRow(p.key);
        r.noRise++; fxNoRise++;
        delete pendingFx[k];
    }
}

function report(tick) {
    log("");
    log("---- tick " + tick + " ----");
    const names = Object.keys(B.fn).sort();
    const hitNames = names.filter(function (n) { return fired[n]; });
    log("  hooks that FIRED:  " +
        (hitNames.length ? hitNames.map(function (n) {
            return n + "=" + fired[n];
        }).join("  ") : "(none yet)"));
    const quiet = names.filter(function (n) { return !fired[n] && B.fn[n] != null; });
    log("  silent:            " + (quiet.length ? quiet.join(" ") : "(none)"));

    if (!fxTotal) { log("  no heal FX seen yet — cast a heal."); return; }
    log("  -- heal events vs what the meter can count --");
    log("     fx=" + fxTotal + "   onFullHealth=" + fxFull +
        " (" + (fxFull / fxTotal * 100).toFixed(1) + "%)" +
        "   noHealthRise=" + fxNoRise +
        " (" + (fxNoRise / fxTotal * 100).toFixed(1) + "%)" +
        "   landedHp=" + landedTotal.toFixed(0));
    const keys = Object.keys(ledger).sort(function (a, b) {
        return ledger[b].fx - ledger[a].fx;
    });
    for (const k of keys.slice(0, 12)) {
        const r = ledger[k];
        log("     " + k.slice(0, 44).padEnd(44) +
            " fx=" + String(r.fx).padStart(4) +
            "  full=" + String(r.full).padStart(4) +
            "  noRise=" + String(r.noRise).padStart(4) +
            "  landed=" + r.landed.toFixed(0).padStart(8) +
            (r.amtSeen ? "   HitData.amount!=0 x" + r.amtSeen +
                         " " + (r.amtSamples || []).join(",") : ""));
    }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);

    if (B.fn2 && B.fn2.hasMaxHealth != null) {
        try {
            fnHasMaxHealth = new NativeFunction(
                base.add(B.fn2.hasMaxHealth * 8).readPointer(), "uint8", ["pointer"]);
        } catch (e) { log("hasMaxHealth unavailable: " + e); }
    }

    let n = 0;
    for (const name of Object.keys(B.fn).sort()) {
        const fi = B.fn[name];
        if (fi == null) { log("  [" + name + "] not in this build"); continue; }
        // The two the shipping meter already relies on get purpose-built hooks
        // below; everything else is a discovery hook.
        if (name === "Unit.playHitHealFX" || name === "UnitAttributes.set_health")
            continue;
        if (attachGeneric(name, fi)) n++;
    }
    log("attached " + n + " discovery hooks");

    const fxFi = B.fn["Unit.playHitHealFX"], shFi = B.fn["UnitAttributes.set_health"];
    if (fxFi != null) {
        Interceptor.attach(base.add(fxFi * 8).readPointer(), {
            onEnter: function () {
                fired["Unit.playHitHealFX"] = (fired["Unit.playHitHealFX"] || 0) + 1;
                const c = this.context;
                const n2 = samples["Unit.playHitHealFX"] =
                    (samples["Unit.playHitHealFX"] || 0) + 1;
                if (n2 <= SAMPLE_MAX) {
                    log("[Unit.playHitHealFX] #" + n2 + "  target=" + describe(c.rcx) +
                        "  hitData=" + describe(c.rdx));
                    log("    stack " + stackDump(c.rsp));
                    dumpHitData(c.rdx, "hitData");
                    try {
                        const bs = c.rdx.add(B.HitData.baseSkill).readPointer();
                        if (bs && !bs.isNull()) {
                            log("    skill=" + skillOf(bs) + "  owner=" + ownerOf(bs));
                            if (B.BaseSkill.curHit != null) {
                                const ch = bs.add(B.BaseSkill.curHit).readPointer();
                                if (ch && !ch.isNull() && !ch.equals(c.rdx))
                                    dumpHitData(ch, "baseSkill.curHit");
                            }
                        }
                    } catch (e) {}
                }
                onHealFx(c.rcx, c.rdx);
            }
        });
        log("hooked ent.Unit.playHitHealFX");
    }
    if (shFi != null) {
        Interceptor.attach(base.add(shFi * 8).readPointer(), {
            onEnter: function () {
                this.attrs = this.context.rcx;
                this.unit = onSetHealth(this.attrs);
                if (this.unit) {
                    try { this.old = this.attrs.add(B.UnitAttributes.health).readDouble(); }
                    catch (e) { this.old = null; }
                }
            },
            onLeave: function () {
                try {
                    if (!this.unit || this.old === null || !(this.old > 0)) return;
                    const nv = this.attrs.add(B.UnitAttributes.health).readDouble();
                    const d = nv - this.old;
                    if (!(d > 0)) return;
                    const k = this.unit.toString();
                    const p = pendingFx[k];
                    if (p && Date.now() - p.t < 1500) {
                        const r = ledgerRow(p.key);
                        r.landed += d; r.rises++;
                        landedTotal += d;
                        delete pendingFx[k];
                    }
                } catch (e) {}
            }
        });
        log("hooked ent.UnitAttributes.set_health");
    }

    let tick = 0;
    setInterval(function () { sweepPending(); report(++tick); }, 3000);
    log("");
    log("ARMED. Now: heal a hurt target, then heal a target already at FULL "
        + "health (yourself, out of combat, is easiest).");
    send({ kind: "armed" });
}

// Deferred off script.load() on purpose — the functions_ptrs scan blows frida's
// load() handshake timeout when run synchronously.
setTimeout(main, 150);
