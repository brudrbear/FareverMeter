// elixir_probe.js — WHAT DOES AN ELIXIR ACTUALLY CHANGE ON THE HERO?
//
// The question: you drink an Elixir of Abundance ("Your chance to find rare
// components while gathering is increased") — what, if anything, moves on the
// client's copy of the character?
//
// THE STATIC PICTURE (hlboot + data.cdb, 2026-08-08). Worth reading, because it
// predicts a NEGATIVE result and this probe exists to test that prediction
// rather than to publish it:
//
//   * Every other elixir in the game carries its effect in the ITEM row's
//     `affixes` — Elixir of Minor Strength is TAttribute_Flat / Strength / +3,
//     Elixir of Minor Armor is TAttribute_Flat / Armor / +200 — and they all
//     share the generic st.skill.ItemStatus for the icon.
//   * Elixir of Abundance is the ONLY elixir with a status row of its own
//     (ElixirOfAbundanceStatus), and that row is a shell: no affixes, no
//     mastery, no vars, no steps, no script, empty props apart from
//     types:[Buff]. The item row has no affixes and no aptitudes either.
//   * None of the 78 attributes in data.cdb is about gathering or loot, and
//     none of the 12 affix kinds is either. There is no dedicated Haxe class
//     for the status — it is a plain st.skill.Status.
//
// So the prediction is: the ONLY client-side change is a Status appearing in
// ent.Unit.statuses, and the rare-component roll lives on the server — which
// would match what FareverChest and FareverLoot already measured about loot
// never being rolled client-side.
//
// A prediction is not a measurement. This probe diffs the hero WHOLE.
//
// HOW IT AVOIDS LYING. A live hero has hundreds of fields and most of them move
// every frame (position, timers, dirty bits, health). Diffing naively gives a
// wall of noise in which a real one-field change is invisible. So the probe
// runs in two phases:
//
//   PHASE 1 — CALIBRATE. For the first N seconds after arming it samples every
//   watched field 4x/second and records every field that EVER changes. That is
//   the volatile set, and it is thrown away for the rest of the run. Stand
//   still-ish and do nothing during this; a field you never exercise here will
//   be reported later as a false positive, which is the known failure mode.
//
//   PHASE 2 — WATCH. Any change to a field OUTSIDE the volatile set is printed
//   with its before and after. This is the answer.
//
// WHAT IS WATCHED (every field of each, by name, resolved from hlboot):
//   ent.Hero (219)         — the character itself
//   ent.UnitAttributes     — the replicated stat block (Hero.attr)
//   ent.AffixManager       — Hero.affixes, plus its `cache` walked as a LIST of
//                            ent.AffixApplication (kind / baseVal / source /
//                            target), which is where any stat modification in
//                            this game actually lives. Reported in full on
//                            every change, calibrated or not.
//   st.Player, HeroStats, Progress, AccountProgress — the account/character
//                            records hanging off Hero.player
//   UnitAttributes.attributes — the full 78-attribute IntMap (the f64 fields
//                            above are only the ~33 replicated ones). Dumped on
//                            the game thread, since walking it allocates.
//   ent.Unit.statuses      — always printed on change, so the elixir's arrival
//                            is timestamped against everything else.
//
// The affix list and the attribute map are the two places a "+x% rare
// component chance" COULD hide. If both are unmoved and the only delta is a
// new Status, the effect is server-side and a client-side meter can only ever
// report that the buff is up — never what it is worth.
//
// Reads only. Nothing is called and nothing is written, except the two map
// natives (hikeys/higet) which allocate and therefore run on the game thread
// inside postUpdate — the same split every probe here uses.
//
// DATA + OFF + P are prepended by run_elixir.py.

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

// The hero lookup, re-asked rather than latched. ROUND 1 DIED HERE: the hero
// object is REPLACED on a zone change, an instance transfer or a relog, and the
// old pointer does not fault — it becomes a freed husk whose fields simply stop
// moving. A probe that latches it once goes quiet and looks like "nothing
// changed", which is the most expensive kind of wrong answer this project can
// produce. So the winning function is cached and re-called, and a pointer that
// comes back different tears the whole baseline down and recalibrates.
const heroFnCache = {};
let heroFnName = null;

function callHeroFn(nm) {
    try {
        let f = heroFnCache[nm];
        if (f === undefined) {
            f = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                   "pointer", []);
            heroFnCache[nm] = f;
        }
        const h = f();
        return (h && !h.isNull() && typeName(h) === "ent.Hero") ? h : null;
    } catch (e) { return null; }
}

