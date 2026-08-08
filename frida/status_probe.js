// status_probe.js — BUFF/STATUS TRACKING SPIKE.
//
// The static picture (hlboot 2026-08-08): every unit carries its live statuses
// in two arrays, and st.skill.Status (extends st.skill.BaseSkill) carries
// everything a buff tracker wants:
//
//   ent.Unit.statuses           : hxbit.ArrayProxyData  — on me
//   ent.Unit.instigatedStatuses : hl.types.ArrayObj     — applied BY me
//   st.skill.Status.kind        : String   (the cdb skill id — 250 rows in
//                                data.cdb carry props.status)
//   .stacks / .refreshDuration  : hxbit network properties, so replicated
//   .startTime .stopTime .duration : f64
//   .shieldAmount .auraRange    : f64
//
// Six questions, none of which static analysis can answer:
//   1. Does the `statuses` walk work, and through which chain? ArrayProxyData
//      -> ArrayDyn -> ArrayObj is the shape Collection.pets uses; the probe
//      prints the class at every hop rather than assuming it repeats here.
//   2. WHICH CLOCK are startTime/duration in? Round 1 settled half of this:
//      stopTime is -1 on every status, so the expiry is startTime + duration,
//      and the rate is 1s/s (two statuses whose computed expiries were 4.10s
//      apart were observed vanishing 4.21s apart, inside the sweep's 250ms).
//      What is still open is where to READ "now" — serverNow is shipped in the
//      offsets but nothing in the meter has ever read it, so the [tick] line
//      prints it against the wall clock. This is the whole feature: a tracker
//      that can't say "4.2s left" is a tracker that shows a lit square.
//   3. Is `stacks` 0- or 1-based for a single application, and does it climb?
//      Round 1: 1-based (a lone GA_Demon_Combo_Status read 1).
//   4. What does a permanent/aura status look like — stopTime 0, huge, or
//      equal to startTime? A "forever" buff must not render as expired.
//   5. Does an expired status leave the array promptly, or linger with
//      removed=true? Decides whether the tracker needs its own reaping.
//   6. What actually lands on a hero in ordinary play? Every distinct kind is
//      collected, so the picker's "seen this session" list can be judged
//      against real traffic — including the internal ones (Dash_Status and
//      friends) that a player would not want in a list.
//
// ---------------------------------------------------------------------------
// ROUND 1 RESULTS — 2026-08-08, Warrior, ~14 min of ordinary play (combat,
// dashing, eating, swimming). Everything below is measured, not inferred.
//
//   * The walk works: Unit.statuses is ArrayProxyData -> ArrayDyn -> ArrayObj,
//     the same chain as Collection.pets. instigatedStatuses is a bare ArrayObj.
//   * `kind` IS the cdb skill id — Enchant_Zealot_Status, GA_Demon_Combo_Status,
//     Warrior_BerserkStatus all resolve against data.cdb. The ONE exception is
//     st.skill.ItemStatus, which reports the literal class name "ItemStatus"
//     for every food and potion alike; those are identified by originItem.
//   * stopTime is -1 on EVERY status. It is not a clock. The expiry is
//     startTime + duration.
//   * `duration` GROWS EVERY TIME THE STATUS IS REFRESHED while startTime
//     stays put: one Zealot was watched going 15.00 -> 17.03 -> 34.53 ->
//     64.03 with startTime unmoved. So `duration` is lifetime-at-expiry, not
//     "how long this buff lasts", and a clock swipe drawn as
//     remaining/duration would sit near full on a buff about to drop.
//     refreshDuration is the constant nominal length (Zealot 15.00, Burning
//     Appetite 20.00, Battle Shout 15.00 — matching the cdb exactly) and is
//     the correct denominator.
//   * refreshDuration == 0 (with duration == 0) is the game's own "this one
//     has no timer": Dash_Status, UnderWater, Surge of Violence and the demon
//     passive all read it. Those are conditional statuses that end on an event,
//     and they must render as simply "up" rather than as expired.
//   * `stacks` is 1-BASED and climbs on ONE entry — a status does not appear
//     once per stack. Watched 1->5 on Zealot and 1->9 on Buzzing.
//   * The cdb's maxStacks is a BASE, NOT A CEILING: GA_Demon_Combo_Status is
//     maxStacks 3 in data.cdb and was observed live at 5. Never render "x/max"
//     from cdb data.
//   * A status can be caught MID-CONSTRUCTION, with start, stop and duration
//     all 0 (seen once on Surge of Violence). Anything reading these has to
//     tolerate that rather than compute an expiry four hours in the past.
//
// Still open, and the only reason to run this again: question 2's second half
// — WHERE to read "now". Round 1 asked GameLayer for a field called `time`;
// the class calls it `_time`, so every clock read came back null for the whole
// session. Fixed here.
// ---------------------------------------------------------------------------
//
// Nothing is called and nothing is written. Reads only, all plain pointer
// arithmetic — the one HL-call site is the hero lookup, on the game thread
// inside postUpdate, same split as every other probe here.
//
// DATA + OFF + P are prepended by run_status.py.

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const tb=m.address.sub(seed.findex*8);if(tb.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=tb.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return tb;}}}return null;}
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

