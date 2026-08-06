// shard_probe.js — SHARD IDENTITY SPIKE. Throwaway.
//
// Question: can a client name the shard (server instance) its character is on?
//
// The bytecode offers several candidates and the field names are NOT to be
// trusted — Main.getMapId() was the zone signal for months and turned out to
// return the MACHINE HOSTNAME. So this reads every candidate at once and
// prints them side by side, so a relog / region change / dungeon entry can be
// watched moving them:
//
//   st.GameLayer.serverName   String   hxbit-replicated (__net_mark_serverName
//                                      exists), so the SERVER pushes this to
//                                      the client — the prime candidate.
//   st.GameLayer.config       virtual  { activityID, difficulty, mapId }
//   world.World.level/name/branchName  what the zone signal already reads,
//                                      here to prove serverName is NOT just
//                                      another spelling of the zone.
//   st.Player.lobbyId         String   per-player; "dead" for travel (see the
//                                      portal work) but never read for identity
//   st.Player.uid / name               to tell whose player object this is
//
// Reads only. No HL calls, no hooks — the same off-thread pointer-walk pattern
// checkRift() uses, which is what makes it safe from a timer.
//
// DATA + OFF are prepended by run_shard.py.

// Offsets hardcoded HERE ON PURPOSE, like world_probe.js: until this confirms
// which field actually names a shard, adding it to emit_offsets.py would churn
// a tracked data file for a script that gets deleted. Whatever survives moves
// there so it self-heals across patches.
// Current build; regenerate if Farever patches under you.
const S = {
    GameLayer: { serverName: 184, config: 208 },
};

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=base.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return base;}}}return null;}
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

// A standalone hl vvirtual: field storage ADDRESSES live in the pointer array
// at v+24, so a value is a double deref. Same layout readSlot() walks in the
// shipping hook for inventory slots — see its comment block for the structs.
const HVIRTUAL = 15;
function virtualFields(p) {
    // Returns { name: <String value or "(non-string)"> } for every field of a
    // standalone virtual, by NAME out of its own field table — index order is
    // not something to guess at.
    const out = {};
    try {
        if (!p || p.isNull()) return out;
        const t = p.readPointer();
        if (t.readU32() !== HVIRTUAL) return out;
        const vt = t.add(8).readPointer();          // hl_type_virtual
        const fields = vt.readPointer();            // hl_obj_field[]
        const n = vt.add(8).readS32();
        if (n <= 0 || n > 64) return out;
        for (let i = 0; i < n; i++) {
            const nm = fields.add(i * 24).readPointer().readUtf16String();
            const slot = p.add(24 + i * 8).readPointer();
            if (!slot || slot.isNull()) { out[nm] = null; continue; }
            const v = slot.readPointer();
            if (!v || v.isNull()) { out[nm] = null; continue; }
            // Only strings are worth decoding here; anything else is reported
            // as its runtime class so a wrong guess is visible rather than
            // silently printed as garbage.
            const cls = typeName(v);
            out[nm] = cls === "String" ? hlStr(v) : ("<" + (cls || "?") + ">");
        }
    } catch (e) {}
    return out;
}

let localHero = null, base = null;
let tick = 0;
let last = "";

function refreshLocalHero() {
    for (const nm in DATA.funcs) {
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") { localHero = h; return; }
        } catch (e) {}
    }
}

function fmt(v) {
    if (v === null || v === undefined) return "(null)";
    if (v === "") return "(empty string)";
    return JSON.stringify(v);
}

function sweep() {
    try { sweepInner(); }
    catch (e) { log("!! sweep failed: " + e + "\n" + (e.stack || "(no stack)")); }
}

function sweepInner() {
    if (!localHero || localHero.isNull()) { refreshLocalHero(); return; }
    tick++;

    const layer = localHero.add(OFF.Hero.layer).readPointer();
    if (!layer || layer.isNull()) { log("no layer"); return; }

    const serverName = hlStr(layer.add(S.GameLayer.serverName).readPointer());
    const cfg = virtualFields(layer.add(S.GameLayer.config).readPointer());

    let level = null, wname = null, branch = null;
    const w = layer.add(OFF.GameLayer.world).readPointer();
    if (w && !w.isNull()) {
        level = hlStr(w.add(OFF.World.level).readPointer());
        wname = hlStr(w.add(OFF.World.name).readPointer());
        branch = hlStr(w.add(OFF.World.branchName).readPointer());
    }

    let pname = null, uid = null, lobby = null;
    const pl = localHero.add(OFF.Hero.player).readPointer();
    if (pl && !pl.isNull()) {
        pname = hlStr(pl.add(OFF.Player.name).readPointer());
        uid = hlStr(pl.add(OFF.Player.uid).readPointer());
        lobby = hlStr(pl.add(OFF.Player.lobbyId).readPointer());
    }

    // How many players the client holds state for — a shard's population is
    // the sanity check on "did I actually change shard": a different shard
    // means a different roster, not merely a different string.
    // Traversal per readShard() in the shipping hook: proxy -> ArrayDyn ->
    // ArrayObj. Skipping the ArrayDyn hop reads a pointer as a length and
    // prints a fresh nine-digit number every sweep, which is what the first
    // run of this probe did.
    let nplayers = -1;
    try {
        const proxy = layer.add(OFF.GameLayer.players).readPointer();
        const arrDyn = proxy.add(OFF.ArrayProxyData.array).readPointer();
        const arrObj = arrDyn.add(OFF.ArrayDyn.array).readPointer();
        const n = arrObj.add(OFF.ArrayObj.length).readS32();
        if (n >= 0 && n <= 4096) nplayers = n;
    } catch (e) {}

    const line = [
        "  GameLayer.serverName = " + fmt(serverName),
        "  GameLayer.config     = activityID=" + fmt(cfg.activityID) +
            "  mapId=" + fmt(cfg.mapId) +
            "  difficulty=" + fmt(cfg.difficulty),
        "  World.level          = " + fmt(level) +
            "   name=" + fmt(wname) + "  branch=" + fmt(branch),
        "  Player               = " + fmt(pname) + "  uid=" + fmt(uid) +
            "  lobbyId=" + fmt(lobby),
        "  layer.players        = " + nplayers + " players held",
    ].join("\n");

    // Only speak when something moves. A probe that reprints an unchanged
    // block every 2s buries the one line that matters.
    if (line !== last) {
        last = line;
        log("");
        log("---- change at sweep #" + tick + " ----");
        log(line);
    }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    refreshLocalHero();
    log("localHero = " + localHero + "  name=" +
        (localHero ? hlStr(localHero.add(OFF.Hero.name).readPointer()) : "?"));
    log("ARMED — reporting on change only.");
    setInterval(refreshLocalHero, 3000);
    setInterval(sweep, 2000);
    sweep();
}

main();
