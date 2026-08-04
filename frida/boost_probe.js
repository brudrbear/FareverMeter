// boost_probe.js — WHO ACTUALLY SWUNG, on a Swarmstrike Accord proc?
//
// Swarmstrike Accord (`DS_Bladeleaf_Skill2`, from the Wingsabers dual blades
// `DS_Z1RBee_AssWiz` — NOT Beefury/Sword_Swarm, which the 3.3.3 comment named
// by mistake) blesses every ally in range. The blessing is a STATUS, and the
// game's own script is what makes this worth probing:
//
//     // DS_Bladeleaf_Skill2_Status
//     function onInflictDamage(hit) {
//         if (hit.isBaseAttack || hit.isFinalCombo)
//             playStep(Steps.BonusDamage, hit.target);
//     }
//
// That fires on the STATUS HOLDER's own attacks. So the game already models
// this per-ally, and the meter's problem — all of it landing on the wielder —
// may be recoverable rather than inherent.
//
// RUN THIS FROM A BUFFED ALLY'S CLIENT, not the wielder's. That is the whole
// leverage: when the local player swings and the proc fires, a field naming the
// SWINGER must resolve to the local hero and a field naming the CASTER must
// not. On the wielder's own client those are the same object and every
// candidate looks equally correct. `ownerIsMe` in the report is that check.
//
// Q1  IS THE STATUS PER-ALLY OR SHARED?  The discriminator that needs no
//     guessing: collect the distinct `DamageResult.baseSkill` POINTERS across
//     boosted hits. One pointer for the whole fight means a single instance
//     owned by the caster and there is no per-ally identity to recover. N
//     distinct pointers, one per buffed player, means the identity is right
//     there on the hit.
//
// Q2  WHICH FIELD NAMES THE SWINGER?  Read side by side, per distinct skill
//     instance, exactly as the summon spike did:
//       A  DamageResult.baseSkill -> BaseSkill.owner        (ent.GameObject)
//       B  DamageResult.baseSkill -> BaseSkill.ownerPlayer  (st.Player)
//       C  DamageResult.ctx -> SkillContext.baseSkill -> .owner
//       D  DamageResult.serverSource                        (ent.GameObject)
//       E  DamageResult.weakSource                          (i64 handle — never
//          tested; the summon spike only ruled out serverSource)
//       F  the dealer itself (rcx), i.e. what the meter credits today
//
// Q3  TEMPORAL FALLBACK.  If every field above names the caster, the swing
//     that triggered the proc may still be identifiable by order: the status
//     script runs inside the holder's own onInflictDamage, so the triggering
//     attack should be the immediately preceding hit. Every boosted hit
//     therefore records the previous non-boosted hit's dealer and the gap in
//     ms. A tight, consistent gap with a single plausible dealer is a usable
//     signal; a scattered one is not, and saying so is the point.
//
// Q4  DO SUMMONS PROC IT?  The blessing's area uses hitFilter 6, which the cdb
//     does not spell out. If a pet is an ally it gets the status and its hits
//     proc the bonus. Any boosted hit whose owner (or preceding dealer) is an
//     ent.Foe answers this; it is reported separately rather than folded in.
//
// READ-ONLY. No HL calls at all — plain pointer reads inside the damage hook,
// which is already on the game thread. See TESTING.md "Thread safety".
//
// DATA + OFF + B are prepended by run_boost.py.

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

const UNIT = {}, FOE = {};
(B.unitClasses || []).forEach(function (c) { UNIT[c] = 1; });
(B.foeClasses || []).forEach(function (c) { FOE[c] = 1; });

// A hero reads its name; anything else reads its unit kind. Same shape the
// census probe used, so the two logs are comparable.
function describe(p) {
    if (!p || p.isNull()) return "(null)";
    const cls = typeName(p);
    if (!cls) return "(bad ptr)";
    let nm = null;
    try {
        if (cls === "ent.Hero") nm = hlStr(p.add(B.Hero.name).readPointer());
        else if (UNIT[cls]) nm = hlStr(p.add(B.Unit.kind).readPointer());
    } catch (e) {}
    return cls + (nm ? "(" + nm + ")" : "");
}

