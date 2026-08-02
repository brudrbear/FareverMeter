// glider_equip_probe.js — GLIDER RE-EQUIP PROOF (round 3).
//
// Rounds 1-2 measured: the deploy chain carries no glider id (nothing to
// arg-swap), the glider model is pre-spawned at equip time, and the UI's
// persistent equip is Collection.equipItem(kind, infRow, 65535) — the same
// call as mounts. Brudr chose the real-re-equip design: the meter performs
// that same call with a random favorite, so the change replicates and other
// players see it. This probe proves the mechanism in three gated stages:
//
//   1. PASSIVE CAPTURE — cdb.IndexId.resolve is hooked enter+leave; the
//      first item-kind id through it identifies the Data.item index
//      instance (it fires at spawn for Glider_Generic). A manual equip is
//      captured with FOUR arg slots logged (arity check) and its a2 row.
//   2. ROW MAP (plain reads only) — the IndexId's `all` array (@8, an
//      ArrayDyn of every CDB item row) is walked, each row's id read via
//      libhl's hl_obj_get_field — the same C API the meter's name lookup
//      already uses. rowMap[capturedKind] must equal the captured a2: that
//      proves row identity without calling any HL bytecode. (Round 3b tried
//      CALLING resolve — access violation, cleanly caught; the walk
//      replaces it.)
//   3. AUTO-EQUIP — armed ONLY via rpc.exports.go() (the host calls it when
//      the GO file appears — rpc instead of recv() because a pending recv
//      is the prime suspect for the round-3b unload wedge), and only if the
//      map proof passed. On each glide-end of the LOCAL hero (isMe walked
//      from the hooked object, never cached), the next camera frame calls
//      equipItem(collection, <random other unlocked glider>, row, 65535).
//      5s cooldown, hard cap 5 per session.
//
// The String pointers passed to equipItem come from the collection's own
// gliders array — GC-rooted, and HL's GC doesn't move objects.
//
// DATA + OFF + P are prepended by run_glider_equip.py.

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
let frames = 0;

function refreshLocalHeroOnGameThread() {
    if (localHero && !localHero.isNull()) return;
    for (const nm in DATA.funcs) {
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") { localHero = h; return; }
        } catch (e) {}
    }
}

function describe(p) {
    try {
        if (!p || p.isNull()) return "null";
        if (p.compare(ptr("0x10000")) <= 0) return "int:" + p.toInt32();
        const tn = typeName(p);
        if (tn === "String") return "\"" + (hlStr(p) || "?") + "\"";
        if (tn === "ent.Hero") {
            let nm = null;
            try { nm = hlStr(p.add(P.Hero.name).readPointer()); } catch (e) {}
            return tn + "(" + (nm || "?") + ")";
        }
        if (tn) return tn;
        return p.toString();
    } catch (e) { return "err:" + e.message; }
}

function heroIsMe(hero) {
    try {
        const player = hero.add(P.Hero.player).readPointer();
        if (!player || player.isNull()) return false;
        return player.add(P.Player.isMe).readU8() === 1;
    } catch (e) { return false; }
}

function collectionOf(hero) {
    try {
        const player = hero.add(P.Hero.player).readPointer();
        const acct = player.add(P.Player.accountProgress).readPointer();
        return acct.add(P.AccountProgress.collection).readPointer();
    } catch (e) { return null; }
}

// {kind -> String ptr} from the collection's gliders array (GC-rooted).
function readGliderKinds(hero) {
    const out = {};
    try {
        const coll = collectionOf(hero);
        const proxy = coll.add(P.Collection.gliders).readPointer();
        const dyn = proxy.add(P.ArrayProxyData.array).readPointer();
        const inner = dyn.add(P.ArrayDyn.array).readPointer();
        if (typeName(inner) !== "hl.types.ArrayObj") return out;
        const n = inner.add(OFF.ArrayObj.length).readS32();
        if (n < 0 || n > 4096) return out;
        const data = inner.add(OFF.ArrayObj.array).readPointer();
        for (let i = 0; i < n; i++) {
            const s = data.add(OFF.ArrayObj.data + i * 8).readPointer();
            const k = hlStr(s);
            if (k) out[k] = s;
        }
    } catch (e) {}
    return out;
}

// ---- the row map: hl_obj_get_field over IndexId.all (plain reads) ----------
let hl_getField = null;
let idHash = 0;