let base = null;
let localHero = null;
let frames = 0, heroTries = 0;

// GAME THREAD ONLY — the timer below never HL-calls.
function refreshLocalHeroOnGameThread() {
    if (localHero && !localHero.isNull()) return;
    heroTries++;
    for (const nm in DATA.funcs) {
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") { localHero = h; return; }
        } catch (e) {}
    }
}

// ---- the array walk, reporting its own shape -------------------------------
// Question 1. Rather than assuming the pets chain repeats, every hop is named
// and the chain that actually worked is logged once. A proxy field and a plain
// ArrayObj field are both accepted, because `statuses` is one and
// `instigatedStatuses` is the other.
const chainSeen = {};

function walkArray(holder, off, label) {
    if (off == null) return null;
    let p;
    try { p = holder.add(off).readPointer(); } catch (e) { return null; }
    if (!p || p.isNull()) return null;
    const hops = [];
    let cur = p;
    for (let depth = 0; depth < 4; depth++) {
        const cls = typeName(cur);
        hops.push(cls || cur.toString());
        if (cls === "hl.types.ArrayObj") {
            const chain = hops.join(" -> ");
            if (chainSeen[label] !== chain) {
                chainSeen[label] = chain;
                log("  chain " + label + ": " + chain);
            }
            const n = cur.add(OFF.ArrayObj.length).readS32();
            if (n < 0 || n > 4096) return null;
            const data = cur.add(OFF.ArrayObj.array).readPointer();
            const out = [];
            for (let i = 0; i < n; i++) {
                try {
                    const el = data.add(OFF.ArrayObj.data + i * 8).readPointer();
                    if (el && !el.isNull()) out.push(el);
                } catch (e) { break; }
            }
            return out;
        }
        // Step through the two known wrappers; anything else and we stop and
        // say what we found rather than reading a guessed offset.
        let next = null;
        try {
            if (cls === "hxbit.ArrayProxyData" && P.ArrayProxyData.array != null)
                next = cur.add(P.ArrayProxyData.array).readPointer();
            else if (cls === "hl.types.ArrayDyn" && P.ArrayDyn.array != null)
                next = cur.add(P.ArrayDyn.array).readPointer();
        } catch (e) {}
        if (!next || next.isNull()) {
            if (chainSeen[label] !== hops.join(" -> ")) {
                chainSeen[label] = hops.join(" -> ");
                log("  chain " + label + " DEAD END: " + hops.join(" -> "));
            }
            return null;
        }
        cur = next;
    }
    return null;
}

// ---- one status, read whole ------------------------------------------------
function readStatus(s) {
    const S = P.Status;
    const o = { cls: typeName(s), ptr: s.toString() };
    function f64(name) {
        if (S[name] == null) return null;
        try { return s.add(S[name]).readDouble(); } catch (e) { return null; }
    }
    function s32(name) {
        if (S[name] == null) return null;
        try { return s.add(S[name]).readS32(); } catch (e) { return null; }
    }
    o.kind = S.kind != null ? hlStr(s.add(S.kind).readPointer()) : null;
    // Measured round 1: every st.skill.ItemStatus reports kind "ItemStatus" —
    // the literal class name, not a cdb id — so two food buffs are one string.
    // originItem is what separates them.
    if (S.originItem != null && P.Item && P.Item.kind != null) {
        try {
            const it = s.add(S.originItem).readPointer();
            o.item = (it && !it.isNull())
                ? hlStr(it.add(P.Item.kind).readPointer()) : null;
        } catch (e) { o.item = null; }
    }
    o.stacks = s32("stacks");
    o.start = f64("startTime");
    o.stop = f64("stopTime");
    o.dur = f64("duration");
    o.refresh = f64("refreshDuration");
    o.shield = f64("shieldAmount");
    o.aura = f64("auraRange");
    try { o.removed = S.removed != null ? s.add(S.removed).readU8() : null; }
    catch (e) { o.removed = null; }
    // Whose status is it? For `statuses` this should be me; the field is read
    // anyway because it is what would let a target/party tray work later.
    if (S.owner != null) {
        try {
            const ow = s.add(S.owner).readPointer();
            o.owner = (ow && !ow.isNull()) ? typeName(ow) : null;
        } catch (e) { o.owner = null; }
    }
    return o;
}