function refreshLocalHeroOnGameThread() {
    if (heroFnName) {
        const h = callHeroFn(heroFnName);
        if (h) {
            if (!localHero || !h.equals(localHero)) onHeroChanged(h);
            return;
        }
        localHero = null;      // loading screen / between heroes — rescan below
    }
    heroTries++;
    for (const nm in DATA.funcs) {
        const h = callHeroFn(nm);
        if (h) { heroFnName = nm; onHeroChanged(h); return; }
    }
}

// ---- generic field reader ---------------------------------------------------
// P.fields maps a class name to [[fieldName, byteOffset, hlKind], ...], built
// host-side from hlboot so nothing here is a guessed offset. Kind codes are
// hashlink's own (hl.h): 1 u8, 2 u16, 3 i32, 4 i64, 5 f32, 6 f64, 7 bool,
// 18 enum, everything else pointer-shaped.
function num(v) {
    // Round hard. A float that only jitters in the 12th decimal is noise, and
    // printing it as a change would bury the one field that really moved.
    if (typeof v !== "number") return String(v);
    if (Number.isInteger(v)) return String(v);
    return v.toFixed(6);
}

function readField(p, off, kind) {
    try {
        const a = p.add(off);
        switch (kind) {
            case 1: return String(a.readU8());
            case 2: return String(a.readU16());
            case 3: return String(a.readS32());
            case 4: return a.readS64().toString();
            case 5: return num(a.readFloat());
            case 6: return num(a.readDouble());
            case 7: return a.readU8() ? "true" : "false";
        }
        const q = a.readPointer();
        if (!q || q.isNull()) return "null";
        // hl_enum_value is { hl_type *t; int index; } — the constructor index is
        // the whole content of a no-argument enum like ent.MoveMode, so it is
        // read rather than reported as an opaque pointer.
        if (kind === 18) { try { return "enum#" + q.add(8).readS32(); } catch (e) { return "enum?"; } }
        const tn = typeName(q);
        if (tn === "String") { const s = hlStr(q); return s === null ? "String?" : JSON.stringify(s); }
        if (tn === "hl.types.ArrayObj") {
            try { return "ArrayObj[" + q.add(OFF.ArrayObj.length).readS32() + "]"; }
            catch (e) { return "ArrayObj?"; }
        }
        return tn || "<ptr>";
    } catch (e) { return "<err>"; }
}

function snapObject(out, prefix, p, cls) {
    const fs = P.fields[cls];
    if (!fs) return;
    if (!p || p.isNull()) { out[prefix] = "<null object>"; return; }
    for (let i = 0; i < fs.length; i++)
        out[prefix + "." + fs[i][0]] = readField(p, fs[i][1], fs[i][2]);
}

// P.roots: [label, [offset chain from hero], className]. A chain that hits a
// null pointer records the whole object as absent rather than throwing.
function follow(chain) {
    let p = localHero;
    for (let i = 0; i < chain.length; i++) {
        if (!p || p.isNull()) return null;
        try { p = p.add(chain[i]).readPointer(); } catch (e) { return null; }
    }
    return (p && !p.isNull()) ? p : null;
}

function snapshot() {
    const out = {};
    for (const r of P.roots) snapObject(out, r[0], follow(r[1]), r[2]);
    return out;
}

// ---- the affix list — where a stat change would actually live ---------------
// AffixManager.cache is NOT a flat list of applications. Round 1 measured it as
// an ArrayObj OF ArrayObj — buckets, sparse, with empty slots held as null and
// whole buckets appearing and vanishing as statuses come and go. So the walk
// descends instead of assuming, and anything that is neither a bucket nor an
// application is reported by class rather than skipped.
function describeApp(el) {
    const A = P.AffixApplication;
    const kind = A.kind != null ? hlStr(el.add(A.kind).readPointer()) : "?";
    const val = A.baseVal != null ? num(el.add(A.baseVal).readDouble()) : "?";
    const src = A.source != null ? readField(el, A.source, 18) : "?";
    const tgt = A.target != null ? readField(el, A.target, 18) : "?";
    const tg2 = A.target2 != null ? readField(el, A.target2, 18) : "?";
    const inst = A.instigator != null
        ? (typeName(el.add(A.instigator).readPointer()) || "null") : "?";
    return kind + " val=" + val + " src=" + src + " tgt=" + tgt + "/" + tg2
           + " from=" + inst;
}