function setupFieldApi() {
    try {
        const m = Process.findModuleByName("libhl.dll");
        hl_getField = new NativeFunction(m.findExportByName("hl_obj_get_field"),
                                         "pointer", ["pointer", "int"]);
        const hasher = new NativeFunction(m.findExportByName("hl_hash_utf8"),
                                          "int", ["pointer"]);
        idHash = hasher(Memory.allocUtf8String("id"));
        return true;
    } catch (e) { return false; }
}

// ---- state -----------------------------------------------------------------
let goArmed = false;              // set by the host via rpc.exports.go()
let capturedKind = null;          // last manual glider equip: kind string
let capturedInf = null;           // last manual glider equip: a2 (the row)
let indexIdThis = null;           // VERIFIED Data.item cdb.IndexId instance
const candidates = [];            // unverified IndexId instances, in order
const candidateSeen = {};         // ptr string -> true (dedupe)
let rowMap = {};                  // id -> row, walked from the verified inst
let rowMapBuilt = false;
let typeScanDone = false;         // heap scan for sibling IndexId instances
let selfTested = false;
let mapProven = false;
let pendingEquip = null;          // kind chosen at glide-end, fired next frame
let lastAutoEquip = 0;
let autoEquipCount = 0;
let lastGlideUp = false;
const AUTO_CAP = 5;
const COOLDOWN_MS = 5000;

// ---- hooks -----------------------------------------------------------------
function hookEquipItem() {
    const fi = P.hooks["Collection.equipItem"];
    if (fi == null) { log("!! Collection.equipItem missing"); return; }
    Interceptor.attach(base.add(fi * 8).readPointer(), {
        onEnter: function (args) {
            let line = ">>> Collection.equipItem  this=" + describe(args[0]);
            for (let i = 1; i <= 4; i++) {
                try { line += "  a" + i + "=" + describe(args[i]); }
                catch (e) { break; }
            }
            log(line);
            try {
                const kind = hlStr(args[1]);
                if (kind && kind.indexOf("Glider_") === 0) {
                    capturedKind = kind;
                    capturedInf = args[2];
                    selfTested = false;    // re-run against the new capture
                    log("[capture] manual equip: " + kind + " row=" + args[2]);
                }
            } catch (e) {}
        }
    });
    log("hooked Collection.equipItem");
}

function hookResolve() {
    const fi = P.hooks["IndexId.resolve"];
    if (fi == null) { log("!! cdb.IndexId.resolve missing — cannot prove the "
                          + "resolver; auto-equip will never arm."); return; }
    Interceptor.attach(base.add(fi * 8).readPointer(), {
        onEnter: function (args) {
            // Round 3c latched the FIRST Glider_/Mount_ id — and captured the
            // FXSET sheet, because "Glider_Generic" is an FxSetKind too. Now
            // every such caller is only a CANDIDATE; verifyCandidates() walks
            // each one and demands real glider item kinds in its rows.
            try {
                if (indexIdThis) return;
                const id = hlStr(args[1]);
                if (id && (id.indexOf("Glider_") === 0
                           || id.indexOf("Mount_") === 0)) {
                    const key = args[0].toString();
                    if (!candidateSeen[key]) {
                        candidateSeen[key] = true;
                        candidates.push(args[0]);
                        log("[candidate] IndexId " + key
                            + " (via resolve(\"" + id + "\"))");
                    }
                }
            } catch (e) {}
        }
    });
    log("hooked cdb.IndexId.resolve (candidate capture)");
}

function hookGlideEdge() {
    const fi = P.hooks["Hero.toggleGlide"];
    if (fi == null) { log("!! Hero.toggleGlide missing"); return; }
    Interceptor.attach(base.add(fi * 8).readPointer(), {
        onEnter: function (args) {
            try {
                const up = args[1] && !args[1].isNull();
                if (!heroIsMe(args[0])) return;
                log("=== local glide " + (up ? "START" : "END"));
                if (up) { lastGlideUp = true; return; }
                if (!lastGlideUp) return;
                lastGlideUp = false;
                if (!goArmed) { log("[skip] glide end, but GO not armed"); return; }
                if (!mapProven) { log("[skip] row map unproven"); return; }
                if (autoEquipCount >= AUTO_CAP) { log("[skip] cap reached"); return; }
                const now = Date.now();
                if (now - lastAutoEquip < COOLDOWN_MS) { log("[skip] cooldown"); return; }
                // Pick a random unlocked glider different from the current
                // one, from the hooked hero's own collection — mapped kinds
                // only, so the fire step never needs a fallback.
                const pool = readGliderKinds(args[0]);
                const kinds = Object.keys(pool).filter(function (k) {
                    return k !== capturedKind && (k in rowMap);
                });
                if (!kinds.length) { log("[skip] no alternative gliders"); return; }
                const pick = kinds[Math.floor(Math.random() * kinds.length)];
                pendingEquip = { kind: pick, str: pool[pick], hero: args[0] };
                lastAutoEquip = now;
                log("[auto] glide end -> will equip " + pick + " next frame");
            } catch (e) { log("glide-edge ERR " + e); }
        }
    });
    log("hooked Hero.toggleGlide (glide-end trigger)");
}