function fmt(v, dp) {
    if (v === null || v === undefined) return "-";
    return v.toFixed(dp === undefined ? 2 : dp);
}

function statusLine(o, now) {
    // The countdown. Measured round 1: stopTime is -1 on EVERY status, so the
    // expiry is startTime + duration and stopTime is not a clock at all. Both
    // are printed so a build that starts populating stopTime is visible rather
    // than silently preferred.
    const left = (o.start !== null && o.dur !== null && now !== null)
        ? o.start + o.dur - now : null;
    return "    " + (o.kind || "?") + (o.item ? "<" + o.item + ">" : "")
        + "  st=" + (o.stacks === null ? "-" : o.stacks)
        + "  start=" + fmt(o.start) + " stop=" + fmt(o.stop)
        + " dur=" + fmt(o.dur) + " refresh=" + fmt(o.refresh)
        + "  left=" + fmt(left)
        + (o.shield ? "  shield=" + fmt(o.shield, 0) : "")
        + (o.aura ? "  aura=" + fmt(o.aura, 0) : "")
        + (o.removed ? "  REMOVED" : "")
        + (o.cls !== "st.skill.Status" ? "  [" + o.cls + "]" : "")
        + (o.owner && o.owner !== "ent.Hero" ? "  owner=" + o.owner : "");
}

// ---- the clocks ------------------------------------------------------------
// hero -> Hero.layer -> GameLayer._time -> TimeState.serverNow. The offsets are
// taken from OFF (meter_offsets.json, which has read `layer["_time"]` all along)
// with P as an override, and the pair that was actually used is logged once —
// round 1 spent a whole session printing "-" for every clock because the field
// name was wrong and nothing said so.
const CLK = {
    layer: (P.Hero && P.Hero.layer != null) ? P.Hero.layer
        : (OFF.Hero ? OFF.Hero.layer : null),
    time: (P.GameLayer && P.GameLayer.time != null) ? P.GameLayer.time
        : (OFF.GameLayer ? OFF.GameLayer.time : null),
    now: (P.TimeState && P.TimeState.serverNow != null) ? P.TimeState.serverNow
        : (OFF.TimeState ? OFF.TimeState.serverNow : null),
    start: (P.TimeState && P.TimeState.serverStart != null)
        ? P.TimeState.serverStart
        : (OFF.TimeState ? OFF.TimeState.serverStart : null),
};

let clockWarned = false;

function clocks() {
    const out = { now: null, start: null };
    try {
        if (CLK.layer == null || CLK.time == null || CLK.now == null) {
            if (!clockWarned) {
                clockWarned = true;
                log("!! clock offsets incomplete: " + JSON.stringify(CLK)
                    + " — every countdown will read '-'");
            }
            return out;
        }
        const layer = localHero.add(CLK.layer).readPointer();
        if (!layer || layer.isNull()) return out;
        const ts = layer.add(CLK.time).readPointer();
        if (!ts || ts.isNull()) return out;
        // Say what the object at the end of the walk actually is. A TimeState
        // here is the proof the chain is right; anything else means serverNow
        // is being read off the wrong struct and the numbers are garbage that
        // happens to look plausible.
        if (!clockWarned) {
            clockWarned = true;
            log("clock chain: Hero+" + CLK.layer + " -> GameLayer+" + CLK.time
                + " -> " + (typeName(ts) || "?") + "+" + CLK.now);
        }
        out.now = ts.add(CLK.now).readDouble();
        if (CLK.start != null) out.start = ts.add(CLK.start).readDouble();
    } catch (e) {}
    return out;
}

// ---- the sweep -------------------------------------------------------------
let ticks = 0;
let armed = false;
let lastKeys = "";
const kindsSeen = {};          // kind -> times it appeared as a NEW status
const t0 = Date.now();

