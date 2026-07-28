// world_probe.js — MINIMAP SPIKE. Throwaway.
//
// Sweeps st.GameLayer's `units` and `interactibles` arrays from the local hero
// and reports what's actually in them, so the four things the bytecode can't
// answer get answered before any minimap code is written:
//
//   1. which of posx/posy is "north" (walk and watch the deltas)
//   2. whether rotationZ is radians or degrees, and where zero points
//   3. how far the server actually replicates entities (caps the zoom-out)
//   4. whether looted chests / used obelisks stay in `interactibles`
//      (drawing dead markers would be worse than drawing none)
//
// It also times the sweep, since the whole design rests on it being cheap
// enough to run several times a second.
//
// Reads only. No HL calls, no hooks on game functions — the same off-thread
// pattern the shipping hook uses for the rift flag, which is what makes it
// safe to run from a timer.
//
// DATA + OFF are prepended by run_world.py.

// ---------------------------------------------------------------------------
// Offsets are hardcoded HERE ON PURPOSE. This is a spike: until it confirms
// which fields are actually worth reading, adding them to emit_offsets.py
// would churn a tracked data file for a script that gets deleted. Phase 1
// moves the survivors there so they self-heal across patches like the rest.
// Current build; regenerate if Farever patches under you.
const W = {
    Entity: { posx: 176, posy: 184, posz: 192, rotationZ: 200, radius: 288 },
    State: { removed: 8 },
    GameLayer: { entities: 312, gameObjects: 320, interactibles: 328, units: 336,
                 mainActivity: 240 },
    // st.Activity also extends ent.Entity, so it carries the same posx/posy —
    // activities are positionable like everything else.
    Activity: { kind: 464, centroid: 488, boundRadiusSq: 496 },
    Interactible: { enabled: 592, isOffScreen: 608 },
    // ...and the varray's elements start at +24, past the hl_varray header
    // (t, at, size, pad). Reading from +0 hands you the header as if it were
    // your first two entities, which is a garbage pointer and an immediate
    // access violation. readParty() in meter_hook.js documents the same +24.
    ArrayObj: { length: 8, array: 16, data: 24 },
};

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=base.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return base;}}}return null;}
function hlStr(p){try{if(!p||p.isNull())return null;const b=p.add(OFF.String.bytes).readPointer();if(b.isNull())return null;return b.readUtf16String();}catch(e){return null;}}

// Runtime class name from the object header. The shipping hook uses this to
// name game windows; here it's the entity classifier. Cached by type pointer —
// a zone has hundreds of entities but only a couple of dozen distinct classes,
// so after warmup this is a dictionary hit rather than three memory reads.
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
    // hl.types.ArrayObj: length @8, native varray @16. Same shape the shipping
    // hook already walks for the party roster.
    const out = [];
    try {
        if (!arrPtr || arrPtr.isNull()) return out;
        const n = arrPtr.add(W.ArrayObj.length).readS32();
        if (n <= 0 || n > 20000) return out;      // sanity: never trust a length
        const data = arrPtr.add(W.ArrayObj.array).readPointer();
        if (data.isNull()) return out;
        for (let i = 0; i < n; i++) {
            const e = data.add(W.ArrayObj.data + i * 8).readPointer();
            // Cheap sanity filter: a real heap object is far above the first
            // page. Anything low is a header field or a freed slot, and
            // dereferencing it faults.
            if (e && !e.isNull() && e.compare(ptr("0x10000")) > 0) out.push(e);
        }
    } catch (e) {}
    return out;
}

function pos(e) {
    return {
        x: e.add(W.Entity.posx).readDouble(),
        y: e.add(W.Entity.posy).readDouble(),
        z: e.add(W.Entity.posz).readDouble(),
    };
}
function removed(e) { try { return e.add(W.State.removed).readU8() !== 0; } catch (x) { return false; } }
function dist2(a, b) { const dx = a.x - b.x, dy = a.y - b.y; return dx * dx + dy * dy; }

let localHero = null, base = null;
let prev = null;                 // last hero position, for the walk deltas
let rotMin = 1e9, rotMax = -1e9; // to decide radians vs degrees
let tick = 0;

function refreshLocalHero() {
    // The one HL call in here. Same accessor list the shipping hook uses.
    for (const nm in DATA.funcs) {
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") { localHero = h; return; }
        } catch (e) {}
    }
}

function sweep() {
    try { sweepInner(); }
    catch (e) { log("!! sweep failed: " + e + "\n" + (e.stack || "(no stack)")); }
}