// Rebuild observers — the equip should retrigger the gear-display path.
function hookDisplay() {
    ["UnitView.displayGearSlot", "UnitView.loadGearProps"].forEach(function (name) {
        const fi = P.hooks[name];
        if (fi == null) return;
        Interceptor.attach(base.add(fi * 8).readPointer(), {
            onEnter: function (args) {
                try {
                    const a1 = describe(args[1]);
                    if (a1.indexOf("Glider") >= 0)
                        log(">>> " + name + "  a1=" + a1);
                } catch (e) {}
            }
        });
    });
    log("hooked gear-display observers");
}

// ---- game-thread work (called from the camera hook) ------------------------
// Walk a candidate's `all` array (@P.IndexId.all, an ArrayDyn of rows) and
// read each row's id via hl_obj_get_field. Plain reads plus a C helper — no
// HL bytecode is ever called. Returns {id -> row} or null.
function walkAll(inst) {
    try {
        const dyn = inst.add(P.IndexId.all).readPointer();
        if (!dyn || dyn.isNull()) return null;
        const inner = dyn.add(P.ArrayDyn.array).readPointer();
        if (!inner || inner.isNull()
                || typeName(inner) !== "hl.types.ArrayObj") return null;
        const n = inner.add(OFF.ArrayObj.length).readS32();
        if (n <= 0 || n > 4096) return null;
        const data = inner.add(OFF.ArrayObj.array).readPointer();
        const map = {};
        for (let i = 0; i < n; i++) {
            const row = data.add(OFF.ArrayObj.data + i * 8).readPointer();
            if (!row || row.isNull()) continue;
            const id = hlStr(hl_getField(row, idHash));
            if (id) map[id] = row;
        }
        return map;
    } catch (e) { return null; }
}

// The item sheet is bulk-resolved at game boot, so its resolve never fires
// while we're attached (measured round 3c: previews and equips produced no
// new candidates). Instead, the first REJECTED candidate seeds a heap scan:
// every heap object whose first quadword is the same hl_type* is a sibling
// cdb.IndexId instance. Pure memory reads, off the game thread; hits are
// fingerprinted on the game thread like any other candidate. Stack copies
// and stale pointers walk-fail and get rejected harmlessly.
function typeScan(typePtr) {
    try {
        const pat = ptrPattern(typePtr);
        let found = 0;
        const ranges = Process.enumerateRanges("rw-")
            .filter(function (r) { return r.size < 0x4000000; });
        for (const r of ranges) {
            let mm;
            try { mm = Memory.scanSync(r.base, r.size, pat); }
            catch (e) { continue; }
            for (const m of mm) {
                const key = m.address.toString();
                if (candidateSeen[key]) continue;
                candidateSeen[key] = true;
                candidates.push(m.address);
                found++;
            }
        }
        log("[scan] " + found + " same-type IndexId candidates queued");
    } catch (e) { log("[scan] ERR " + e); }
}

// A candidate is Data.item iff its rows contain the player's own glider
// kinds — 5 hits out of a 31-kind pool is an unforgeable fingerprint (the
// fxset sheet that fooled round 3c shares "Glider_Generic" but not the
// per-item kinds). Capped per frame: a scan can queue hundreds of hits and
// each walk is ~650 field reads.
function verifyCandidates() {
    if (rowMapBuilt || !hl_getField || !localHero || !candidates.length) return;
    const pool = Object.keys(readGliderKinds(localHero));
    if (pool.length < 5) return;
    let budget = 3;
    while (candidates.length && budget-- > 0) {
        const inst = candidates.shift();
        const map = walkAll(inst);
        if (!map) continue;               // scan noise — expected, stay quiet
        let hits = 0;
        for (const k of pool) if (k in map) hits++;
        const total = Object.keys(map).length;
        if (hits >= 5) {
            indexIdThis = inst;
            rowMap = map;
            rowMapBuilt = true;
            candidates.length = 0;
            log("[rowmap] VERIFIED Data.item = " + inst + " — " + total
                + " rows, " + hits + "/" + pool.length
                + " of the player's gliders present");
            return;
        }
        log("[verify] " + inst + " rejected: " + total + " rows, only "
            + hits + " glider kinds");
        if (!typeScanDone) {
            typeScanDone = true;
            const tp = inst.readPointer();
            log("[scan] seeding heap scan from rejected candidate's type "
                + tp);
            setTimeout(function () { typeScan(tp); }, 0);
        }
    }
}