// A status is identified by its POINTER for change detection: two stacks of
// the same kind are two entries in some builds and one in others, and the
// probe must not decide that question by how it counts.
function sweep() {
    try {
        ticks++;
        if (!localHero || localHero.isNull()) {
            if (ticks % 10 === 0)
                log("[wait] frames=" + frames + " heroTries=" + heroTries
                    + " — no ent.Hero yet (menu/loading screen is expected).");
            return;
        }
        const c = clocks();
        const els = walkArray(localHero, P.Unit.statuses, "Unit.statuses");
        if (els === null) {
            if (ticks % 10 === 0) log("[wait] statuses walk returned nothing");
            return;
        }
        const rows = els.map(readStatus);
        const key = rows.map(function (r) { return r.ptr + ":" + r.stacks; }).join(",");
        if (key !== lastKeys) {
            const before = lastKeys;
            lastKeys = key;
            // Wall clock and both server clocks on the edge line, so the drift
            // between Date.now() and serverNow is measurable across the run.
            log("=== statuses changed (" + rows.length + " up)  "
                + "wall=" + ((Date.now() - t0) / 1000).toFixed(2)
                + "  serverNow=" + fmt(c.now) + "  serverStart=" + fmt(c.start));
            for (const r of rows) {
                log(statusLine(r, c.now));
                if (r.kind && !(r.kind in kindsSeen)) kindsSeen[r.kind] = 0;
                if (r.kind) kindsSeen[r.kind]++;
            }
            if (before === "") log("    (first reading — this is the resting set)");
        } else if (ticks % 8 === 0 && rows.length) {
            // Question 2 and 4 need the SAME status watched over time, not just
            // on its edges: a countdown that moves proves the clock, a
            // stop-now that sits still proves a permanent buff.
            // `now - wall` is the whole point of this line: if serverNow really
            // is the clock startTime lives in, this difference holds STILL
            // across the run (one second of server per second of wall). Drift
            // means the countdown would slowly lie.
            log("[tick] serverNow=" + fmt(c.now)
                + " now-wall=" + fmt(c.now !== null
                    ? c.now - (Date.now() - t0) / 1000 : null)
                + "  " + rows.map(function (r) {
                    return (r.kind || "?") + " left="
                        + fmt(r.start !== null && r.dur !== null && c.now !== null
                            ? r.start + r.dur - c.now : null, 1)
                        + " x" + r.stacks;
                }).join("  |  "));
        }
    } catch (e) { log("sweep ERR " + e); }
}

// The resting dump doubles as the ARMED gate. It runs on the game thread only
// because the hero lookup does; the reads themselves are timer-safe.
function dumpOnce() {
    log("--- status dump ---");
    log("  hero = " + localHero + "  kind="
        + (P.Unit.kind != null
            ? hlStr(localHero.add(P.Unit.kind).readPointer()) : "?"));
    const c = clocks();
    log("  clocks: serverNow=" + fmt(c.now) + "  serverStart=" + fmt(c.start)
        + "  (wall since attach " + ((Date.now() - t0) / 1000).toFixed(2) + "s)");
    for (const which of ["statuses", "instigatedStatuses"]) {
        const els = walkArray(localHero, P.Unit[which], "Unit." + which);
        if (els === null) { log("  Unit." + which + ": walk failed"); continue; }
        log("  Unit." + which + ": " + els.length + " entries");
        for (const el of els) log(statusLine(readStatus(el), c.now));
    }
    log("--- end status dump ---");
}

function onGameThreadTick() {
    frames++;
    refreshLocalHeroOnGameThread();
    if (!armed && localHero && !localHero.isNull()) {
        armed = true;
        dumpOnce();
        log("PROBE ARMED — statuses watched. Do the in-game steps now.");
    }
}

function summary() {
    const kinds = Object.keys(kindsSeen).sort();
    log("=== kinds seen this run (" + kinds.length + ") ===");
    for (const k of kinds) log("    " + k + "  x" + kindsSeen[k]);
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (P.fn.postUpdate == null) {
        log("!! no postUpdate findex — no game-thread anchor; refusing.");
        return;
    }
    Interceptor.attach(base.add(P.fn.postUpdate * 8).readPointer(), {
        onEnter: function () { onGameThreadTick(); }
    });
    log("waiting for the hero. Nothing is armed until the ARMED line prints.");
    setInterval(sweep, 250);
    recv("summary", function () { summary(); });
}

setTimeout(main, 0);