function sweepInner() {
    if (!localHero || localHero.isNull()) { refreshLocalHero(); return; }
    tick++;
    const t0 = Date.now();

    let layer;
    try { layer = localHero.add(OFF.Hero.layer).readPointer(); } catch (e) { return; }
    if (!layer || layer.isNull()) { log("no layer"); return; }

    const me = pos(localHero);
    const rot = localHero.add(W.Entity.rotationZ).readDouble();
    rotMin = Math.min(rotMin, rot); rotMax = Math.max(rotMax, rot);

    const units = readArray(layer.add(W.GameLayer.units).readPointer());
    const inter = readArray(layer.add(W.GameLayer.interactibles).readPointer());

    // Tally by class, and track how far out things are actually replicated.
    const tally = {}, far = {};
    let maxD = 0, alive = 0;
    function note(e, bucket) {
        const nm = typeName(e) || "?";
        const key = bucket + nm;
        if (removed(e)) { tally[key + " [removed]"] = (tally[key + " [removed]"] || 0) + 1; return; }
        alive++;
        tally[key] = (tally[key] || 0) + 1;
        const d = Math.sqrt(dist2(me, pos(e)));
        if (d > maxD) maxD = d;
        if (!far[nm] || d > far[nm]) far[nm] = d;
    }
    units.forEach(e => note(e, "unit  "));
    inter.forEach(e => note(e, "inter "));

    const ms = Date.now() - t0;

    // Movement delta: whichever of x/y moves when you walk in a straight line
    // tells us the ground plane's orientation.
    let delta = "";
    if (prev) delta = "  d(x,y,z)=(" + (me.x - prev.x).toFixed(2) + "," +
                      (me.y - prev.y).toFixed(2) + "," + (me.z - prev.z).toFixed(2) + ")";
    prev = me;

    log("");
    log("---- sweep #" + tick + "  " + ms + "ms  units=" + units.length +
        " interactibles=" + inter.length + " alive=" + alive + " ----");
    log("  me  x=" + me.x.toFixed(1) + " y=" + me.y.toFixed(1) + " z=" + me.z.toFixed(1) +
        "  rotationZ=" + rot.toFixed(3) + "  (seen " + rotMin.toFixed(2) + " .. " +
        rotMax.toFixed(2) + ")" + delta);
    log("  farthest replicated entity: " + maxD.toFixed(1) + " units");

    const keys = Object.keys(tally).sort();
    for (const k of keys) log("    " + k + " x" + tally[k] +
                              (far[k.slice(6)] ? "   (max " + far[k.slice(6)].toFixed(0) + ")" : ""));

    // Activities: the layer names one "main" activity, but the whole set has to
    // come from somewhere enumerable if the minimap is to mark more than one.
    // `entities` is the widest net, so this reports what st.Activity* it holds.
    try {
        const ma = layer.add(W.GameLayer.mainActivity).readPointer();
        if (ma && !ma.isNull() && ma.compare(ptr("0x10000")) > 0) {
            const p = pos(ma);
            log("  mainActivity: " + typeName(ma) + "  kind=" +
                hlStr(ma.add(W.Activity.kind).readPointer()) +
                "  at (" + p.x.toFixed(0) + "," + p.y.toFixed(0) + ")  d=" +
                Math.sqrt(dist2(me, p)).toFixed(0));
        } else {
            log("  mainActivity: (none)");
        }
    } catch (e) { log("  mainActivity: read failed " + e); }

    const ents = readArray(layer.add(W.GameLayer.entities).readPointer());
    const acts = {};
    for (const e of ents) {
        const nm = typeName(e) || "?";
        if (nm.indexOf("Activity") >= 0 || nm.indexOf("st.activity.") === 0)
            acts[nm] = (acts[nm] || 0) + 1;
    }
    const actKeys = Object.keys(acts).sort();
    log("  entities=" + ents.length + "  activity-ish: " +
        (actKeys.length ? actKeys.map(k => k + " x" + acts[k]).join(", ") : "(none)"));

    // Interactible state: the question is whether a looted chest disappears
    // from the array or just flips a flag. Print each one so you can loot a
    // chest and watch what changes.
    if (inter.length) {
        const rows = inter.map(e => {
            const nm = (typeName(e) || "?").replace("ent.interactible.", "");
            let en = "?";
            try { en = e.add(W.Interactible.enabled).readU8() ? "on " : "OFF"; } catch (x) {}
            return { nm: nm, en: en, d: Math.sqrt(dist2(me, pos(e))), rm: removed(e) };
        }).sort((a, b) => a.d - b.d).slice(0, 12);
        log("  nearest interactibles (enabled / distance):");
        for (const r of rows)
            log("    " + r.en + "  " + r.d.toFixed(0).padStart(5) + "  " + r.nm +
                (r.rm ? "  [removed]" : ""));
    }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    refreshLocalHero();
    log("localHero = " + localHero + "  name=" +
        (localHero ? hlStr(localHero.add(OFF.Hero.name).readPointer()) : "?"));
    setInterval(refreshLocalHero, 3000);
    setInterval(sweep, 2000);
}

main();
