// codex_probe.js — the character's codex (hunting log): kill counts, per-unit
// completion, and which client-side function fires when a kill credits it.
//
// Static analysis (hlboot.dat) already mapped the shape. Two stores exist:
//
//   st.player.Progress.unitsProgress : hxbit.MapData    <- REPLICATED, per char
//   data.CodexData.unitNodes         : StringMap        <- the UI's derived tree
//
// and Progress is reachable from the local hero by pure pointer reads:
//
//   ent.Hero.player(@1216) -> st.Player.progress(@240) -> Progress.unitsProgress(@160)
//
// data.CodexData is a STATIC-ONLY class, so its fields live in an HL global and
// nothing in this project has ever read one. This probe does, WITHOUT calling
// into HL: hl_type_obj carries a `global_value` (void**) pointing at the global
// slot, so the class object — and with it `inited`, `unitNodes`, `allMonsters`
// — is a plain deref away. That matters for safety as much as reach: calling
// data.$CodexData.getUnitNode() before the codex is inited would null-deref a
// StringMap inside JIT'd code, and `inited` is how we find out first.
//
// What this probe is trying to settle, in order:
//
//   1. READ PATH — does unitsProgress hold kill COUNTS keyed by unit id, and is
//      it populated client-side? (It's a networked prop; owner-visibility is an
//      assumption until measured.)
//   2. COMPLETION — CodexNode carries _progress / completionProgress /
//      maxProgress / completed / _progressLevel / progressThresholds. Which of
//      those is the "12/20" the codex window actually shows?
//   3. THE KILL EVENT — which of nine candidate functions fires client-side on
//      a kill, and what it carries. The item-pickup lesson (three probe rounds,
//      every candidate server-side) says assume nothing here.
//   4. IDENTITY — a foe's ent.Unit.kind is the minimap's key already. Does one
//      kind map to exactly one codex node, or does inheritance fold variants
//      (Wolf_Z1W_Alpha -> Wolf) into one entry? getUnitNode returns an ARRAY,
//      which is a strong hint it's not 1:1.
//
// THREAD RULE (paid for twice): timers do plain reads only.
// Every HL call and every allocation rides client.BaseCamera.postUpdate.
//
// DATA + OFF + B are prepended by run_codex.py.

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x40000000).sort((a,b)=>((a.file?1:0)-(b.file?1:0))||(a.size-b.size));for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=base.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return base;}}}return null;}
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

// ---- libhl field API (GAME THREAD ONLY: hl_obj_get_field boxes and allocates) ----
let hl_getField = null, hl_hashUtf8 = null;
const fieldHash = {};
const CDB_FIELDS = ["id", "texts", "name", "flags", "props", "inherit", "type",
                    "typeId", "lvl", "h", "map", "keys", "value"];
function setupFieldApi() {
    try {
        const m = Process.findModuleByName("libhl.dll");
        hl_getField = new NativeFunction(m.findExportByName("hl_obj_get_field"),
                                         "pointer", ["pointer", "int"]);
        hl_hashUtf8 = new NativeFunction(m.findExportByName("hl_hash_utf8"),
                                         "int", ["pointer"]);
        for (const n of CDB_FIELDS)
            fieldHash[n] = hl_hashUtf8(Memory.allocUtf8String(n));
        return true;
    } catch (e) { return false; }
}
function getField(obj, name) {
    try {
        if (!obj || obj.isNull()) return null;
        return hl_getField(obj, fieldHash[name]);
    } catch (e) { return null; }
}
function describeVal(p) {
    try {
        if (!p || p.isNull()) return "(null)";
        const t = p.readPointer();
        const k = t.readU32();
        if (k === 3) return String(p.add(8).readS32());
        if (k === 4) return String(p.add(8).readS64());
        if (k === 5) return p.add(8).readFloat().toFixed(3);
        if (k === 6) return String(p.add(8).readDouble());
        if (k === 7) return p.add(8).readU8() ? "true" : "false";
        if (k === 11 || k === 21) {
            const nm = typeName(p);
            if (nm === "String") return JSON.stringify(hlStr(p));
            return "<" + nm + ">";
        }
        if (k === 15 || k === 16) return "<virtual>";
        if (k === 18) return "<enum idx=" + p.add(8).readS32() + ">";
        return "<kind" + k + ">";
    } catch (e) { return "(unreadable)"; }
}

