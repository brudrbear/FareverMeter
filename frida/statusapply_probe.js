// statusapply_probe.js — WHAT IS THE SIGNATURE OF addStatus?
//
// Goal: learn how the game itself applies a status, precisely enough to call it
// the same way. This round CALLS NOTHING. It only watches.
//
// Why a probe at all, when the name is right there in hlboot: because
// hlbc_parser deliberately stops before function bodies, so a proto gives a
// findex and NOT a signature. The glider work already paid for that lesson —
// an argument that looked like a cdb row was actually a closure, and a round
// was lost building on the guess. So the arity and the argument TYPES get
// measured off the game's own calls before anything is constructed.
//
// The static picture:
//   ent.GameObject.addStatus     findex 4561   <- the base implementation;
//                                ent.Unit and ent.Hero inherit it
//   script.UnitScript.addStatus  findex 14215  <- the script-facing wrapper,
//                                which is what `u.addStatus(Skill.X)` in a cdb
//                                script column actually calls
//   st.skill.Status.init / start               <- construction, for the shape
//                                of the object that comes out the far end
//
// The cdb script on a blessed ore node is literally:
//     function onOwnerGathered(gather, gatherers) {
//         for(u in gatherers) { u.addStatus(Skill.OreAffix_Fire_Status); }
//     }
// so ONE string-ish argument is the likely shape — but `Skill.X` is a cdb enum
// abstract, and whether that reaches native code as an interned String, an
// index, or a Data row is exactly what cannot be read off the type table.
//
// Every hook dumps its first six argument slots. Beyond the real arity those
// are garbage registers, which is fine and is why the dump prints what each
// slot IS rather than asserting a meaning: a slot that is a String on every
// single sample is an argument; one that is a different class each time is not.
//
// Samples are deduplicated by shape, so ten minutes of combat produces a short
// table instead of a flood.
//
// DATA + OFF + P are prepended by run_statusapply.py.

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const tb=m.address.sub(seed.findex*8);if(tb.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=tb.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return tb;}}}return null;}
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

// hl_type.kind for whatever a pointer points at — a dynamic wrapping a String
// and a raw String look identical until you read the kind.
function typeKind(p) {
    try {
        if (!p || p.isNull() || p.compare(ptr("0x10000")) <= 0) return -1;
        return p.readPointer().readU32();
    } catch (e) { return -1; }
}

// What IS this argument slot? Deliberately descriptive rather than
// interpretive: the point is to see the same answer on every sample before
// deciding a slot means anything.
function describeArg(p) {
    if (p === undefined || p === null) return "-";
    let asInt;
    try { asInt = p.toInt32(); } catch (e) { asInt = null; }
    if (p.isNull()) return "null";
    // Small values are not addresses; they are ints, bools or enum indices
    // passed in the same register slot.
    if (p.compare(ptr("0x10000")) <= 0 && p.compare(ptr("0x0")) >= 0)
        return "int:" + asInt;
    const cls = typeName(p);
    if (cls === "String") {
        const s = hlStr(p);
        return 'String:"' + (s === null ? "?" : s) + '"';
    }
    if (cls) return cls;
    const k = typeKind(p);
    if (k >= 0) {
        // A boxed dynamic: kind then payload. HBYTES (8) is a raw utf16 buffer,
        // which is what an interned cdb id looks like before it is wrapped.
        if (k === 8) {
            try { return 'bytes:"' + p.readUtf16String() + '"'; } catch (e) {}
        }
        let pay = "";
        try { pay = " payload=" + p.add(8).readPointer(); } catch (e) {}
        return "dyn(kind=" + k + ")" + pay;
    }
    let raw = "";
    try { raw = " " + p.readByteArray(16); } catch (e) {}
    return "<ptr " + p + ">" + raw;
}

let base = null;
const seen = {};        // hook name -> shape string -> count
let total = 0;

function dumpCall(name, args, n) {
    const parts = [];
    for (let i = 0; i < n; i++) parts.push(describeArg(args[i]));
    // The shape is what dedups: only the CLASSES matter, not the payloads, or
    // every distinct buff would print as a new sample.
    const shape = parts.map(function (s) {
        return s.replace(/:"[^"]*"/, ':"…"').replace(/payload=0x[0-9a-f]+/, "payload=…")
                .replace(/<ptr 0x[0-9a-f]+>.*/, "<ptr>");
    }).join(" | ");
    if (!seen[name]) seen[name] = {};
    const first = !(shape in seen[name]);
    seen[name][shape] = (seen[name][shape] || 0) + 1;
    total++;
    // Print the first few of each distinct shape with FULL payloads (that is
    // where the actual status id lives), then go quiet and just count.
    if (seen[name][shape] <= 3) {
        log((first ? "NEW SHAPE  " : "           ") + name + "  #"
            + seen[name][shape]);
        for (let i = 0; i < n; i++) log("      arg[" + i + "] = " + parts[i]);
    }
}

function hook(name, findex, nargs) {
    if (findex == null) { log("!! no findex for " + name + " — skipped."); return; }
    let addr;
    try { addr = base.add(findex * 8).readPointer(); }
    catch (e) { log("!! could not read slot for " + name); return; }
    try {
        Interceptor.attach(addr, {
            onEnter: function (args) {
                try { dumpCall(name, args, nargs); } catch (e) {}
            }
        });
        log("hooked " + name + " (findex " + findex + ") at " + addr);
    } catch (e) { log("!! attach failed for " + name + ": " + e); }
}

function summary() {
    log("");
    log("=== SHAPES SEEN (" + total + " calls) ===");
    for (const name of Object.keys(seen).sort()) {
        log("  " + name + ":");
        const shapes = seen[name];
        for (const s of Object.keys(shapes).sort(function (a, b) { return shapes[b] - shapes[a]; }))
            log("      x" + String(shapes[s]).padStart(4) + "  " + s);
    }
    log("");
    log("Read the table like this: a slot that is the SAME class in every row "
        + "is a real argument. The first slot that varies wildly is past the "
        + "real arity and is a leftover register.");
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    // PATH A — grant the status directly.
    hook("GameObject.addStatus", P.fn.addStatus, 6);
    hook("UnitScript.addStatus", P.fn.scriptAddStatus, 6);
    if (P.fn.statusInit != null) hook("Status.init", P.fn.statusInit, 6);
    // PATH B — ask the game to gather the node, and let the server grant the
    // buff the way it always does. Likelier to survive than a direct write,
    // because it is the server's own authoritative route; the risk is the
    // other way round (a distance or state check refusing it), which is what
    // getNoActionReason and canConsume exist to say.
    hook("Gatherable.hit", P.fn.gHit, 6);
    hook("Gatherable.tryRequestInteraction", P.fn.gTryRequest, 6);
    hook("Gatherable.doActionServer", P.fn.gDoAction, 6);
    hook("Gatherable.consume", P.fn.gConsume, 6);
    hook("Gatherable.setActiveAffix", P.fn.gSetAffix, 6);
    log("");
    log("PROBE ARMED — nothing is called, only watched.");
    log("  1) Fight something (dash / any class buff / food) — that fills in "
        + "the addStatus shapes.");
    log("  2) Then find a BLESSED node (one with the elemental FX) and gather "
        + "it — that fills in the Gatherable shapes AND shows addStatus being "
        + "called with the buff we actually want.");
    recv("summary", function () { summary(); });
}

setTimeout(main, 0);
