// nodefinder_probe.js — FIND BLESSED NODES, AND (OPTIONALLY) ASK TO GATHER ONE.
//
// What round 3 settled, and why this probe has the shape it does:
//
//   * ent.GameObject.addStatus NEVER RUNS ON THE CLIENT. Across 212
//     st.skill.Status.init calls during combat and a rift, addStatus fired
//     exactly zero times — Status.init is running inside network
//     DESERIALISATION. Statuses arrive from the server already made. So there
//     is no client-side "grant me this buff" to call; the code exists in the
//     binary because client and server ship together, and it executes on the
//     other side.
//   * Gatherable.hit / consume / doActionServer / setActiveAffix — also never
//     fired. All server-side.
//   * The ONE client-side entry point is
//         Gatherable.tryRequestInteraction(node: Gatherable, hero: Hero)
//     measured at arity 2. The client asks; the server decides and replicates
//     the answer back.
//
// So the honest tool is not "apply a buff" — it is "find the blessed node and
// ask to gather it", which is what a player does anyway.
//
// TWO MODES.
//
//   READ MODE (default, and what runs unless --allow-interact is passed):
//   a 2s census of every Gatherable in the layer, reading the REPLICATED
//   `affixId` — the node's rolled affix, which the client knows before you
//   touch it. Blessed nodes are reported with distance and compass bearing.
//   This alone answers "make world buffs easier to get", and it is the only
//   part that will ever ship in the meter.
//
//   INTERACT MODE (opt-in): on an explicit command, call
//   tryRequestInteraction on a chosen node, ON THE GAME THREAD. Nothing is
//   called unless asked, and the target is always logged before the call so a
//   surprising result is attributable.
//
// Note for reading the output: frida CANNOT intercept its own call (measured
// during the glider work), so when this probe calls tryRequestInteraction the
// round-3 hook would not have seen it. The proof that a call worked is the
// node's hitPoints moving and a status arriving — not a hook line.
//
// affixId is an index into the gatherable's cdb props.affixes array. Ore and
// Plant declare the SAME order, so one table serves both:
//     0 Physical(0%)  1 Fire(24%)  2 Ice(24%)  3 Nature(24%)
//     4 Wind(24%)     5 Chaos(4%)  6 Spark(0%)
// with an overall affixChance of 10% per node. Whether the live encoding is
// 0-based, 1-based or -1-for-none is NOT assumed — the census prints the raw
// number next to the label so a mismatch is visible rather than silently
// mislabelled.
//
// DATA + OFF + B are prepended by run_nodefinder.py.

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
// 0x8000000 (128MB), NOT gather_probe's 0x40000000: the wider cap makes
// Memory.scanSync grind through gigabyte heap ranges and the probe produces no
// output for minutes. Both probes that ran against this build today found the
// table in seconds with this cap.
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const b2=m.address.sub(seed.findex*8);if(b2.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=b2.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return b2;}}}return null;}
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

let base = null, localHero = null, frames = 0;
let tryInteract = null;

const heroFnCache = {};
let heroFnName = null;
function callHeroFn(nm) {
    try {
        let f = heroFnCache[nm];
        if (f === undefined) {
            f = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(), "pointer", []);
            heroFnCache[nm] = f;
        }
        const h = f();
        return (h && !h.isNull() && typeName(h) === "ent.Hero") ? h : null;
    } catch (e) { return null; }
}
// Re-asked, never latched: a zone change replaces the Hero and the old pointer
// becomes a freed husk that reads as "nothing is happening".
function refreshHero() {
    if (heroFnName) {
        const h = callHeroFn(heroFnName);
        if (h) { localHero = h; return; }
        localHero = null;
    }
    for (const nm in DATA.funcs) {
        const h = callHeroFn(nm);
        if (h) { heroFnName = nm; localHero = h; return; }
    }
}

function affixLabel(id) {
    const t = B.affixNames || [];
    const nm = (id >= 0 && id < t.length) ? t[id] : null;
    return (nm || "?") + "(" + id + ")";
}

function bearing(dx, dy) {
    const dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"];
    let a = Math.atan2(dy, dx) * 180 / Math.PI;
    if (a < 0) a += 360;
    return dirs[Math.round(a / 45) % 8];
}