// ---- is the codex built yet? -----------------------------------------------
// data.CodexData is static-only, so `inited` lives in an HL global. Reading a
// global would mean finding data.$CodexData's hl_type, and there is no instance
// of a static-only class to take one from — typeCache can never hold it. So the
// gate is the game's own UI instead: ui.BaseUI.displayWindow(ui, win) fires for
// every window, and a ui.win.codex.* window can only exist once initData has
// built the tree.
//
// This is a gate, not a nicety. getUnitNode() reaches into the unitNodes
// StringMap; calling it before initData has assigned that map would null-deref
// inside JIT'd code, and a crash costs Brudr the play session (it has once).
// Whether the codex must be opened before ANY of this is readable is itself one
// of the questions this probe exists to answer — so the gate is also the
// measurement.
let codexReady = false;
const CODEX_WIN_RX = /codex/i;

// ---- the read path: hero -> player -> progress ------------------------------
function progressFromHero(hero) {
    try {
        const player = hero.add(OFF.Hero.player).readPointer();
        if (!player || player.isNull()) return null;
        const prog = player.add(B.Player.progress).readPointer();
        if (!prog || prog.isNull()) return null;
        return prog;
    } catch (e) { return null; }
}

// hxbit.MapData.map(@40) is a virtual; hl_vvirtual is { t@0, value@8, next@16 }
// so `value` is the real haxe.ds.StringMap underneath (null only for a
// standalone virtual, which this is not). StringMap.h(@8) is the native
// hl.types.BytesMap abstract that $std.hbkeys/hbvalues walk.
function mapUnder(mapData) {
    try {
        if (!mapData || mapData.isNull()) return null;
        const v = mapData.add(B.MapData.map).readPointer();
        if (!v || v.isNull()) return null;
        const kindOfV = v.readPointer().readU32();
        // A virtual wrapping a real object: take `value`. An HOBJ here means
        // the field was already the map itself.
        if (kindOfV === 15) {
            const inner = v.add(8).readPointer();
            return (inner && !inner.isNull()) ? inner : null;
        }
        return v;
    } catch (e) { return null; }
}

let hbkeys = null, hbget = null, hbsize = null;
function setupMapApi(base) {
    try {
        if (B.natives.hbkeys != null)
            hbkeys = new NativeFunction(base.add(B.natives.hbkeys * 8).readPointer(),
                                        "pointer", ["pointer"]);
        if (B.natives.hbget != null)
            hbget = new NativeFunction(base.add(B.natives.hbget * 8).readPointer(),
                                       "pointer", ["pointer", "pointer"]);
        if (B.natives.hbsize != null)
            hbsize = new NativeFunction(base.add(B.natives.hbsize * 8).readPointer(),
                                        "int", ["pointer"]);
        return hbkeys !== null;
    } catch (e) { return false; }
}

// The map values are hxbit-generated proxies whose CLASS NAME states their own
// schema — measured 2026-08-05: hxbit.ObjProxy_OkillCount_Int_rank_Int for
// units, ObjProxy_OitemCount_Int_rank_Int for items. Layout (from hlboot):
//     obj@8(virtual)  bit@16(i32)  killCount@20(i32)  rank@24(i32)
// Both counters sit at @20 whatever they're called, so one reader serves both.
const PROXY_COUNT = 20, PROXY_RANK = 24;
function readProxy(v) {
    try {
        if (!v || v.isNull()) return null;
        const nm = typeName(v);
        if (!nm || nm.indexOf("ObjProxy_O") < 0) return null;
        return { count: v.add(PROXY_COUNT).readS32(),
                 rank: v.add(PROXY_RANK).readS32() };
    } catch (e) { return null; }
}

