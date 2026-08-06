// gather_probe.js — ore/herb node discovery for the minimap.
//
// Static analysis (hlboot.dat) says gathering nodes are ONE class:
//
//     ent.interactible.Gatherable <- ent.Element <- ent.Interactible <- ...
//
// so every read the minimap sweep already does on an Element (kind, stateId,
// currentVisualState, enabled, position) applies unchanged, and the class adds
// gatherInf / respawnTime / lastLocalInteractTime / hitPoints (replicated) /
// affixId / gatherers at the tail. What static analysis CANNOT say is the
// live state machine — and that is exactly the thing the minimap filter needs:
//
//   1. WHERE nodes live: which of the layer's three arrays carries them.
//   2. WHAT "mineable" looks like in memory: does a mined-out node keep
//      hitPoints=0 and sit on a respawn timer, does stateId or
//      currentVisualState move (the chest lesson: stateId does NOT move on
//      open — only the visual does), does `removed` flip, does it leave the
//      array entirely?
//   3. WHAT gates gathering: getNoActionReason's vocabulary when you walk up
//      without a pickaxe / with the wrong tool — that's the "mineable BY ME"
//      signal, if one exists client-side.
//   4. ORE vs HERB: whether Element.kind carries the GatherableKind id
//      (Ore_Copper_Small, Madrigold_Large, ...) and what the CDB row's
//      requiredTool / texts.type say per kind.
//
// The probe: a 2s timer censuses all Gatherables in the layer (plain pointer
// reads only — TIMER-SAFE per the thread rule) and logs any
// field that CHANGES on a tracked node. Event hooks on hit / set_hitPoints /
// consume / respawn / tryRequestInteraction / getNoActionReason catch the
// transitions the 2s sampling would blur. CDB rows (display name, required
// tool, props) resolve on the camera hook's game thread, once per kind.
//
// DATA + OFF + B are prepended by run_gather.py.

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

const GATHER = {};
(B.gatherClasses || []).forEach(function (c) { GATHER[c] = 1; });

// ---- CDB row access (GAME THREAD ONLY — drained on the camera hook) ----
// hl_obj_get_field boxes numeric fields, which allocates on the GC — fine on
// the registered game thread, fatal anywhere else. Same rule as skill names.
let hl_getField = null, hl_hashUtf8 = null;
const fieldHash = {};
const CDB_FIELDS = ["id", "requiredTool", "requiredToolId", "props", "texts",
                    "name", "desc", "type", "hitPoints", "respawnTime",
                    "gatherSkill", "gatherSkillId", "size", "affixChance",
                    "loot", "hitLoot", "flags"];
