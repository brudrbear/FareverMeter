// critter_probe.js — CRITTER COLLECTION SPIKE.
//
// The static picture (hlboot 2026-08-07): critters are caught with a
// CaptureNet item ("Large Butterfly Net", captureChance 0.2) whose skill
// script calls `ownerHero?.tryCaptureCritter(hit.targetUnit)`. Two stores
// could carry "already collected":
//
//   st.player.Collection.pets : hxbit.ArrayProxyData   (account-wide, sibling
//       of the PROVEN mounts walk; Collection also has hasPet/addPet/equipPet)
//   st.player.Progress.pets   : hxbit.MapData          (per character, sibling
//       of the codex's unitsProgress on the same Progress object)
//
// Three questions, all client-side:
//   1. What do the two stores hold at rest? The pets ARRAY elements are typed
//      and printed (String unit kind? item id? something else), and the pets
//      MAP is enumerated — keys plus each value's class name, which for
//      hxbit ObjProxy classes declares its own schema.
//   2. What fires on a capture? tryCaptureCritter, notifyCapture__impl /
//      notifyCaptureMiss__impl, Collection.hasPet / addPet are hooked with
//      args logged. hasPet's argument is the key type the game itself uses.
//   3. Does either store change without a capture (equip, zone change)? The
//      sweep watches both lengths and re-dumps on any edge.
//
// Interceptors LOG ONLY — nothing is called, nothing is written. The one
// HL-call site is the hero lookup plus hbkeys/hbget for the map dump, both
// on the game thread inside the postUpdate hook, same as codex_probe.
//
// DATA + OFF + P are prepended by run_critter.py.

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

// GAME THREAD ONLY — same split as every probe; the timer never HL-calls.
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

// ---- argument description for the logged hooks -----------------------------
function describe(p) {
    try {
        if (!p || p.isNull()) return "null";
        // Small values are ints/bools riding in a pointer slot.
        if (p.compare(ptr("0x10000")) <= 0) return "int:" + p.toInt32();
        const tn = typeName(p);
        if (tn === "String") return "\"" + (hlStr(p) || "?") + "\"";
        if (tn === "ent.Hero") {
            let nm = null;
            try { nm = hlStr(p.add(P.Hero.name).readPointer()); } catch (e) {}
            return tn + "(" + (nm || "?") + ")";
        }
        if (tn && tn.lastIndexOf("ent.", 0) === 0) {
            let kd = null;
            try { kd = hlStr(p.add(P.Unit.kind).readPointer()); } catch (e) {}
            return tn + (kd ? "(" + kd + ")" : "");
        }
        if (tn) return tn;
        return p.toString();
    } catch (e) { return "err:" + e.message; }
}

// Log-with-cap per hook: captures are rare, but hasPet might ride a UI
// refresh loop — nothing here is allowed to flood if that guess is wrong.
const fireCount = {};

function attachLogged(name, findex) {
    if (findex == null) { log("!! no findex for " + name + " — skipped"); return; }
    let addr;
    try { addr = base.add(findex * 8).readPointer(); }
    catch (e) { log("!! " + name + " addr unreadable: " + e.message); return; }
    Interceptor.attach(addr, {
        onEnter: function (args) {
            const n = (fireCount[name] = (fireCount[name] || 0) + 1);
            if (n > 40) return;                    // cap, not silence
            let line = ">>> " + name + "  this=" + describe(args[0]);
            for (let i = 1; i <= 3; i++) {
                try { line += "  a" + i + "=" + describe(args[i]); }
                catch (e) { break; }
            }
            log(line + (n === 40 ? "   (cap reached — counts continue)" : ""));
        }
    });
    log("hooked " + name + " (findex " + findex + ")");
}

// ---- the two stores --------------------------------------------------------
function playerPtr() {
    const player = localHero.add(P.Hero.player).readPointer();
    return (player && !player.isNull()) ? player : null;
}

function collectionPtr() {
    const player = playerPtr();
    if (!player) return null;
    const acct = player.add(P.Player.accountProgress).readPointer();
    if (!acct || acct.isNull()) return null;
    return acct.add(P.AccountProgress.collection).readPointer();
}