// GAME THREAD ONLY — hbkeys allocates an hl_varray.
// Returns {id: {count, rank}} so the host can write it out whole; `cap` bounds
// only what is LOGGED, never what is collected.
function dumpStringMap(label, mapObj, cap) {
    const out = {};
    if (!mapObj || mapObj.isNull()) { log("  " + label + ": (null map)"); return out; }
    const cls = typeName(mapObj);
    let h = null;
    try { h = mapObj.add(B.StringMap.h).readPointer(); } catch (e) {}
    if (!h || h.isNull()) { log("  " + label + ": " + cls + " but .h is null"); return out; }
    let n = -1;
    try { if (hbsize) n = hbsize(h); } catch (e) {}
    log("  " + label + ": " + cls + "  size=" + n);
    if (!hbkeys || !hbget) return out;
    let keys;
    try { keys = hbkeys(h); } catch (e) { log("    hbkeys threw " + e); return out; }
    if (!keys || keys.isNull()) return out;
    let shown = 0;
    try {
        // hl_varray: { t@0, at@8, size@16, pad@20 }, elements start at +24.
        const size = keys.add(16).readS32();
        for (let i = 0; i < size; i++) {
            const kb = keys.add(24 + i * 8).readPointer();
            if (!kb || kb.isNull()) continue;
            const ks = String(kb.readUtf16String());
            let v = null;
            try { v = hbget(h, kb); } catch (e) { continue; }
            const p = readProxy(v);
            if (p) out[ks] = p;
            if (shown < cap) {
                log("    " + ks + " = " + (p ? "kills=" + p.count + " rank=" + p.rank
                                             : describeVal(v)));
                shown++;
            }
        }
        if (size > cap) log("    ... " + (size - cap) + " more (all captured)");
    } catch (e) { log("    walk threw " + e); }
    return out;
}

// data.CodexNode is deliberately NOT read here any more — see the binding-trap
// note in drainKinds(). Its numbers (_progress / maxProgress / completed) are
// the same facts unitsProgress carries as (killCount, rank), and that store
// needs no call into the game at all.

// ---- state ----------------------------------------------------------------
let base = null, localHero = null, localName = null, armed = false, tick = 0;
let progressPtr = null;
let nearbyKinds = {};        // unit kind -> a live ent.Foe ptr (for its .inf)
let askedKinds = {};         // kind -> already dumped
let pendingKinds = [];       // kinds queued for a game-thread getUnitNode call

// ---- the sweep (TIMER — plain reads only) ---------------------------------
const FOE = {};
(OFF.foeClasses || []).forEach(function (c) { FOE[c] = 1; });

function sweep() {
    try {
        if (!localHero || localHero.isNull()) return;
        const layer = localHero.add(OFF.Hero.layer).readPointer();
        if (!layer || layer.isNull()) return;
        const arr = layer.add(OFF.GameLayer.units).readPointer();
        if (!arr || arr.isNull()) return;
        const A = OFF.ArrayObj;
        const n = arr.add(A.length).readS32();
        if (n <= 0 || n > 20000) return;
        const data = arr.add(A.array).readPointer();
        if (data.isNull()) return;
        const me = {
            x: localHero.add(OFF.Entity.posx).readDouble(),
            y: localHero.add(OFF.Entity.posy).readDouble(),
        };
        for (let i = 0; i < n; i++) {
            let e;
            try { e = data.add(A.data + i * 8).readPointer(); } catch (x) { continue; }
            if (!e || e.isNull() || e.compare(ptr("0x10000")) <= 0) continue;
            const cls = typeName(e);
            if (!cls || !FOE[cls]) continue;
            try {
                if (e.add(OFF.State.removed).readU8()) continue;
                const owner = e.add(OFF.Foe.summonOwner).readPointer();
                if (owner && !owner.isNull()) continue;      // pet, not a mob
                const dx = e.add(OFF.Entity.posx).readDouble() - me.x;
                const dy = e.add(OFF.Entity.posy).readDouble() - me.y;
                if (dx * dx + dy * dy > 250 * 250) continue;
                const kind = hlStr(e.add(OFF.Unit.kind).readPointer());
                if (!kind) continue;
                nearbyKinds[kind] = e;
                if (!askedKinds[kind] && pendingKinds.indexOf(kind) < 0
                    && pendingKinds.length < 24)
                    pendingKinds.push(kind);
            } catch (x) {}
        }
    } catch (e) {}
}

