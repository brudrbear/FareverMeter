// glider_equip_probe.js — GLIDER RE-EQUIP PROOF (round 4).
//
// Rounds 1-2 measured: the deploy chain carries no glider id (nothing to
// arg-swap), the glider model is pre-spawned at equip time, and the UI's
// persistent equip is st.player.Collection.equipItem(...) — the same call
// as mounts. Brudr chose the real-re-equip design: the meter performs that
// same call with a random favorite, so the change replicates and other
// players see it.
//
// Round 3 chased the second argument, believing it was the CDB item row,
// and built an index walk to produce one. Round 3d's diagnostics killed
// that theory outright:
//
//     [self-test] captured=0x1739aafbb00 kind=10        <- HFUN: a CLOSURE
//     [self-test] indexRow=0x17276d220b0 kind=16 id="Glider_Bat_Burning"
//
// args[2] is a freshly allocated closure (a different pointer for the same
// kind on every click) — the UI's result callback, i.e. an OPTIONAL Haxe
// argument. args[3] (65535) is caller register leftover, not a parameter:
// round 3c saw args[4] mirror args[2] the same way. So the only argument
// that carries meaning is the kind String, which the collection already
// hands us. No row, no index, no heap scan, no hl_to_virtual.
//
// This round therefore calls, on the game thread, exactly:
//
//     equipItem(collection, kindString, null)
//
// null being what "callback not passed" means in generated Haxe. Gated
// behind the GO file as before: log-only until Brudr has read the capture.
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
function kindOf(p) {
    try { return p.readPointer().readU32(); } catch (e) { return -1; }
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
        const k = kindOf(p);
        if (k === 10) return "closure@" + p;
        if (k === 16) return "dynobj@" + p;
        if (k === 15) return "virtual@" + p;
        return p.toString() + " kind=" + k;
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

// {kind -> String ptr} from the collection's gliders array (GC-rooted, and
// HL's GC doesn't move objects — so these pointers stay valid as call args).
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

// ---- state -----------------------------------------------------------------
let goArmed = false;              // set by the host via rpc.exports.go()
let capturedKind = null;          // last glider kind seen through equipItem
let sawManualEquip = false;       // the capture gate for arming
let equipAddr = null;
let pendingEquip = null;          // {kind, str, hero} set at glide end
let lastEquipMs = 0;
let autoEquipCount = 0;
let lastGlideUp = false;
const AUTO_CAP = 5;
const COOLDOWN_MS = 5000;

// ---- hooks -----------------------------------------------------------------
function hookEquipItem() {
    const fi = P.hooks["Collection.equipItem"];
    if (fi == null) { log("!! Collection.equipItem missing"); return; }
    equipAddr = base.add(fi * 8).readPointer();
    Interceptor.attach(equipAddr, {
        onEnter: function (args) {
            let line = ">>> Collection.equipItem  this=" + describe(args[0]);
            for (let i = 1; i <= 3; i++) {
                try { line += "  a" + i + "=" + describe(args[i]); }
                catch (e) { break; }
            }
            log(line);
            try {
                const kind = hlStr(args[1]);
                if (kind && kind.lastIndexOf("Glider_", 0) === 0) {
                    capturedKind = kind;
                    if (!sawManualEquip) {
                        sawManualEquip = true;
                        log("[capture] glider equip seen (" + kind + "). "
                            + "a2 kind=" + kindOf(args[2])
                            + " (10 = closure / optional callback). "
                            + "READY — create the GO file to arm the "
                            + "auto-equip.");
                    }
                }
            } catch (e) {}
        }
    });
    log("hooked Collection.equipItem");
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
                if (!goArmed) { log("[skip] glide end, GO not armed"); return; }
                if (autoEquipCount >= AUTO_CAP) { log("[skip] cap reached"); return; }
                const now = Date.now();
                if (now - lastEquipMs < COOLDOWN_MS) { log("[skip] cooldown"); return; }
                const pool = readGliderKinds(args[0]);
                const kinds = Object.keys(pool).filter(function (k) {
                    return k !== capturedKind;
                });
                if (!kinds.length) { log("[skip] no alternative gliders"); return; }
                const pick = kinds[Math.floor(Math.random() * kinds.length)];
                pendingEquip = { kind: pick, str: pool[pick], hero: args[0] };
                lastEquipMs = now;
                log("[auto] glide end -> will equip " + pick + " next frame");
            } catch (e) { log("glide-edge ERR " + e); }
        }
    });
    log("hooked Hero.toggleGlide (glide-end trigger)");
}

// Rebuild observers — a successful equip retriggers the gear-display path,
// which is the visual confirmation that the call did something real.
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
function fireAutoEquipOnGameThread() {
    if (!pendingEquip) return;
    const job = pendingEquip;
    pendingEquip = null;
    try {
        if (!equipAddr) return;
        if (!heroIsMe(job.hero)) { log("[auto] hero not me — refusing"); return; }
        const coll = collectionOf(job.hero);
        if (!coll || coll.isNull()) { log("[auto] no collection"); return; }
        autoEquipCount++;
        log("[auto] CALLING equipItem(coll, \"" + job.kind + "\", null)  ["
            + autoEquipCount + "/" + AUTO_CAP + "]");
        new NativeFunction(equipAddr, "pointer",
                           ["pointer", "pointer", "pointer"])(
            coll, job.str, ptr(0));
        log("[auto] returned — expect a Slot_Glider rebuild below, then "
            + "deploy to confirm visually");
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
                + " gliders in the pool. Equip any glider manually once, so "
                + "the capture confirms the call shape.");
        }
    } catch (e) { log("sweep ERR " + e); }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (P.fn.postUpdate == null) { log("!! no postUpdate findex; refusing."); return; }
    Interceptor.attach(base.add(P.fn.postUpdate * 8).readPointer(), {
        onEnter: function () {
            frames++;
            refreshLocalHeroOnGameThread();
            fireAutoEquipOnGameThread();
        }
    });
    hookEquipItem();
    hookGlideEdge();
    hookDisplay();
    log("waiting for the hero. Log-only until the GO file appears.");
    setInterval(sweep, 500);
}

// rpc instead of recv: a pending recv() leaves work outstanding at unload.
rpc.exports = {
    go: function () {
        goArmed = true;
        log("GO received — auto-equip armed (cap " + AUTO_CAP + ", cooldown "
            + (COOLDOWN_MS / 1000) + "s)"
            + (sawManualEquip ? ". Glide and land to trigger it."
                              : ", but no manual equip has been captured yet."));
        return sawManualEquip;
    }
};

setTimeout(main, 0);