// hxbit.ArrayProxyData -> .array (hl.types.ArrayDyn) -> .array (ArrayBase).
function proxyElements(proxy, label) {
    if (!proxy || proxy.isNull()) { log("  " + label + ": proxy NULL"); return null; }
    const dyn = proxy.add(P.ArrayProxyData.array).readPointer();
    if (!dyn || dyn.isNull()) { log("  " + label + ": ArrayDyn NULL"); return null; }
    const inner = dyn.add(P.ArrayDyn.array).readPointer();
    if (!inner || inner.isNull()) { log("  " + label + ": inner array NULL"); return null; }
    const cls = typeName(inner);
    if (cls === "hl.types.ArrayObj") {
        const n = inner.add(OFF.ArrayObj.length).readS32();
        if (n < 0 || n > 4096) { log("  " + label + ": bad length " + n); return null; }
        const data = inner.add(OFF.ArrayObj.array).readPointer();
        const out = [];
        for (let i = 0; i < n; i++)
            out.push(data.add(OFF.ArrayObj.data + i * 8).readPointer());
        return { cls: cls, els: out };
    }
    let raw = "?";
    try { raw = inner.readByteArray(48); } catch (e) {}
    log("  " + label + ": inner array class=" + (cls || "<null>") +
        " — unhandled; first bytes: " + raw);
    return { cls: cls, els: [] };
}

function elementDesc(e) {
    if (!e || e.isNull()) return "null";
    const tn = typeName(e);
    if (tn === "String") return "\"" + (hlStr(e) || "?") + "\"";
    let kind = null;
    try { kind = hlStr(e.add(P.Item.kind).readPointer()); } catch (x) {}
    return (tn || e.toString()) + (kind !== null ? " kind=" + kind : "");
}

// hxbit.MapData.map(@P.MapData.map) is a virtual; hl_vvirtual is
// { t@0, value@8, next@16 } so `value` is the haxe.ds.StringMap underneath,
// and StringMap.h is the native BytesMap that $std.hbkeys/hbget walk.
function mapUnder(mapData) {
    try {
        const v = mapData.add(P.MapData.map).readPointer();
        if (!v || v.isNull()) return null;
        const val = v.add(8).readPointer();
        return (val && !val.isNull()) ? val : null;
    } catch (e) { return null; }
}

let hbkeys = null, hbget = null;
function setupMapNatives() {
    try {
        if (P.natives.hbkeys != null)
            hbkeys = new NativeFunction(base.add(P.natives.hbkeys * 8).readPointer(),
                                        "pointer", ["pointer"]);
        if (P.natives.hbget != null)
            hbget = new NativeFunction(base.add(P.natives.hbget * 8).readPointer(),
                                       "pointer", ["pointer", "pointer"]);
        return hbkeys !== null && hbget !== null;
    } catch (e) { return false; }
}

// The map's value class is UNKNOWN until printed — an ObjProxy class name
// declares its own schema (the codex's was ObjProxy_OkillCount_Int_rank_Int),
// so the class name plus a few raw i32s is enough to derive the layout.
// GAME THREAD ONLY — hbkeys allocates an hl_varray.
function dumpPetsMap() {
    const player = playerPtr();
    if (!player) { log("  Progress.pets: no player"); return; }
    const prog = player.add(P.Player.progress).readPointer();
    if (!prog || prog.isNull()) { log("  Progress.pets: progress NULL"); return; }
    const md = prog.add(P.Progress.pets).readPointer();
    if (!md || md.isNull()) { log("  Progress.pets: MapData NULL"); return; }
    const sm = mapUnder(md);
    if (!sm) { log("  Progress.pets: map virtual empty"); return; }
    let h = null;
    try { h = sm.add(P.StringMap.h).readPointer(); } catch (e) {}
    if (!h || h.isNull()) { log("  Progress.pets: BytesMap NULL"); return; }
    if (!hbkeys || !hbget) { log("  Progress.pets: map natives unavailable"); return; }
    let keys;
    try { keys = hbkeys(h); } catch (e) { log("  Progress.pets: hbkeys threw " + e); return; }
    // hl_varray: { t@0, at@8, size@16 } with bytes* elements after the header.
    const n = keys.add(16).readS32();
    log("  Progress.pets MAP: " + n + " entries");
    for (let i = 0; i < n && i < 200; i++) {
        const kb = keys.add(24 + i * 8).readPointer();
        const key = kb.readUtf16String();
        let v = null;
        try { v = hbget(h, kb); } catch (e) {}
        if (!v || v.isNull()) { log("      [" + key + "] value NULL"); continue; }
        const cls = typeName(v);
        const ints = [];
        for (let o = 8; o <= 32; o += 4) {
            try { ints.push("@" + o + "=" + v.add(o).readS32()); } catch (e) { break; }
        }
        log("      [" + key + "] " + (cls || v.toString()) + "  " + ints.join(" "));
    }
}

