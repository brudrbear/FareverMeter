// mount_swap_probe.js — THE ARG-SWAP PROOF.
//
// mount_probe.js established (measured, 2026-08-01):
//   * collection.mounts is an ArrayObj of HL String kinds (31 for Brodr);
//   * ent.Hero.setMount(id) is the LOCAL summon entry — it fired only for
//     the local hero, args[1] = the kind String, null on dismount;
//   * everything downstream (setMount__impl, setupMount, set_mountId, the
//     replicated selection) inherits setMount's argument.
//
// This probe tests the whole feature in one move: onEnter of setMount on the
// LOCAL hero with a non-null id, replace args[1] with a random String picked
// from the collection's own mounts array. No allocation — the replacement
// pointers are the collection's, GC-rooted by it, and HL's GC does not move
// objects. If the game summons the swapped mount and the log shows the
// downstream family carrying the swapped id, the random-favorite feature is
// exactly this hook plus a favorites filter.
//
// DATA + OFF + P are prepended by run_swap.py.

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

// The swap pool: {kind -> String ptr}, re-walked each sweep so a pointer is
// never used long after the collection last held it.
let pool = {};

function rebuildPool() {
    const out = {};
    try {
        const player = localHero.add(P.Hero.player).readPointer();
        const acct = player.add(P.Player.accountProgress).readPointer();
        const coll = acct.add(P.AccountProgress.collection).readPointer();
        const proxy = coll.add(P.Collection.mounts).readPointer();
        const dyn = proxy.add(P.ArrayProxyData.array).readPointer();
        const inner = dyn.add(P.ArrayDyn.array).readPointer();
        if (typeName(inner) !== "hl.types.ArrayObj") return out;
        const n = inner.add(OFF.ArrayObj.length).readS32();
        if (n < 0 || n > 4096) return out;
        const data = inner.add(OFF.ArrayObj.array).readPointer();
        for (let i = 0; i < n; i++) {
            const s = data.add(OFF.ArrayObj.data + i * 8).readPointer();
            const kind = hlStr(s);
            if (kind) out[kind] = s;
        }
    } catch (e) {}
    return out;
}

let armed = false;
let swaps = 0;

function sweep() {
    try {
        if (!localHero || localHero.isNull()) return;
        pool = rebuildPool();
        if (!armed && Object.keys(pool).length > 0) {
            armed = true;
            log("PROBE ARMED — " + Object.keys(pool).length + " mounts in the "
                + "swap pool. SUMMON YOUR MOUNT NOW; every summon should come "
                + "out as a different random mount.");
        }
    } catch (e) { log("sweep ERR " + e); }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    Interceptor.attach(base.add(P.fn.postUpdate * 8).readPointer(), {
        onEnter: function () { frames++; refreshLocalHeroOnGameThread(); }
    });

    // Round 2 lesson: run 1 gated both hooks on pointer-equality with a
    // ONCE-latched localHero and logged nothing at all — a re-replicated
    // hero (a zone change is enough) leaves the cached pointer stale and
    // every comparison false, which is invisible when the filtered path is
    // also the only logging path. So: the local check is now a plain-read
    // walk on the hooked object itself (this -> player -> isMe), and every
    // fire logs unconditionally first.
    function isMeHero(hero) {
        try {
            const player = hero.add(P.Hero.player).readPointer();
            if (!player || player.isNull()) return false;
            return player.add(P.Player.isMe).readU8() === 1;
        } catch (e) { return false; }
    }

    // The swap pool comes from the hooked hero's own collection, walked
    // inside the hook — no cached pointers anywhere. setMount is rare
    // (once per summon), so the 31-entry walk costs nothing that matters.
    function poolOf(hero) {
        const out = {};
        try {
            const player = hero.add(P.Hero.player).readPointer();
            const acct = player.add(P.Player.accountProgress).readPointer();
            const coll = acct.add(P.AccountProgress.collection).readPointer();
            const proxy = coll.add(P.Collection.mounts).readPointer();
            const dyn = proxy.add(P.ArrayProxyData.array).readPointer();
            const inner = dyn.add(P.ArrayDyn.array).readPointer();
            if (typeName(inner) !== "hl.types.ArrayObj") return out;
            const n = inner.add(OFF.ArrayObj.length).readS32();
            if (n < 0 || n > 4096) return out;
            const data = inner.add(OFF.ArrayObj.array).readPointer();
            for (let i = 0; i < n; i++) {
                const s = data.add(OFF.ArrayObj.data + i * 8).readPointer();
                const kind = hlStr(s);
                if (kind) out[kind] = s;
            }
        } catch (e) {}
        return out;
    }

    Interceptor.attach(base.add(P.fn.setMount * 8).readPointer(), {
        onEnter: function (args) {
            try {
                const me = isMeHero(args[0]);
                const orig = hlStr(args[1]);
                log("setMount fired  isMe=" + me + "  id="
                    + JSON.stringify(orig));
                if (!me || orig === null) return;
                const mounts = poolOf(args[0]);
                const kinds = Object.keys(mounts).filter(k => k !== orig);
                if (!kinds.length) { log("  (empty pool — untouched)"); return; }
                const pick = kinds[Math.floor(Math.random() * kinds.length)];
                args[1] = mounts[pick];
                swaps++;
                log("SWAP #" + swaps + ": \"" + orig + "\" -> \"" + pick + "\"");
            } catch (e) { log("swap ERR " + e); }
        }
    });

    // Ground truth that the swap carried: the replicated selection setter.
    Interceptor.attach(base.add(P.fn.set_mountId * 8).readPointer(), {
        onEnter: function (args) {
            try {
                if (!isMeHero(args[0])) return;
                log("    set_mountId <- " + JSON.stringify(hlStr(args[1])));
            } catch (e) {}
        }
    });

    log("hooks live; waiting for hero + collection. Nothing is armed until "
        + "the ARMED line prints.");
    setInterval(sweep, 500);
}

setTimeout(main, 0);