function selfTestOnGameThread() {
    // Once per manual capture: the walked row for the SAME kind Brudr
    // equipped must be the row the UI passed. Pure comparison, no calls.
    if (selfTested || !capturedKind || !capturedInf || !rowMapBuilt) return;
    selfTested = true;
    const row = rowMap[capturedKind];
    mapProven = !!(row && capturedInf && row.equals(capturedInf));
    log("[self-test] rowMap[\"" + capturedKind + "\"] = " + row
        + "  captured row = " + capturedInf + "  MATCH=" + mapProven);
    log(mapProven
        ? "ROW MAP PROVEN — review the log, then create the GO file to arm "
          + "the auto-equip."
        : "!! self-test failed — auto-equip stays disarmed.");
}

function fireAutoEquipOnGameThread() {
    if (!pendingEquip) return;
    const job = pendingEquip;
    pendingEquip = null;
    try {
        if (!mapProven) { log("[auto] row map unproven — refusing"); return; }
        if (!heroIsMe(job.hero)) { log("[auto] hero not me — refusing"); return; }
        const coll = collectionOf(job.hero);
        if (!coll || coll.isNull()) { log("[auto] no collection"); return; }
        const row = rowMap[job.kind];
        if (!row || row.isNull()) { log("[auto] no row for " + job.kind); return; }
        const addr = base.add(P.hooks["Collection.equipItem"] * 8).readPointer();
        autoEquipCount++;
        log("[auto] CALLING equipItem(coll, \"" + job.kind + "\", " + row
            + ", 65535)  [" + autoEquipCount + "/" + AUTO_CAP + "]");
        // Same extra-null trick as resolve, in case of a trailing optional.
        new NativeFunction(addr, "pointer",
                           ["pointer", "pointer", "pointer", "int", "pointer"])(
            coll, job.str, row, 65535, ptr(0));
        log("[auto] equipItem returned — watch for the rebuild, then deploy "
            + "to confirm visually");
        capturedKind = job.kind;   // next pick avoids repeating this one
    } catch (e) { log("[auto] equip ERR " + e); }
}

// ---- sweep / arm -----------------------------------------------------------
let ticks = 0;
let dumped = false;

function sweep() {
    try {
        ticks++;
        if (!localHero || localHero.isNull()) {
            if (ticks % 10 === 0) log("[wait] no hero yet (frames=" + frames + ")");
            return;
        }
        if (!dumped) {
            dumped = true;
            const pool = readGliderKinds(localHero);
            log("PROBE ARMED (log-only) — " + Object.keys(pool).length
                + " gliders in the pool. Equip any glider manually now; the "
                + "self-test runs automatically after the capture.");
        }
    } catch (e) { log("sweep ERR " + e); }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (P.fn.postUpdate == null) { log("!! no postUpdate findex; refusing."); return; }
    if (!setupFieldApi())
        log("!! hl_obj_get_field unavailable — row map cannot build; "
            + "auto-equip will never arm.");
    Interceptor.attach(base.add(P.fn.postUpdate * 8).readPointer(), {
        onEnter: function () {
            frames++;
            refreshLocalHeroOnGameThread();
            verifyCandidates();
            selfTestOnGameThread();
            fireAutoEquipOnGameThread();
        }
    });
    hookEquipItem();
    hookResolve();
    hookGlideEdge();
    hookDisplay();
    log("waiting for the hero. Log-only until the GO file appears.");
    setInterval(sweep, 500);
}

// rpc instead of recv: a pending recv() is the prime suspect for the
// round-3b unload wedge, and rpc.exports leaves nothing pending.
rpc.exports = {
    go: function () {
        goArmed = true;
        log("GO received — auto-equip armed (cap " + AUTO_CAP + ", cooldown "
            + (COOLDOWN_MS / 1000) + "s)"
            + (mapProven ? ". Glide and land to trigger it."
                         : ", but the self-test has not passed yet."));
        return mapProven;
    }
};

setTimeout(main, 0);