function snapNode(e) {
    const G = B.G;
    function f64(off) { try { return e.add(off).readDouble(); } catch (x) { return null; } }
    function s32(off) { try { return e.add(off).readS32(); } catch (x) { return null; } }
    return {
        p: e,
        kind: hlStr(e.add(OFF.Element.kind).readPointer()),
        affix: G.affixId != null ? s32(G.affixId) : null,
        hp: G.hitPoints != null ? f64(G.hitPoints) : null,
        x: f64(OFF.Entity.posx), y: f64(OFF.Entity.posy), z: f64(OFF.Entity.posz),
        en: (function () { try { return e.add(OFF.Interactible.enabled).readU8(); } catch (x) { return null; } })(),
        rm: (function () { try { return e.add(8).readU8(); } catch (x) { return null; } })(),
    };
}

function walk(arr, out) {
    if (!arr || arr.isNull()) return;
    let n, data;
    try {
        n = arr.add(OFF.ArrayObj.length).readS32();
        data = arr.add(OFF.ArrayObj.array).readPointer();
    } catch (e) { return; }
    if (n < 0 || n > 20000 || !data || data.isNull()) return;
    for (let i = 0; i < n; i++) {
        let e;
        try { e = data.add(OFF.ArrayObj.data + i * 8).readPointer(); } catch (x) { break; }
        if (!e || e.isNull() || e.compare(ptr("0x10000")) <= 0) continue;
        const cls = typeName(e);
        if (!cls || !GATHER[cls]) continue;
        const k = e.toString();
        if (out[k]) continue;
        try { out[k] = snapNode(e); } catch (x) {}
    }
}

let lastCensus = {};
function census() {
    try {
        if (!localHero || localHero.isNull()) return;
        const layer = localHero.add(OFF.Hero.layer).readPointer();
        if (!layer || layer.isNull()) return;
        const G = OFF.GameLayer;
        const found = {};
        walk(layer.add(G.units).readPointer(), found);
        walk(layer.add(G.interactibles).readPointer(), found);
        walk(layer.add(G.entities).readPointer(), found);
        lastCensus = found;
    } catch (e) {}
}

let tick = 0;
const affixSeen = {};

function report() {
    tick++;
    if (!localHero || localHero.isNull()) {
        if (tick % 5 === 0) log("[waiting] no hero yet.");
        return;
    }
    const keys = Object.keys(lastCensus);
    if (!keys.length) { if (tick % 5 === 0) log("[census] no gatherables in layer."); return; }

    let hx = 0, hy = 0;
    try { hx = localHero.add(OFF.Entity.posx).readDouble(); hy = localHero.add(OFF.Entity.posy).readDouble(); } catch (e) {}

    // The distribution is the sanity check on affixId's encoding: ~90% of nodes
    // should sit at whatever "no affix" is, and the rest should spread over the
    // four common elements with Chaos rare.
    const dist = {};
    const blessed = [];
    for (const k of keys) {
        const s = lastCensus[k];
        const a = s.affix === null ? -999 : s.affix;
        dist[a] = (dist[a] || 0) + 1;
        if (!(a in affixSeen)) affixSeen[a] = 0;
        affixSeen[a]++;
        // "Blessed" is provisional until the distribution proves the encoding —
        // anything that is not the single dominant value is worth showing.
        blessed.push({ s: s, a: a, d: Math.hypot(s.x - hx, s.y - hy) });
    }
    const counts = Object.keys(dist).map(Number).sort(function (p, q) { return dist[q] - dist[p]; });
    const plain = counts.length ? counts[0] : -999;

    log("");
    log("---- tick " + tick + "  gatherables: " + keys.length
        + "   affixId distribution: "
        + counts.map(function (a) { return affixLabel(a) + " x" + dist[a]; }).join("  "));
    log("     (treating " + affixLabel(plain) + " as plain: it is the most common)");

    const special = blessed.filter(function (r) { return r.a !== plain; })
                           .sort(function (p, q) { return p.d - q.d; });
    if (!special.length) {
        log("     no blessed nodes in the layer right now.");
    } else {
        log("     BLESSED NODES (" + special.length + "):");
        for (const r of special.slice(0, 12)) {
            const s = r.s;
            log("       " + affixLabel(r.a).padEnd(14) + (s.kind || "?").padEnd(22)
                + " d=" + r.d.toFixed(0).padStart(5) + " " + bearing(s.x - hx, s.y - hy)
                + "  hp=" + (s.hp === null ? "?" : s.hp)
                + "  enabled=" + s.en + "  @" + s.p);
        }
    }
    // Nearest few regardless, so there is always something to aim the interact
    // test at even when nothing is blessed.
    const near = blessed.sort(function (p, q) { return p.d - q.d; }).slice(0, 3);
    for (const r of near)
        log("     NEAR " + (r.s.kind || "?").padEnd(22) + " d=" + r.d.toFixed(0).padStart(5)
            + "  affix=" + affixLabel(r.a) + "  hp=" + r.s.hp + "  @" + r.s.p);
}

