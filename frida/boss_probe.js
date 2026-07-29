// boss_probe.js — BOSS HEALTHBAR SPIKE. Throwaway.
//
// The bytecode says ui.hud.BossesInfo holds a `bossInfos` array of
// ui.hud.BossInfo, each with { active:bool, unit:ent.Unit }. That's the game's
// own "a boss bar is on screen" state. What the bytecode CANNOT say, and what
// this answers before any meter code gets written:
//
//   1. how often fetchBosses actually fires — if it's per-frame, the shipping
//      hook body has to stay near-free, the same constraint the damage hook has
//   2. whether `active` is the right gate, or whether visible / alpha / removed
//      is what really tracks the bar, and whether bossInfos is a FIXED pool
//      that toggles or an array that grows and shrinks
//   3. what ent.Unit.isBoss / isMiniboss / isElite return for the units that
//      get a bar, and what raw inf.flags reads — so "boss vs miniboss vs elite"
//      can be tagged without guessing bit positions from declaration order
//
// It also proves the edge detection end to end: every transition is logged as
// BAR UP / BAR DOWN, which is the actual feature in miniature.
//
// Hooks fetchBosses + init only to capture `this` and count. Everything else is
// plain pointer reads off a timer — the same off-thread discipline the shipping
// hook uses for the rift flag.
//
// DATA + OFF + B are prepended by run_boss.py.

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

function readArray(arrPtr) {
    const out = [];
    try {
        if (!arrPtr || arrPtr.isNull()) return out;
        const n = arrPtr.add(OFF.ArrayObj.length).readS32();
        if (n < 0 || n > 4096) return out;         // never trust a length
        const data = arrPtr.add(OFF.ArrayObj.array).readPointer();
        if (data.isNull()) return out;
        for (let i = 0; i < n; i++) {
            const e = data.add(OFF.ArrayObj.data + i * 8).readPointer();
            if (e && !e.isNull() && e.compare(ptr("0x10000")) > 0) out.push(e);
            else out.push(null);                   // keep slot indices aligned
        }
    } catch (e) {}
    return out;
}

let base = null;
let bossesInfo = null;          // captured `this` from fetchBosses / init
let fetchCalls = 0, lastFetchCalls = 0;
let firstFetchAt = null;
let arrLenSeen = {};            // bossInfos length -> times observed
let gateDivergence = 0;         // slots where active != visible
let tick = 0;

// ---- Q3: classify each unit ONCE, on the game thread ----
// isBoss/isMiniboss/isElite are HL calls and inf.flags comes back boxed, which
// allocates. Neither belongs on a timer thread, and neither belongs in a
// per-frame hook body either — so it happens inside the fetchBosses hook but
// only for a unit kind we have not already classified.
let fnIsBoss = null, fnIsMini = null, fnIsElite = null;
let fnHasBossInfo = null, fnShouldShow = null;
let hl_getField = null, hl_hashUtf8 = null;
const fieldHash = {};
const classified = {};          // unit kind -> description string

function setupCalls() {
    function fn(findex) {
        if (findex == null) return null;
        try {
            return new NativeFunction(base.add(findex * 8).readPointer(),
                                      "uint8", ["pointer"]);
        } catch (e) { return null; }
    }
    fnIsBoss = fn(B.fn.isBoss);
    fnIsMini = fn(B.fn.isMiniboss);
    fnIsElite = fn(B.fn.isElite);
    fnHasBossInfo = fn(B.fn.hasBossInfo);
    fnShouldShow = fn(B.fn.shouldShowBossInfo);
    try {
        const m = Process.findModuleByName("libhl.dll");
        hl_getField = new NativeFunction(m.findExportByName("hl_obj_get_field"),
                                         "pointer", ["pointer", "int"]);
        hl_hashUtf8 = new NativeFunction(m.findExportByName("hl_hash_utf8"),
                                         "int", ["pointer"]);
        for (const n of ["texts", "name", "flags"])
            fieldHash[n] = hl_hashUtf8(Memory.allocUtf8String(n));
    } catch (e) { hl_getField = null; }
}

function getField(obj, name) {
    try {
        if (!obj || obj.isNull() || !hl_getField) return null;
        return hl_getField(obj, fieldHash[name]);
    } catch (e) { return null; }
}

function unitKind(u) {
    try { return hlStr(u.add(B.Unit.kind).readPointer()); } catch (e) { return null; }
}