// ---- game-thread work ------------------------------------------------------
function drainKinds() {
    if (!pendingKinds.length) return;
    const kind = pendingKinds.shift();
    if (askedKinds[kind]) return;
    askedKinds[kind] = true;
    const foe = nearbyKinds[kind];
    log("");
    log("UNIT " + kind);
    // The CDB row. `flags` carries the NoCodex bit — the game's own "this mob
    // has no codex entry" marker — and `inherit` is how variants may fold into
    // one entry. Both are plain field reads, safe whether or not the codex has
    // been built.
    try {
        if (foe && hl_getField) {
            const inf = foe.add(OFF.Unit.inf).readPointer();
            if (inf && !inf.isNull()) {
                const flags = getField(inf, "flags");
                const texts = getField(inf, "texts");
                const inh = getField(inf, "inherit");
                let fv = null;
                try { fv = flags && !flags.isNull() ? flags.add(8).readS32() : null; } catch (e) {}
                log("  cdb id=" + describeVal(getField(inf, "id"))
                    + " name=" + (texts ? describeVal(getField(texts, "name")) : "?")
                    + " flags=" + fv
                    + (fv !== null ? " NoCodex=" + ((fv >> B.NoCodexBit) & 1) : "")
                    + " inherit=" + describeVal(inh));
            }
        }
    } catch (e) { log("  cdb read threw " + e); }
    // NOT called: data.$CodexData.getUnitNode / isInCodex / shouldShowUnit.
    // Round 1 (2026-08-05) tried them and they are the HL BINDING TRAP: a
    // binding's findex is a CLOSURE whose first argument is the receiver, so
    // getUnitNode(kindString) put the String where the class object belongs.
    // Measured symptom — isInCodex threw "access violation accessing
    // 0x10000003f" every time, and getUnitNode returned null or faulted on
    // 0x13/0x1a (a String's `length` read as a pointer). Reaching the class
    // object means reading an HL global, which nothing here does yet.
    //
    // It turned out not to matter: unitsProgress carries killCount AND rank
    // already, and the rank thresholds are constants in data.cdb, so the whole
    // feature reads without a single call into the game. Same family of mistake
    // as the glider's args[2] — dump what a value IS before building on it.
    if (progressPtr) {
        const p = unitProgressOf(kind);
        log("  unitsProgress[" + kind + "] = "
            + (p ? "kills=" + p.count + " rank=" + p.rank : "(absent)"));
    }
}

// One unit's replicated entry, by id. Pure reads apart from hbget.
function unitProgressOf(kind) {
    if (!progressPtr || !hbget) return null;
    try {
        const m = mapUnder(progressPtr.add(B.Progress.unitsProgress).readPointer());
        if (!m) return null;
        const h = m.add(B.StringMap.h).readPointer();
        return readProxy(hbget(h, Memory.allocUtf16String(kind)));
    } catch (e) { return null; }
}