function collectApps(node, out, depth) {
    if (!node || node.isNull() || depth > 3) return;
    const cls = typeName(node);
    if (cls === "ent.AffixApplication") { out.push(describeApp(node)); return; }
    if (cls === "hl.types.ArrayObj") {
        let n, data;
        try {
            n = node.add(OFF.ArrayObj.length).readS32();
            data = node.add(OFF.ArrayObj.array).readPointer();
        } catch (e) { return; }
        if (n < 0 || n > 4096 || !data || data.isNull()) return;
        for (let i = 0; i < n; i++) {
            let el;
            try { el = data.add(OFF.ArrayObj.data + i * 8).readPointer(); } catch (e) { break; }
            if (el && !el.isNull()) collectApps(el, out, depth + 1);
        }
        return;
    }
    out.push("<" + (cls || "ptr") + ">");
}

function affixList() {
    const mgr = follow(P.chain.affixes);
    if (!mgr) return null;
    let arr;
    try { arr = mgr.add(P.AffixManager.cache).readPointer(); } catch (e) { return null; }
    if (!arr || arr.isNull()) return [];
    const out = [];
    collectApps(arr, out, 0);
    out.sort();
    return out;
}

// ---- the status list --------------------------------------------------------
function walkProxyArray(holder, off) {
    if (off == null) return null;
    let cur;
    try { cur = holder.add(off).readPointer(); } catch (e) { return null; }
    if (!cur || cur.isNull()) return null;
    for (let d = 0; d < 4; d++) {
        const cls = typeName(cur);
        if (cls === "hl.types.ArrayObj") {
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
        let next = null;
        try {
            if (cls === "hxbit.ArrayProxyData") next = cur.add(P.ArrayProxyData.array).readPointer();
            else if (cls === "hl.types.ArrayDyn") next = cur.add(P.ArrayDyn.array).readPointer();
        } catch (e) {}
        if (!next || next.isNull()) return null;
        cur = next;
    }
    return null;
}

function statusList() {
    const els = walkProxyArray(localHero, P.Unit.statuses);
    if (els === null) return null;
    const S = P.Status;
    const out = [];
    for (const s of els) {
        const kind = S.kind != null ? hlStr(s.add(S.kind).readPointer()) : "?";
        let item = null;
        if (S.originItem != null && P.Item && P.Item.kind != null) {
            try {
                const it = s.add(S.originItem).readPointer();
                if (it && !it.isNull()) item = hlStr(it.add(P.Item.kind).readPointer());
            } catch (e) {}
        }
        const st = S.stacks != null ? s.add(S.stacks).readS32() : "?";
        const dur = S.duration != null ? num(s.add(S.duration).readDouble()) : "?";
        out.push((kind || "?") + (item ? "<" + item + ">" : "") + " x" + st + " dur=" + dur);
    }
    out.sort();
    return out;
}

// ---- the attribute map — all 78, not just the replicated f64 block ----------
// GAME THREAD ONLY: hikeys allocates an hl_varray.
let hikeys = null, higet = null;
function setupMapNatives() {
    try {
        if (P.natives.hikeys != null)
            hikeys = new NativeFunction(base.add(P.natives.hikeys * 8).readPointer(),
                                        "pointer", ["pointer"]);
        if (P.natives.higet != null)
            higet = new NativeFunction(base.add(P.natives.higet * 8).readPointer(),
                                       "pointer", ["pointer", "int"]);
    } catch (e) {}
    return hikeys !== null && higet !== null;
}

let attrMapShapeLogged = false, attrValKindLogged = false;
function readAttributeMap() {
    const attr = follow(P.chain.attr);
    if (!attr) return null;
    let md;
    try { md = attr.add(P.UnitAttributes.attributes).readPointer(); } catch (e) { return null; }
    if (!md || md.isNull()) return null;
    // hxbit.MapData.map is a virtual; hl_vvirtual is { t@0, value@8, next@16 }.
    let inner;
    try {
        const v = md.add(OFF.MapData.map).readPointer();
        if (!v || v.isNull()) return null;
        inner = v.add(OFF.MapData.value).readPointer();
    } catch (e) { return null; }
    if (!inner || inner.isNull()) return null;
    const cls = typeName(inner);
    if (!attrMapShapeLogged) {
        attrMapShapeLogged = true;
        log("  attributes map under MapData: " + (cls || "<null>"));
    }
    // haxe.ds.IntMap.h sits at the same offset as StringMap.h (both @8).
    let h;
    try { h = inner.add(OFF.StringMap.h).readPointer(); } catch (e) { return null; }
    if (!h || h.isNull() || !hikeys || !higet) return null;
    let keys;
    try { keys = hikeys(h); } catch (e) { return null; }
    // hl_varray: { t@0, at@8, size@16 }, elements from @24.
    const n = keys.add(16).readS32();
    if (n < 0 || n > 512) return null;
    const out = {};
    for (let i = 0; i < n; i++) {
        const k = keys.add(24 + i * 4).readS32();
        let v = null;
        try { v = higet(h, k); } catch (e) {}
        // The values come back as boxed dynamics — hl_vdynamic is
        // { hl_type *t; union value; } so the payload is at @8. That the type
        // really is HF64 (kind 6) is checked once and logged, because reading a
        // pointer as a double produces a huge plausible-looking number rather
        // than an error.
        let val = "?";
        if (v && !v.isNull()) {
            if (!attrValKindLogged) {
                attrValKindLogged = true;
                let kk = -1;
                try { kk = v.readPointer().readU32(); } catch (e) {}
                log("  attribute map value type kind = " + kk
                    + (kk === 6 ? " (f64 — good)"
                                : " (NOT f64 — the numbers below are suspect)"));
            }
            try { val = num(v.add(8).readDouble()); } catch (e) {}
        }
        const nm = (P.attrNames && P.attrNames[k]) ? P.attrNames[k] : ("#" + k);
        out[nm] = val;
    }
    return out;
}

// ---- diff plumbing ---------------------------------------------------------
function diffMaps(a, b) {
    const changes = [];
    const seen = {};
    for (const k in a) {
        seen[k] = 1;
        if (!(k in b)) { changes.push([k, a[k], "<gone>"]); continue; }
        if (a[k] !== b[k]) changes.push([k, a[k], b[k]]);
    }
    for (const k in b) if (!(k in seen)) changes.push([k, "<absent>", b[k]]);
    return changes;
}

function diffLists(a, b) {
    // Multiset diff: the affix cache and the status array are both bags, and an
    // entry appearing twice is meaningful.
    const count = function (l) { const m = {}; for (const x of l) m[x] = (m[x] || 0) + 1; return m; };
    const ca = count(a || []), cb = count(b || []);
    const added = [], removed = [];
    for (const k in cb) { const d = (cb[k] || 0) - (ca[k] || 0); for (let i = 0; i < d; i++) added.push(k); }
    for (const k in ca) { const d = (ca[k] || 0) - (cb[k] || 0); for (let i = 0; i < d; i++) removed.push(k); }
    return { added: added, removed: removed };
}

// ---- the run ---------------------------------------------------------------
const t0 = Date.now();
let armed = false, calibrating = false, calibrated = false;
let calibEnd = 0;
const volatileKeys = {};
let baseline = null;
let lastAffix = null, lastStatus = null, lastAttrMap = null;
let wantAttrDump = false;
let ticks = 0, calibSamples = 0;

function el() { return ((Date.now() - t0) / 1000).toFixed(2) + "s"; }

function sweep() {
    try {
        ticks++;
        if (!localHero || localHero.isNull()) {
            if (ticks % 20 === 0)
                log("[wait] frames=" + frames + " heroTries=" + heroTries
                    + " — no ent.Hero yet (menu/loading is expected).");
            return;
        }
        const snap = snapshot();

        if (calibrating) {
            calibSamples++;
            if (baseline) {
                for (const c of diffMaps(baseline, snap)) volatileKeys[c[0]] = 1;
            }
            baseline = snap;
            if (Date.now() >= calibEnd) {
                calibrating = false;
                calibrated = true;
                let total = 0, vol = 0;
                for (const k in snap) { total++; if (volatileKeys[k]) vol++; }
                log("=== CALIBRATED at " + el() + " — " + calibSamples + " samples, "
                    + vol + " volatile fields ignored, " + (total - vol)
                    + " stable fields now watched.");
                log("*** DRINK THE ELIXIR NOW. Every change below is a real one. ***");
            }
            return;
        }

        // Statuses: always reported. This is the timestamp everything else is
        // read against.
        const st = statusList();
        if (st !== null) {
            const d = diffLists(lastStatus, st);
            if (lastStatus === null) {
                log("[" + el() + "] statuses at rest (" + st.length + "): " + st.join(" | "));
            } else if (d.added.length || d.removed.length) {
                log("[" + el() + "] STATUS " + d.added.map(function (x) { return "+" + x; })
                    .concat(d.removed.map(function (x) { return "-" + x; })).join("  "));
                wantAttrDump = true;
            }
            lastStatus = st;
        }

        // Affixes: always reported, calibrated or not — this is the one place a
        // stat modification in this game is allowed to live.
        const ax = affixList();
        if (ax !== null) {
            const d = diffLists(lastAffix, ax);
            if (lastAffix === null) {
                log("[" + el() + "] affix cache at rest (" + ax.length + "):");
                for (const a of ax) log("      " + a);
            } else if (d.added.length || d.removed.length) {
                log("[" + el() + "] AFFIX CACHE CHANGED:");
                for (const a of d.added) log("      + " + a);
                for (const a of d.removed) log("      - " + a);
            }
            lastAffix = ax;
        }

        if (!calibrated) return;

        const changes = diffMaps(baseline, snap).filter(function (c) { return !volatileKeys[c[0]]; });
        if (changes.length) {
            log("[" + el() + "] " + changes.length + " stable field(s) moved:");
            for (const c of changes) log("      " + c[0] + ": " + c[1] + "  ->  " + c[2]);
        }
        baseline = snap;
    } catch (e) { log("sweep ERR " + e + "\n" + e.stack); }
}

// GAME THREAD — the attribute map walk allocates, so it happens here.
function attrDumpOnGameThread() {
    const m = readAttributeMap();
    if (m === null) { log("  attributes map: unavailable"); return; }
    if (lastAttrMap === null) {
        const ks = Object.keys(m).sort();
        log("[" + el() + "] attributes map at rest (" + ks.length + "):");
        let line = [];
        for (const k of ks) {
            line.push(k + "=" + m[k]);
            if (line.length === 6) { log("      " + line.join("  ")); line = []; }
        }
        if (line.length) log("      " + line.join("  "));
    } else {
        const d = diffMaps(lastAttrMap, m);
        if (d.length) {
            log("[" + el() + "] ATTRIBUTE MAP CHANGED:");
            for (const c of d) log("      " + c[0] + ": " + c[1] + "  ->  " + c[2]);
        } else {
            log("[" + el() + "] attribute map: no change (" + Object.keys(m).length + " entries)");
        }
    }
    lastAttrMap = m;
}

// A new Hero means every baseline the probe holds describes an object that no
// longer exists. Rather than carry them forward and report the difference
// between two different characters as a change, everything is dropped and the
// calibration is run again from scratch.
function onHeroChanged(h) {
    const first = !armed;
    localHero = h;
    armed = true;
    baseline = null;
    lastAffix = null;
    lastStatus = null;
    lastAttrMap = null;
    for (const k in volatileKeys) delete volatileKeys[k];
    calibSamples = 0;
    calibrated = false;
    calibrating = false;
    log("hero = " + localHero + "  kind="
        + (P.Unit.kind != null ? hlStr(localHero.add(P.Unit.kind).readPointer()) : "?"));
    attrDumpOnGameThread();
    calibrating = true;
    calibEnd = Date.now() + P.calibMs;
    if (first) {
        log("PROBE ARMED — CALIBRATING for " + (P.calibMs / 1000)
            + "s. Stand still and do NOTHING in game until calibration ends; "
            + "anything you do now becomes noise that gets ignored later.");
    } else {
        log("!! HERO REPLACED at " + el() + " (zone change, instance transfer, "
            + "relog or death). Everything measured before this line is VOID. "
            + "RE-CALIBRATING for " + (P.calibMs / 1000) + "s — stand still, "
            + "and re-do the elixirs after the CALIBRATED line.");
    }
}

let heroCheckFrame = 0;

function onGameThreadTick() {
    frames++;
    // Every 15th frame: one HL call, and it is the difference between noticing
    // a hero swap and silently reading a corpse for the rest of the run.
    if (!localHero || ++heroCheckFrame % 15 === 0) refreshLocalHeroOnGameThread();
    if (wantAttrDump && armed) { wantAttrDump = false; attrDumpOnGameThread(); }
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
        log("!! hikeys/higet unavailable — the 78-attribute map will be skipped "
            + "(the ~33 replicated f64 stats are still watched).");
    let nf = 0;
    for (const c in P.fields) nf += P.fields[c].length;
    log("watching " + nf + " fields across " + P.roots.length + " objects.");
    Interceptor.attach(base.add(P.fn.postUpdate * 8).readPointer(), {
        onEnter: function () { onGameThreadTick(); }
    });
    log("waiting for the hero. Nothing is armed until the ARMED line prints.");
    setInterval(sweep, 250);
    recv("attrdump", function () { wantAttrDump = true; });
}

setTimeout(main, 0);