function playerName(p) {
    try {
        if (!p || p.isNull() || typeName(p) !== "st.Player") return "(null)";
        return "st.Player(" + (hlStr(p.add(B.Player.name).readPointer()) || "?") + ")";
    } catch (e) { return "(null)"; }
}

let base = null, localHero = null, localName = null, armed = false;

// Boost skill instances, keyed by the BaseSkill POINTER — the Q1 discriminator.
const inst = {};
// Every (dealer class, skill kind) seen, so a boosted hit can be compared with
// the ordinary hits in the same session rather than in isolation.
const seen = {};
// Q3: the last non-boosted hit, and the tally of what preceded each boost.
let lastHit = null;
const preceded = {};
let boostHits = 0, boostDmg = 0;
// weakSource sampled PER HIT, not per instance. The first run showed a single
// shared skill instance serving procs from several different triggers, so
// anything recorded once per instance cannot possibly vary with the swinger —
// it has to be read on every hit and cross-tabbed against what preceded it. A
// handle that tracks the trigger is the field we are looking for; one that
// stays constant across different triggers is the caster's and is useless.
const weakSrc = {};

function isBoostKind(kind) {
    if (!kind) return false;
    for (let i = 0; i < B.boostPrefixes.length; i++)
        if (kind.indexOf(B.boostPrefixes[i]) === 0) return true;
    return false;
}

function onInflict(dealer, dr) {
    try {
        if (!dealer || dealer.isNull() || !dr || dr.isNull()) return;
        const dcls = typeName(dealer);
        if (!dcls) return;

        const bs = dr.add(OFF.DamageResult.baseSkill).readPointer();
        let kind = null;
        if (bs && !bs.isNull()) {
            try { kind = hlStr(bs.add(B.BaseSkill.kind).readPointer()); } catch (e) {}
        }
        const amount = dr.add(OFF.DamageResult._amount).readDouble();
        const now = Date.now();

        const sk = (dcls || "?") + "  |  " + (kind || "?");
        if (!seen[sk]) seen[sk] = { hits: 0, dmg: 0 };
        seen[sk].hits++; seen[sk].dmg += amount;

        if (!isBoostKind(kind)) {
            // Only real damage counts as a candidate trigger — a 0-damage
            // bookkeeping hit is not the swing that set the proc off.
            if (amount > 0) lastHit = { who: describe(dealer), t: now,
                                        kind: kind || "?", foe: !!FOE[dcls] };
            return;
        }

        boostHits++; boostDmg += amount;

        // Q3, recorded before anything else can overwrite lastHit.
        const gap = lastHit ? (now - lastHit.t) : -1;
        const pk = lastHit ? (lastHit.who + "   [" + lastHit.kind + "]") : "(nothing yet)";
        if (!preceded[pk]) preceded[pk] = { n: 0, gapSum: 0, gapMax: 0, foe: lastHit ? lastHit.foe : false };
        preceded[pk].n++;
        if (gap >= 0) {
            preceded[pk].gapSum += gap;
            if (gap > preceded[pk].gapMax) preceded[pk].gapMax = gap;
        }

        // Q1 + Q2: one record per distinct skill instance.
        const key = bs.toString();
        if (!inst[key]) {
            let owner = null, ownerPlayer = null, ctxOwner = "(no ctx)",
                ctxKind = null, srvSrc = "(null)", weak = "?";
            try { owner = bs.add(B.BaseSkill.owner).readPointer(); } catch (e) {}
            try { ownerPlayer = bs.add(B.BaseSkill.ownerPlayer).readPointer(); } catch (e) {}
            try {
                const ctx = dr.add(B.DamageResult.ctx).readPointer();
                if (ctx && !ctx.isNull()) {
                    const cbs = ctx.add(B.SkillContext.baseSkill).readPointer();
                    if (cbs && !cbs.isNull()) {
                        ctxKind = hlStr(cbs.add(B.BaseSkill.kind).readPointer());
                        ctxOwner = describe(cbs.add(B.BaseSkill.owner).readPointer());
                    }
                }
            } catch (e) {}
            try { srvSrc = describe(dr.add(OFF.DamageResult.serverSource).readPointer()); } catch (e) {}
            try { weak = dr.add(B.DamageResult.weakSource).readS64().toString(); } catch (e) {}

            inst[key] = {
                kind: kind, hits: 0, dmg: 0,
                dealer: describe(dealer),
                dealerIsMe: (localHero && dealer.equals(localHero)) ? "YES" : "no",
                A_owner: describe(owner),
                B_ownerPlayer: playerName(ownerPlayer),
                C_ctxOwner: ctxOwner + (ctxKind ? "  [" + ctxKind + "]" : ""),
                D_serverSource: srvSrc,
                E_weakSource: weak,
                ownerIsFoe: (owner && FOE[typeName(owner)]) ? "YES" : "no",
                // THE MONEY CHECK, and the reason it is worth probing from a
                // buffed ally's client rather than the wielder's: when the
                // local player is the one who swung, a field that names the
                // SWINGER must point at the local hero, and a field that names
                // the CASTER must not. From the wielder's own client those two
                // are the same object and the question cannot be settled.
                ownerIsMe: (owner && localHero && !owner.isNull()
                            && owner.equals(localHero)) ? "YES <<<<" : "no",
            };
            log("  NEW BOOST INSTANCE  " + kind + "  @" + key);
        }
        inst[key].hits++; inst[key].dmg += amount;
    } catch (e) {}
}

