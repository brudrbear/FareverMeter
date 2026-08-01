// mount_probe.js — MOUNT DISCOVERY SPIKE.
//
// Three questions, all client-side:
//   1. What does the unlocked-mount list look like at runtime?
//        localHero -> player -> accountProgress -> collection -> mounts
//        (hxbit.ArrayProxyData -> ArrayDyn -> ?) — element type unknown until
//        printed, so the first walk types every hop and every element.
//   2. Which function fires when the SELECTED mount changes?
//        ent.Hero.mountId@P.Hero.mountId is the replicated selection; the
//        set_mountId / setMount family and ui.Console.equipMount are hooked
//        with args logged, and the sweep watches the string for edges.
//   3. What marks a DISMOUNT? Mounted state is the vehicle system
//        (SkillKind carries Mount AND Exit_Vehicle), so ent.Unit.leaveVehicle
//        and onVehicleChange are hooked for the edge, mountUnit for the
//        mount-up side.
//
// Interceptors LOG ONLY — nothing is called, nothing is written. The sweep is
// plain pointer/string reads on a timer. The one HL-call site is the hero
// lookup inside the postUpdate hook (game thread), same as every probe.
//
// DATA + OFF + P are prepended by run_mount.py.

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
const heroOutcomes = {};

// GAME THREAD ONLY — same split as every probe; the timer never HL-calls.
function refreshLocalHeroOnGameThread() {
    if (localHero && !localHero.isNull()) return;
    heroTries++;
    for (const nm in DATA.funcs) {
        let outcome;
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (!h || h.isNull()) outcome = "null";
            else {
                const tn = typeName(h);
                outcome = tn || "<untyped>";
                if (tn === "ent.Hero") { localHero = h; }
            }
        } catch (e) { outcome = "threw: " + e.message; }
        const key = nm + " -> " + outcome;
        heroOutcomes[key] = (heroOutcomes[key] || 0) + 1;
        if (localHero && !localHero.isNull()) return;
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
            // name is a plain read; it can be null early in an entity's life.
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

// Log-with-cap per hook: the interesting calls are rare, but nothing here is
// allowed to flood if a guess about rarity is wrong.
const fireCount = {};
const counterOnly = { updateMount: true, updateMountAnim: true,
                      controlMount: true };

function attachLogged(name, findex) {
    if (findex == null) { log("!! no findex for " + name + " — skipped"); return; }
    let addr;
    try { addr = base.add(findex * 8).readPointer(); }
    catch (e) { log("!! " + name + " addr unreadable: " + e.message); return; }
    Interceptor.attach(addr, {
        onEnter: function (args) {
            const n = (fireCount[name] = (fireCount[name] || 0) + 1);
            if (counterOnly[name]) return;
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

// ---- the sweep: selection watch + collection walk (plain reads) ------------
let ticks = 0;
let armed = false;
let lastMountId = "<unread>";
let chainDumped = false;
let lastCounts = "";
let lastMountsLen = -1;

function heartbeat() {
    if (frames === 0) {
        log("[wait] postUpdate has fired 0 times after " + ticks + " ticks.");
        return;
    }
    const parts = [];
    for (const k in heroOutcomes) parts.push(k + " x" + parts.length);
    log("[wait] frames=" + frames + " heroTries=" + heroTries +
        " — no ent.Hero yet (menu/loading screen is expected).");
}

function collectionPtr() {
    const player = localHero.add(P.Hero.player).readPointer();
    if (!player || player.isNull()) return null;
    const acct = player.add(P.Player.accountProgress).readPointer();
    if (!acct || acct.isNull()) return null;
    return acct.add(P.AccountProgress.collection).readPointer();
}

// hxbit.ArrayProxyData -> .array (hl.types.ArrayDyn) -> .array (ArrayBase).
// The concrete ArrayBase class decides the element read; typed, not assumed.
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
    // Unknown container: print what it is and its first bytes, conclude nothing.
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

function dumpCollection() {
    log("--- collection dump ---");
    const player = localHero.add(P.Hero.player).readPointer();
    log("  player = " + describe(player) +
        (player && !player.isNull()
            ? "  name=" + (hlStr(player.add(P.Player.name).readPointer()) || "?")
              + "  isMe=" + player.add(P.Player.isMe).readU8()
            : ""));
    const coll = collectionPtr();
    log("  collection = " + describe(coll));
    if (!coll || coll.isNull()) { log("--- end (no collection) ---"); return false; }
    for (const label of ["mounts", "gliders", "pets"]) {
        const r = proxyElements(coll.add(P.Collection[label]).readPointer(), label);
        if (!r) continue;
        log("  " + label + ": " + r.els.length + " entries (" + r.cls + ")");
        for (let i = 0; i < r.els.length && i < 200; i++)
            log("      [" + i + "] " + elementDesc(r.els[i]));
    }
    log("--- end collection dump ---");
    return true;
}

function sweep() {
    try {
        ticks++;
        if (!localHero || localHero.isNull()) {
            if (ticks % 10 === 0) heartbeat();
            return;
        }
        if (!chainDumped) {
            chainDumped = true;
            log("localHero = " + localHero + " (after " + frames + " frames)");
            const ok = dumpCollection();
            log(ok ? "PROBE ARMED — mountId and the vehicle/setMount family are "
                     + "being watched. Do the in-game steps now."
                   : "PROBE PARTIALLY ARMED — hooks live, but the collection "
                     + "walk failed; mountId is still watched.");
            armed = true;
        }
        // Selection watch: a plain string read, logged on every edge.
        const cur = hlStr(localHero.add(P.Hero.mountId).readPointer());
        if (cur !== lastMountId) {
            log("=== mountId: " + JSON.stringify(lastMountId) + " -> "
                + JSON.stringify(cur));
            lastMountId = cur;
        }
        // Mount-list growth (a new unlock) shows as a length change.
        const coll = collectionPtr();
        if (coll && !coll.isNull()) {
            const dyn = coll.add(P.Collection.mounts).readPointer();
            if (dyn && !dyn.isNull()) {
                const ad = dyn.add(P.ArrayProxyData.array).readPointer();
                if (ad && !ad.isNull()) {
                    const inner = ad.add(P.ArrayDyn.array).readPointer();
                    if (inner && !inner.isNull()
                            && typeName(inner) === "hl.types.ArrayObj") {
                        const n = inner.add(OFF.ArrayObj.length).readS32();
                        if (n !== lastMountsLen) {
                            if (lastMountsLen >= 0)
                                log("=== mounts length: " + lastMountsLen
                                    + " -> " + n);
                            lastMountsLen = n;
                        }
                    }
                }
            }
        }
        // Periodic counters so the hot hooks still report.
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

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    if (P.fn.postUpdate == null) {
        log("!! no postUpdate findex — no game-thread anchor; refusing.");
        return;
    }
    Interceptor.attach(base.add(P.fn.postUpdate * 8).readPointer(), {
        onEnter: function () { frames++; refreshLocalHeroOnGameThread(); }
    });
    for (const name in P.hooks) attachLogged(name, P.hooks[name]);
    log("waiting for the hero. Nothing is armed until the ARMED line prints.");
    setInterval(sweep, 500);
}

setTimeout(main, 0);