function heartbeat() {
    tick++;
    if (!armed) {
        if (tick % 4 === 0) log("[waiting] localHero not latched yet.");
        return;
    }
    log("");
    log("---- tick " + tick + " ----");
    if (!progressPtr) {
        log("  !! Progress not reachable from hero (player or progress null)");
        return;
    }
    // The whole replicated store, so the SHAPE of a value is visible rather
    // than assumed: an Int kill count and a small object read very differently
    // here, and the feature's arithmetic depends on which it is.
    let units = {}, items = {};
    try {
        const md = progressPtr.add(B.Progress.unitsProgress).readPointer();
        log("  unitsProgress MapData @" + md + " type=" + typeName(md));
        units = dumpStringMap("unitsProgress", mapUnder(md), 12);
    } catch (e) { log("  unitsProgress dump threw " + e); }
    try {
        const ip = progressPtr.add(B.Progress.itemProgress).readPointer();
        items = dumpStringMap("itemProgress", mapUnder(ip), 4);
    } catch (e) {}
    // The whole table as structured data — every (killCount, rank) pair is a
    // constraint on which threshold set that unit uses. Re-sent periodically
    // rather than once, because on a FRESH character the interesting samples
    // are the ones that appear while you play, not the ones present at login.
    // The host overwrites the same file, so the last write is the fullest.
    if (Object.keys(units).length && (tick % DUMP_EVERY_TICKS) === 1) {
        send({ kind: "progressdump", units: units, items: items });
        log("  [sent progress dump: " + Object.keys(units).length
            + " units, " + Object.keys(items).length + " items]");
    }
}
const DUMP_EVERY_TICKS = 4;      // heartbeat is ~8s, so ~every 32s

// ---- kill-event candidates -------------------------------------------------
// Nine of them, all read-only. The item-pickup dead end is the reason for the
// spread: every plausible client hook there turned out to be server code, and
// the only way to find that out was to hook them all and watch.
function argDesc(p) {
    try {
        if (!p || p.isNull()) return "(null)";
        if (p.compare(ptr("0x10000")) <= 0) return "int:" + p.toInt32();
        const nm = typeName(p);
        if (nm === "String") return JSON.stringify(hlStr(p));
        if (nm) return "<" + nm + ">";
        return p.toString();
    } catch (e) { return "?"; }
}

// Some of these are getters (getNbUnitKilled is read by the codex UI itself),
// so an unthrottled log would flood the game thread with send() calls and drown
// the events we're actually here for. Identical consecutive lines collapse, and
// each hook is capped — a hook that goes quiet after its cap says so once.
//
// The three codex/kill hooks get a much larger cap than the rest: on a fresh
// character every one of them is a THRESHOLD SAMPLE (kills 1,2,3... against the
// rank that results), which is the whole point of the run. The getters keep the
// small cap, since they are noise.
const HOOK_CAP_DEFAULT = 25, HOOK_CAP_EVENT = 400;
const hookSeen = {}, hookLast = {};
function hookLog(name, line, big) {
    if (hookLast[name] === line) return;
    hookLast[name] = line;
    const cap = big ? HOOK_CAP_EVENT : HOOK_CAP_DEFAULT;
    const n = (hookSeen[name] || 0) + 1;
    hookSeen[name] = n;
    if (n > cap) {
        if (n === cap + 1) log("EVT " + name + "  ... capped at " + cap);
        return;
    }
    log("EVT " + name + "  " + line);
}

// Which register carries the UNIT ID, per hook — measured round 1:
//   notifyUnitKilled__impl(this, unitId)
//   notifyCodexUnit__impl(this, notifKind, unitId)   notifKind: CodexDiscovered
//                                                    | CodexProgress | CodexMastered
//   onUnitCodexRankProgress__impl(this, unitId, rank)
// These fire ON THE GAME THREAD (game code calls them), so reading the unit's
// replicated entry right here is both safe and the point: it says whether the
// count has already been incremented by the time the event lands, which decides
// whether a popup can quote the new number inline or must wait a frame.
const UNIT_ARG = {
    "st.Player.notifyUnitKilled__impl": "rdx",
    "st.Player.notifyCodexUnit__impl": "r8",
    "st.Player.onUnitCodexRankProgress__impl": "rdx",
};