function setupNameApi() {
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
// A boxed vdynamic from hl_obj_get_field: type* at 0, value at 8. Decode by
// the type kind rather than guessing — an unfamiliar kind prints as kindN so
// it is visible rather than silently dropped.
function describeVal(p) {
    try {
        if (!p || p.isNull()) return "(null)";
        const t = p.readPointer();
        const k = t.readU32();
        if (k === 3) return String(p.add(8).readS32());          // i32
        if (k === 4) return String(p.add(8).readS64());          // i64
        if (k === 5) return p.add(8).readFloat().toFixed(3);     // f32
        if (k === 6) return String(p.add(8).readDouble());       // f64
        if (k === 7) return p.add(8).readU8() ? "true" : "false";
        if (k === 11 || k === 21) {                              // obj/struct
            const nm = typeName(p);
            if (nm === "String") return JSON.stringify(hlStr(p));
            return "<" + nm + ">";
        }
        if (k === 15 || k === 16) return "<virtual>";            // needs getField
        if (k === 18) return "<enum idx=" + p.add(8).readS32() + ">";
        return "<kind" + k + ">";
    } catch (e) { return "(unreadable)"; }
}

// ---- per-kind CDB resolution, queued by the sweep, drained on game thread ----
const kindInfo = {};        // kind -> true once dumped
let kindPending = [];       // [{kind, ptr}] node ptrs whose gatherInf to read
function queueKind(kind, nodePtr) {
    if (!kind || kind in kindInfo) return;
    for (let i = 0; i < kindPending.length; i++)
        if (kindPending[i].kind === kind) return;
    kindPending.push({ kind: kind, ptr: nodePtr });
}
function drainKinds() {
    if (!kindPending.length || !hl_getField) return;
    for (let i = 0; i < 2 && kindPending.length; i++) {
        const job = kindPending.shift();
        if (job.kind in kindInfo) continue;
        kindInfo[job.kind] = true;
        try {
            const inf = job.ptr.add(B.G.gatherInf).readPointer();
            if (!inf || inf.isNull()) { log("CDB " + job.kind + ": gatherInf is null"); continue; }
            const texts = getField(inf, "texts");
            const props = getField(inf, "props");
            let line = "CDB " + job.kind
                + "  name=" + (texts ? describeVal(getField(texts, "name")) : "?")
                + "  type=" + (texts ? describeVal(getField(texts, "type")) : "?")
                + "  tool=" + describeVal(getField(inf, "requiredTool"))
                + "/" + describeVal(getField(inf, "requiredToolId"));
            if (props && !props.isNull())
                line += "  props{hp=" + describeVal(getField(props, "hitPoints"))
                    + " respawn=" + describeVal(getField(props, "respawnTime"))
                    + " gatherSkill=" + describeVal(getField(props, "gatherSkill"))
                    + "/" + describeVal(getField(props, "gatherSkillId"))
                    + " size=" + describeVal(getField(props, "size"))
                    + " affix%=" + describeVal(getField(props, "affixChance")) + "}";
            log(line);
        } catch (e) { log("CDB " + job.kind + ": failed (" + e + ")"); }
    }
}

// ---- the census (TIMER — plain reads only) ----
let base = null, localHero = null, localName = null, armed = false, tick = 0;
const tracked = {};   // ptr -> last snapshot {kind, hp, st, vis, en, rm, resp}
let changeLines = 0;
const CHANGE_CAP = 40;         // per tick, so a zone load can't flood the log

function snapNode(e, arrays) {
    const k = e.toString();
    const s = {
        kind: hlStr(e.add(OFF.Element.kind).readPointer()),
        st: hlStr(e.add(OFF.Element.stateId).readPointer()),
        vis: hlStr(e.add(OFF.Element.currentVisualState).readPointer()),
        en: e.add(OFF.Interactible.enabled).readU8(),
        rm: e.add(OFF.State.removed).readU8(),
        hp: e.add(B.G.hitPoints).readDouble(),
        resp: e.add(B.G.respawnTime).readDouble(),
        affix: e.add(B.G.affixId).readS32(),
        x: e.add(OFF.Entity.posx).readDouble(),
        y: e.add(OFF.Entity.posy).readDouble(),
        z: e.add(OFF.Entity.posz).readDouble(),
        arrays: arrays,
    };
    const old = tracked[k];
    if (old && changeLines < CHANGE_CAP) {
        const diffs = [];
        if (old.hp !== s.hp) diffs.push("hp " + old.hp + "->" + s.hp);
        if (old.st !== s.st) diffs.push("stateId " + old.st + "->" + s.st);
        if (old.vis !== s.vis) diffs.push("visual " + old.vis + "->" + s.vis);
        if (old.en !== s.en) diffs.push("enabled " + old.en + "->" + s.en);
        if (old.rm !== s.rm) diffs.push("removed " + old.rm + "->" + s.rm);
        if (old.affix !== s.affix) diffs.push("affixId " + old.affix + "->" + s.affix);
        if (diffs.length) {
            log("  CHANGE " + (s.kind || "?") + " @" + k + "  " + diffs.join(", "));
            changeLines++;
        }
    }
    tracked[k] = s;
    queueKind(s.kind, e);
    return s;
}

function walk(arrPtr, found, arrayTag) {
    const A = OFF.ArrayObj;
    if (!arrPtr || arrPtr.isNull()) return;
    const n = arrPtr.add(A.length).readS32();
    if (n <= 0 || n > 20000) return;
    const data = arrPtr.add(A.array).readPointer();
    if (data.isNull()) return;
    for (let i = 0; i < n; i++) {
        let e;
        try { e = data.add(A.data + i * 8).readPointer(); } catch (x) { continue; }
        if (!e || e.isNull() || e.compare(ptr("0x10000")) <= 0) continue;
        const cls = typeName(e);
        if (!cls || !GATHER[cls]) continue;
        const k = e.toString();
        if (found[k]) { found[k].arrays += arrayTag; continue; }
        try { found[k] = snapNode(e, arrayTag); } catch (x) {}
    }
}

function census() {
    try {
        if (!localHero || localHero.isNull()) return;
        const layer = localHero.add(OFF.Hero.layer).readPointer();
        if (!layer || layer.isNull()) return;
        const G = OFF.GameLayer;
        const found = {};
        changeLines = 0;
        walk(layer.add(G.units).readPointer(), found, "u");
        walk(layer.add(G.interactibles).readPointer(), found, "i");
        walk(layer.add(G.entities).readPointer(), found, "e");
        // Nodes that vanished from the arrays since last census — despawn is a
        // possible answer to "what does mined-out look like", so it has to be
        // seen, not inferred.
        for (const k in tracked) {
            if (!found[k] && !tracked[k].gone) {
                tracked[k].gone = true;
                log("  GONE " + (tracked[k].kind || "?") + " @" + k
                    + "  (left the layer arrays; last hp=" + tracked[k].hp
                    + " stateId=" + tracked[k].st + " visual=" + tracked[k].vis + ")");
            } else if (found[k]) {
                tracked[k].gone = false;
            }
        }
        lastCensus = found;
    } catch (e) {}
}

let lastCensus = {};

function report() {
    tick++;
    if (!armed) {
        if (tick % 4 === 0) log("[waiting] localHero not latched yet.");
        return;
    }
    const keys = Object.keys(lastCensus);
    const byKind = {};
    for (const k of keys) {
        const s = lastCensus[k];
        const kk = s.kind || "?";
        let b = byKind[kk];
        if (!b) b = byKind[kk] = { n: 0, hp0: 0, states: {} };
        b.n++;
        if (s.hp <= 0) b.hp0++;
        const sig = "st=" + s.st + " vis=" + s.vis + " en=" + s.en + " arr=" + s.arrays;
        b.states[sig] = (b.states[sig] || 0) + 1;
    }
    log("");
    log("---- tick " + tick + "   gatherables in layer: " + keys.length + " ----");
    for (const kk of Object.keys(byKind).sort()) {
        const b = byKind[kk];
        log("  " + kk.padEnd(26) + " x" + String(b.n).padStart(3)
            + (b.hp0 ? "  (" + b.hp0 + " at hp<=0)" : ""));
        for (const sig of Object.keys(b.states).sort())
            log("      x" + byKind[kk].states[sig] + "  " + sig);
    }
    // The close-ups: full field detail on whatever Brudr is standing next to.
    let me = null;
    try {
        me = { x: localHero.add(OFF.Entity.posx).readDouble(),
               y: localHero.add(OFF.Entity.posy).readDouble() };
    } catch (e) {}
    if (me) {
        const near = keys.map(function (k) {
            const s = lastCensus[k];
            return { k: k, s: s, d: Math.hypot(s.x - me.x, s.y - me.y) };
        }).filter(function (r) { return r.d < 120; })
          .sort(function (a, b) { return a.d - b.d; });
        for (const r of near.slice(0, 8)) {
            const s = r.s;
            log("  NEAR " + (s.kind || "?") + " @" + r.k
                + "  d=" + r.d.toFixed(0)
                + "  hp=" + s.hp + "  respawnTime=" + s.resp
                + "  stateId=" + s.st + "  visual=" + s.vis
                + "  enabled=" + s.en + "  removed=" + s.rm
                + "  affixId=" + s.affix + "  arrays=" + s.arrays);
        }
    }
}

// ---- event hooks (game thread) ----
// getNoActionReason likely runs per-frame while the interact popup is up —
// log only when the answer CHANGES for a node, or the log drowns.
const lastReason = {};
function hookAll() {
    function hook(name, fns) {
        const fi = B.fn[name];
        if (fi == null) { log("!! no findex for " + name); return; }
        try {
            Interceptor.attach(base.add(fi * 8).readPointer(), fns);
            log("hooked ent.interactible.Gatherable." + name + " (findex " + fi + ")");
        } catch (e) { log("!! hook failed for " + name + ": " + e); }
    }
    function nodeTag(p) {
        let kind = null, hp = null;
        try { kind = hlStr(p.add(OFF.Element.kind).readPointer()); } catch (e) {}
        try { hp = p.add(B.G.hitPoints).readDouble(); } catch (e) {}
        return (kind || "?") + " @" + p.toString() + " hp=" + hp;
    }
    hook("hit", { onEnter: function () { log("EVT hit           " + nodeTag(this.context.rcx)); } });
    hook("consume", { onEnter: function () { log("EVT consume       " + nodeTag(this.context.rcx)); } });
    hook("respawn", { onEnter: function () { log("EVT respawn       " + nodeTag(this.context.rcx)); } });
    hook("tryRequestInteraction", { onEnter: function () { log("EVT tryInteract   " + nodeTag(this.context.rcx)); } });
    hook("set_hitPoints", {
        onEnter: function () {
            // rdx is the new value for a scalar setter — but read the field on
            // leave instead of trusting the register layout for an f64 arg.
            this.node = this.context.rcx;
            try { this.before = this.node.add(B.G.hitPoints).readDouble(); } catch (e) { this.before = null; }
        },
        onLeave: function () {
            try {
                const after = this.node.add(B.G.hitPoints).readDouble();
                if (after !== this.before)
                    log("EVT set_hitPoints " + nodeTag(this.node) + "  (" + this.before + " -> " + after + ")");
            } catch (e) {}
        }
    });
    hook("getNoActionReason", {
        onEnter: function () { this.node = this.context.rcx; },
        onLeave: function (retval) {
            try {
                const r = hlStr(retval) || (retval.isNull() ? "(null=no objection)" : "<" + typeName(retval) + ">");
                const k = this.node.toString();
                if (lastReason[k] !== r) {
                    lastReason[k] = r;
                    log("EVT noActionReason " + hlStr(this.node.add(OFF.Element.kind).readPointer())
                        + " @" + k + " -> " + r);
                }
            } catch (e) {}
        }
    });
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
                return;
            }
        } catch (e) {}
    }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (!setupNameApi()) log("!! libhl field API unavailable; CDB dumps will be skipped");

    hookAll();

    const camFi = DATA.cam_targets && DATA.cam_targets["client.BaseCamera.postUpdate"];
    if (camFi == null) { log("!! postUpdate findex missing"); return; }
    let due = true;
    setInterval(function () { due = true; }, 3000);
    Interceptor.attach(base.add(camFi * 8).readPointer(), {
        onEnter: function () {
            drainKinds();                      // game thread: CDB reads live here
            if (!due) return;
            due = false;
            refreshLocalHero();
            if (localHero && !armed) {
                armed = true;
                log(">>> PROBE ARMED <<< hero=" + (localName || "?")
                    + "  censusing gatherables. Walk to an ore or herb node.");
            }
        }
    });
    setInterval(census, 2000);
    setInterval(report, 6000);
}

setTimeout(main, 150);