function dumpStores() {
    log("--- critter store dump ---");
    const player = playerPtr();
    log("  player = " + describe(player) +
        (player
            ? "  name=" + (hlStr(player.add(P.Player.name).readPointer()) || "?")
              + "  isMe=" + player.add(P.Player.isMe).readU8()
            : ""));
    const coll = collectionPtr();
    log("  collection = " + describe(coll));
    if (coll && !coll.isNull()) {
        // mounts included as a known-good control for the walk itself.
        for (const label of ["mounts", "pets"]) {
            const r = proxyElements(coll.add(P.Collection[label]).readPointer(), label);
            if (!r) continue;
            log("  Collection." + label + ": " + r.els.length + " entries (" + r.cls + ")");
            for (let i = 0; i < r.els.length && i < 200; i++)
                log("      [" + i + "] " + elementDesc(r.els[i]));
        }
    }
    dumpPetsMap();
    log("--- end critter store dump ---");
    return true;
}

// ---- the sweep -------------------------------------------------------------
let ticks = 0;
let chainDumped = false;
let lastCounts = "";
let lastPetsLen = -1;
let redumpDue = false;

function petsArrayLen() {
    const coll = collectionPtr();
    if (!coll || coll.isNull()) return -1;
    const proxy = coll.add(P.Collection.pets).readPointer();
    if (!proxy || proxy.isNull()) return -1;
    const dyn = proxy.add(P.ArrayProxyData.array).readPointer();
    if (!dyn || dyn.isNull()) return -1;
    const inner = dyn.add(P.ArrayDyn.array).readPointer();
    if (!inner || inner.isNull() || typeName(inner) !== "hl.types.ArrayObj") return -1;
    return inner.add(OFF.ArrayObj.length).readS32();
}

function sweep() {
    try {
        ticks++;
        if (!localHero || localHero.isNull()) {
            if (ticks % 10 === 0)
                log("[wait] frames=" + frames + " heroTries=" + heroTries +
                    " — no ent.Hero yet (menu/loading screen is expected).");
            return;
        }
        if (!chainDumped) {
            chainDumped = true;
            log("localHero = " + localHero + " (after " + frames + " frames)");
            redumpDue = true;   // first dump happens on the game thread below
        }
        const n = petsArrayLen();
        if (n !== lastPetsLen) {
            if (lastPetsLen >= 0) {
                log("=== Collection.pets length: " + lastPetsLen + " -> " + n);
                redumpDue = true;
            }
            lastPetsLen = n;
        }
        if (ticks % 20 === 0) {
            const parts = [];
            for (const k in fireCount) parts.push(k + " x" + fireCount[k]);
            const line = parts.sort().join("  ");
            if (line && line !== lastCounts) {
                log("[counts] " + line);
                lastCounts = line;
            }
        }
    } catch (e) { log("sweep ERR " + e); }
}

// The dump HL-calls (hbkeys/hbget), so it runs inside postUpdate — the sweep
// only raises the flag. First dump doubles as the ARMED gate.
let armed = false;
function onGameThreadTick() {
    frames++;
    refreshLocalHeroOnGameThread();
    if (redumpDue && localHero && !localHero.isNull()) {
        redumpDue = false;
        dumpStores();
        if (!armed) {
            armed = true;
            log("PROBE ARMED — stores dumped, capture family watched. Do the "
                + "in-game steps now.");
        }
    }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (P.fn.postUpdate == null) {
        log("!! no postUpdate findex — no game-thread anchor; refusing.");
        return;
    }
    if (!setupMapNatives())
        log("!! hbkeys/hbget unresolved — Progress.pets map dump disabled.");
    Interceptor.attach(base.add(P.fn.postUpdate * 8).readPointer(), {
        onEnter: function () { onGameThreadTick(); }
    });
    for (const name in P.hooks) attachLogged(name, P.hooks[name]);
    log("waiting for the hero. Nothing is armed until the ARMED line prints.");
    setInterval(sweep, 500);
}

setTimeout(main, 0);