// ---- the one call, on the game thread ---------------------------------------
let pendingInteract = null;   // {mode: "nearest"|"blessed", ptr: string|null}

function doInteractOnGameThread() {
    const req = pendingInteract;
    pendingInteract = null;
    if (!B.allowInteract) { log("!! interact requested but this run is READ-ONLY."); return; }
    if (!tryInteract) { log("!! tryRequestInteraction unavailable."); return; }
    if (!localHero || localHero.isNull()) { log("!! no hero."); return; }

    let hx = 0, hy = 0;
    try { hx = localHero.add(OFF.Entity.posx).readDouble(); hy = localHero.add(OFF.Entity.posy).readDouble(); } catch (e) {}
    const keys = Object.keys(lastCensus);
    let best = null;
    for (const k of keys) {
        const s = lastCensus[k];
        if (req.ptr && k !== req.ptr) continue;
        if (s.hp !== null && s.hp <= 0) continue;     // already mined out
        const d = Math.hypot(s.x - hx, s.y - hy);
        if (!best || d < best.d) best = { s: s, d: d, k: k };
    }
    if (!best) { log("!! no candidate node found."); return; }

    // Say exactly what is about to be called, BEFORE calling it, so any
    // surprising consequence is attributable to a named target.
    log(">>> CALLING tryRequestInteraction(node=" + best.s.p + " kind=" + best.s.kind
        + " affix=" + affixLabel(best.s.affix === null ? -999 : best.s.affix)
        + " d=" + best.d.toFixed(1) + " hp=" + best.s.hp + ", hero=" + localHero + ")");
    let r = null;
    try { r = tryInteract(best.s.p, localHero); }
    catch (e) { log("!!! call threw: " + e); return; }
    log("<<< returned " + r + "   (watch hp and your buff bar — frida cannot "
        + "intercept its own call, so the proof is the node changing, not a hook line)");
}

function onGameThreadTick() {
    frames++;
    if (!localHero || frames % 15 === 0) refreshHero();
    if (pendingInteract) { try { doInteractOnGameThread(); } catch (e) { log("interact ERR " + e); } }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (B.fn.postUpdate == null) { log("!! no postUpdate — refusing."); return; }
    if (B.allowInteract && B.fn.tryRequestInteraction != null) {
        try {
            tryInteract = new NativeFunction(
                base.add(B.fn.tryRequestInteraction * 8).readPointer(),
                "pointer", ["pointer", "pointer"]);
            log("INTERACT MODE ARMED — tryRequestInteraction is callable on command.");
        } catch (e) { log("!! could not bind tryRequestInteraction: " + e); }
    } else {
        log("READ-ONLY MODE — nothing will be called. Pass --allow-interact to enable.");
    }
    Interceptor.attach(base.add(B.fn.postUpdate * 8).readPointer(),
                       { onEnter: function () { onGameThreadTick(); } });
    setInterval(census, 2000);
    setInterval(report, 5000);
    log("PROBE ARMED — censusing gatherables. Run around ore/herb country; "
        + "every blessed node in the layer gets listed with distance and bearing.");
    recv("interact", function (m) { pendingInteract = { ptr: m.ptr || null }; });
}

setTimeout(main, 0);