// Runs on the GAME thread (inside the fetchBosses hook). Safe place for calls.
//
// fromBar says the unit came out of a BossInfo slot, so it is certainly an
// ent.Foe and the two Foe-only predicates are safe to call. Units discovered
// by the layer sweep get only the ent.Unit predicates — calling a Foe method
// on something that turned out to be a Hero would corrupt the game, and there
// is no cheap runtime inheritance check to rule that out.
function classify(u, fromBar) {
    const kind = unitKind(u);
    if (!kind || kind in classified) return;
    const cls = typeName(u) || "?";
    let dn = "", flags = null, bits = "";
    try {
        const inf = u.add(B.Unit.inf).readPointer();
        const texts = getField(inf, "texts");
        dn = hlStr(getField(texts, "name")) || "";
        // hl_obj_get_field hands back a boxed vdynamic; an i32 payload sits at
        // +8, past the hl_type* header.
        const bf = getField(inf, "flags");
        if (bf && !bf.isNull()) flags = bf.add(8).readS32();
    } catch (e) {}
    function call(f) {
        if (!f) return "?";
        try { return f(u) ? "Y" : "n"; } catch (e) { return "!"; }
    }
    // The three predicates are the ground truth. The raw flags value next to
    // them is what lets us name the bits instead of trusting field order.
    let preds = "isBoss=" + call(fnIsBoss) + " isMini=" + call(fnIsMini) +
                " isElite=" + call(fnIsElite);
    if (fromBar)
        preds += " hasBossInfo=" + call(fnHasBossInfo) +
                 " shouldShow=" + call(fnShouldShow);
    else
        preds += " (foe preds skipped: not from a bar)";
    if (flags !== null) {
        const set = [];
        for (let i = 0; i < 32; i++) if (flags & (1 << i)) set.push(i);
        bits = " flags=0x" + (flags >>> 0).toString(16) + " bits[" + set.join(",") + "]";
    } else {
        bits = " flags=(unread)";
    }
    classified[kind] = "  " + kind + "  \"" + dn + "\"  [" + cls + "]" +
                       (fromBar ? "  <- HAD A BAR" : "") + "\n" +
                       "      " + preds + bits;
    pendingClass.push(kind);
}

let pendingClass = [];          // kinds newly classified, drained by the timer

// ---- discover foes that never get a bar ----
// An elite may or may not raise a boss bar. If it doesn't, the fetchBosses path
// never sees it and its flags go unmeasured — so the layer's `units` array gets
// swept too. The sweep only QUEUES pointers (reads-only, timer thread); the
// classification calls happen in the fetchBosses hook on the game thread, the
// same split the shipping meter uses for CDB name lookups.
let localHero = null;
let foeQueue = [];
const FOE_QUEUE_MAX = 48;
const CLASSIFY_PER_TICK = 3;

function refreshLocalHero() {
    if (localHero && !localHero.isNull()) return;
    for (const nm in DATA.funcs) {
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") { localHero = h; return; }
        } catch (e) {}
    }
}

function sweepFoes() {
    try {
        if (!localHero || localHero.isNull()) return;
        const layer = localHero.add(OFF.Hero.layer).readPointer();
        if (!layer || layer.isNull()) return;
        const units = readArray(layer.add(OFF.GameLayer.units).readPointer());
        for (const u of units) {
            if (!u) continue;
            const cls = typeName(u);
            // Never queue a Hero: classify() would still only call ent.Unit
            // methods on it, but there is no reason to spend calls on players.
            if (!cls || cls === "ent.Hero") continue;
            const kind = unitKind(u);
            if (!kind || kind in classified) continue;
            if (foeQueue.length >= FOE_QUEUE_MAX) break;
            if (!foeQueue.some(q => q.kind === kind)) foeQueue.push({ kind: kind, ptr: u });
        }
    } catch (e) {}
}

// ---- edge detection: the feature in miniature ----
let lastActive = {};            // slot index -> unit pointer string
const edges = [];               // queued for the timer to print

function snapshot() {
    // Cheap: reads only. No calls, no allocation. This is exactly what the
    // shipping hook body would do if it lived here.
    if (!bossesInfo || bossesInfo.isNull()) return null;
    let arr;
    try { arr = bossesInfo.add(B.BossesInfo.bossInfos).readPointer(); } catch (e) { return null; }
    const slots = readArray(arr);
    arrLenSeen[slots.length] = (arrLenSeen[slots.length] || 0) + 1;
    const out = [];
    for (let i = 0; i < slots.length; i++) {
        const s = slots[i];
        if (!s) { out.push(null); continue; }
        let active = false, visible = false, alpha = 0, removed = false, unit = null;
        try {
            active = s.add(B.BossInfo.active).readU8() !== 0;
            visible = s.add(B.UIElement.visible).readU8() !== 0;
            alpha = s.add(B.UIElement.alpha).readDouble();
            removed = s.add(B.UIElement.removed).readU8() !== 0;
            const u = s.add(B.BossInfo.unit).readPointer();
            if (u && !u.isNull() && u.compare(ptr("0x10000")) > 0) unit = u;
        } catch (e) { continue; }
        if (active !== visible) gateDivergence++;
        out.push({ i: i, active: active, visible: visible, alpha: alpha,
                   removed: removed, unit: unit });
    }
    return out;
}

function onFetch() {
    fetchCalls++;
    if (firstFetchAt === null) firstFetchAt = Date.now();
    // Game thread: the only safe place for the HL calls the sweep queued up.
    for (let i = 0; i < CLASSIFY_PER_TICK && foeQueue.length; i++) {
        const job = foeQueue.shift();
        classify(job.ptr, false);
    }
    const snap = snapshot();
    if (!snap) return;
    const now = {};
    for (const s of snap) {
        if (!s || !s.active || !s.unit) continue;
        now[s.i] = s.unit.toString();
        if (lastActive[s.i] !== now[s.i]) {
            edges.push({ up: true, i: s.i, unit: s.unit, t: Date.now() });
            classify(s.unit, true);   // from a bar: Foe predicates are safe
        }
    }
    for (const i in lastActive)
        if (!(i in now)) edges.push({ up: false, i: parseInt(i, 10), t: Date.now() });
    lastActive = now;
}