// Called only from the postUpdate hook — these are HL calls and belong on the
// game thread. See TESTING.md "Thread safety": the same calls from a
// setInterval killed the game.
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

function report() {
    const keys = Object.keys(inst);
    log("");
    log("================ BOOST PROBE ================  hero=" + (localName || "?"));
    log("boosted hits: " + boostHits + "   damage: " + Math.round(boostDmg)
        + "   DISTINCT SKILL INSTANCES: " + keys.length);
    if (!keys.length) {
        log("  (no Swarmstrike Accord damage yet — equip Wingsabers and attack)");
    } else {
        log("  Q1: " + (keys.length > 1
            ? "MULTIPLE instances -> per-ally identity EXISTS on the hit"
            : "ONE instance so far -> either solo, or a single shared instance"));
    }
    for (const k of keys) {
        const d = inst[k];
        log("  ---- @" + k + "  " + d.kind + "   hits=" + d.hits
            + " dmg=" + Math.round(d.dmg));
        log("      F dealer(rcx) : " + d.dealer + "   isLocalHero=" + d.dealerIsMe);
        log("      A owner       : " + d.A_owner + "   isLocalHero=" + d.ownerIsMe
            + (d.ownerIsFoe === "YES" ? "   <-- A FOE/SUMMON" : ""));
        log("      B ownerPlayer : " + d.B_ownerPlayer);
        log("      C ctx owner   : " + d.C_ctxOwner);
        log("      D serverSource: " + d.D_serverSource);
        log("      E weakSource  : " + d.E_weakSource);
    }
    const pk = Object.keys(preceded);
    if (pk.length) {
        log("  -- Q3: what immediately preceded a boosted hit --");
        pk.sort(function (a, b) { return preceded[b].n - preceded[a].n; });
        for (const p of pk.slice(0, 12)) {
            const v = preceded[p];
            log("      x" + v.n + "  avg " + Math.round(v.gapSum / Math.max(v.n, 1))
                + "ms  max " + v.gapMax + "ms   " + p
                + (v.foe ? "   <-- SUMMON/FOE" : ""));
        }
    }
    log("=============================================");
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base + "   watching for: " + B.boostPrefixes.join(", "));

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
                    + "  — get in range of the Wingsabers player so the "
                    + "blessing lands on YOU, then attack. Your own swings "
                    + "are the controlled case: watch A owner isLocalHero.");
            }
        }
    });
    setInterval(report, 8000);
}

setTimeout(main, 150);