function hookAll() {
    let n = 0;
    for (const name in B.hooks) {
        const fi = B.hooks[name];
        if (fi == null) { log("!! no findex for " + name); continue; }
        const idReg = UNIT_ARG[name];
        try {
            Interceptor.attach(base.add(fi * 8).readPointer(), {
                onEnter: function () {
                    const c = this.context;
                    let extra = "";
                    if (idReg) {
                        try {
                            const id = hlStr(c[idReg]);
                            const p = id ? unitProgressOf(id) : null;
                            if (p) extra = "  -> NOW kills=" + p.count + " rank=" + p.rank;
                            else if (id) extra = "  -> NOW (absent)";
                        } catch (e) {}
                    }
                    hookLog(name, "rcx=" + argDesc(c.rcx)
                        + "  rdx=" + argDesc(c.rdx)
                        + "  r8=" + argDesc(c.r8)
                        + "  r9=" + argDesc(c.r9) + extra, !!idReg);
                }
            });
            n++;
        } catch (e) { log("!! hook failed for " + name + ": " + e); }
    }
    log("hooked " + n + "/" + Object.keys(B.hooks).length + " kill-event candidates");
}

function refreshLocalHero() {
    if (!base) return;
    for (const nm in DATA.funcs) {
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") {
                localHero = h;
                localName = hlStr(h.add(OFF.Hero.name).readPointer());
                progressPtr = progressFromHero(h);
                return;
            }
        } catch (e) {}
    }
}

// ui.BaseUI.displayWindow(ui, win): the second argument is the window. A codex
// window existing means initData has run, which is the gate getUnitNode needs.
// Hooked read-only — the meter already does exactly this for its game-menu
// awareness, so the pattern is proven rather than new.
const seenWindows = {};
function hookWindows() {
    const fi = DATA.ui_targets && DATA.ui_targets["ui.BaseUI.displayWindow"];
    if (fi == null) { log("!! displayWindow findex missing — codex gate unavailable"); return; }
    try {
        Interceptor.attach(base.add(fi * 8).readPointer(), {
            onEnter: function () {
                try {
                    const nm = typeName(this.context.rdx);
                    if (!nm) return;
                    if (!seenWindows[nm]) {
                        seenWindows[nm] = 1;
                        log("WINDOW " + nm);
                    }
                    if (!codexReady && CODEX_WIN_RX.test(nm)) {
                        codexReady = true;
                        askedKinds = {};      // re-ask now that nodes exist
                        log(">>> codex UI seen (" + nm + ") — node dumps enabled <<<");
                    }
                } catch (e) {}
            }
        });
        log("hooked ui.BaseUI.displayWindow (codex gate)");
    } catch (e) { log("!! window hook failed: " + e); }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (!setupFieldApi()) log("!! libhl field API unavailable");
    if (!setupMapApi(base)) log("!! map natives unavailable; map dumps will be skipped");

    hookAll();
    hookWindows();

    const camFi = DATA.cam_targets && DATA.cam_targets["client.BaseCamera.postUpdate"];
    if (camFi == null) { log("!! postUpdate findex missing"); return; }
    let due = true, slow = true;
    setInterval(function () { due = true; }, 1500);
    setInterval(function () { slow = true; }, 8000);
    Interceptor.attach(base.add(camFi * 8).readPointer(), {
        onEnter: function () {
            if (due) {
                due = false;
                refreshLocalHero();
                if (localHero && !armed) {
                    armed = true;
                    log(">>> PROBE ARMED <<< hero=" + (localName || "?")
                        + "  progress=" + (progressPtr ? "reachable" : "NOT REACHABLE"));
                }
                drainKinds();          // game thread: every HL call lives here
            }
            if (slow) { slow = false; heartbeat(); }
        }
    });
    setInterval(sweep, 2000);
}

setTimeout(main, 150);