function report() {
    tick++;
    const rate = fetchCalls - lastFetchCalls;
    lastFetchCalls = fetchCalls;

    while (pendingClass.length) {
        const k = pendingClass.shift();
        log("  [classified] " + classified[k]);
    }
    while (edges.length) {
        const e = edges.shift();
        if (e.up) {
            const k = unitKind(e.unit) || "?";
            log(">>> BAR UP    slot " + e.i + "  " + k + "  " + (typeName(e.unit) || "?"));
        } else {
            log("<<< BAR DOWN  slot " + e.i);
        }
    }

    if (!bossesInfo) {
        if (tick % 5 === 0)
            log("[waiting] fetchBosses/init have not fired yet - no BossesInfo "
                + "pointer. Load into the world and approach a boss.");
        return;
    }

    const snap = snapshot();
    const live = snap ? snap.filter(s => s && s.active).length : 0;
    log("");
    log("---- tick " + tick + "  fetchBosses " + rate + "/s (total " + fetchCalls +
        ")  slots=" + (snap ? snap.length : "?") + "  active=" + live + " ----");
    if (!snap) return;
    for (const s of snap) {
        if (!s) { log("    (null slot)"); continue; }
        let desc = "-";
        if (s.unit) {
            const k = unitKind(s.unit) || "?";
            let hp = "";
            try {
                const attr = s.unit.add(B.Unit.attr).readPointer();
                if (attr && !attr.isNull()) {
                    const h = attr.add(B.UnitAttributes.health).readDouble();
                    const mh = attr.add(B.UnitAttributes.maxHealth).readDouble();
                    hp = "  hp=" + h.toFixed(0) + "/" + mh.toFixed(0) +
                         (mh > 0 ? " (" + (100 * h / mh).toFixed(1) + "%)" : "");
                }
            } catch (e) {}
            desc = k + "  " + (typeName(s.unit) || "?") + hp;
        }
        log("    slot " + s.i + "  active=" + (s.active ? "Y" : "n") +
            " visible=" + (s.visible ? "Y" : "n") +
            " alpha=" + s.alpha.toFixed(2) +
            " removed=" + (s.removed ? "Y" : "n") + "   " + desc);
    }
}

function summary() {
    log("");
    log("================ SUMMARY ================");
    const secs = firstFetchAt ? (Date.now() - firstFetchAt) / 1000 : 0;
    log("Q1  fetchBosses total=" + fetchCalls +
        (secs > 0 ? "  avg " + (fetchCalls / secs).toFixed(1) + "/s over " +
                    secs.toFixed(0) + "s since first call" : "  (never fired)"));
    log("      -> if this tracks your framerate, the shipping hook body must be");
    log("         reads-only, like snapshot() here.");
    const lens = Object.keys(arrLenSeen).sort();
    log("Q2  bossInfos lengths seen: " +
        (lens.length ? lens.map(l => l + " (x" + arrLenSeen[l] + ")").join(", ")
                     : "(none)"));
    log("      -> a single length = fixed pool, `active` is the gate.");
    log("         several lengths = the array grows/shrinks, gate on presence.");
    log("      active != visible observed " + gateDivergence + " times");
    log("      -> 0 means the two agree and either works; >0 means pick `active`.");
    log("Q3  classified unit kinds:");
    const ks = Object.keys(classified).sort();
    if (!ks.length) log("      (none - no boss bar came up during the run)");
    for (const k of ks) log(classified[k]);
    log("=========================================");
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    setupCalls();
    log("HL calls " + (fnIsBoss ? "resolved" : "UNRESOLVED") +
        ", CDB field api " + (hl_getField ? "ready" : "UNAVAILABLE"));

    if (B.fn.fetchBosses == null) { log("!! fetchBosses findex missing"); return; }
    Interceptor.attach(base.add(B.fn.fetchBosses * 8).readPointer(), {
        onEnter: function () { bossesInfo = this.context.rcx; onFetch(); }
    });
    if (B.fn.init != null) {
        // Catches the pointer at HUD build time, so a zone where no boss is
        // near still tells us the pool shape.
        Interceptor.attach(base.add(B.fn.init * 8).readPointer(), {
            onEnter: function () { bossesInfo = this.context.rcx; }
        });
    }
    log("hooked ui.hud.BossesInfo.fetchBosses (findex " + B.fn.fetchBosses + ")"
        + (B.fn.init != null ? " + init (" + B.fn.init + ")" : ""));
    log("walk to a boss and pull it. Ctrl+C when done.");
    refreshLocalHero();
    log("localHero = " + localHero);
    setInterval(refreshLocalHero, 3000);
    setInterval(sweepFoes, 2000);
    setInterval(report, 1000);
    recv("summary", function () { summary(); });
}

main();
