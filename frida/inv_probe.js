// inv_probe.js — INVENTORY WATCHER SPIKE. Throwaway.
//
// Rounds 1-3 of item_probe.js established that item gains do NOT pass through
// client-side gain/loot functions (they're server code; the client receives
// replicated hxbit state), and that ui.notify.NotifyManager is the keybind
// hint system, not a pickup toast. So this round watches the replicated state
// itself, which is the meter's native architecture anyway:
//
//   localHero -> loadout -> inventory -> content[]   (st.Item instances)
//   localHero -> weaponInHand                        (the equipped weapon)
//
// Pure pointer/string reads on a timer — no Interceptor, no HL calls — so
// there is no game-thread constraint and nothing to destabilise. Items are
// identified by their hxbit __uid, and every add/remove logs class + kind +
// (for weapons) the live rarity string, which is the whole point: learning
// how "legendary" is actually spelled in the data.
//
// DATA + OFF + P are prepended by run_inv.py.

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

function readArray(arrPtr) {
    const out = [];
    try {
        if (!arrPtr || arrPtr.isNull()) return out;
        const n = arrPtr.add(OFF.ArrayObj.length).readS32();
        if (n < 0 || n > 4096) return out;
        const data = arrPtr.add(OFF.ArrayObj.array).readPointer();
        if (data.isNull()) return out;
        for (let i = 0; i < n; i++) {
            const e = data.add(OFF.ArrayObj.data + i * 8).readPointer();
            if (e && !e.isNull() && e.compare(ptr("0x10000")) > 0) out.push(e);
            else out.push(null);
        }
    } catch (e) {}
    return out;
}

let base = null;
let localHero = null;
let heroAnnounced = false;

// Diagnostics, because the first real run of this probe produced NO output at
// all and there was no way to tell which half had failed: a hook that resolved
// but never fired, or a hook that fired with getHero calls that never returned
// a Hero. (The actual cause was neither — analysis_out was pre-patch, so the
// getHero findices pointed at unrelated functions. run_inv.py now refuses to
// start in that state.) These counters make each of those states name itself.
let frames = 0;                 // postUpdate invocations
let heroTries = 0;              // refreshLocalHeroOnGameThread passes
const heroOutcomes = {};        // "candidate -> what came back" -> count

// GAME THREAD ONLY. The getHero functions are HL calls, and an HL call from
// frida's timer thread dies with "Can't lock GC in unregistered thread" —
// measured the hard way: the first version of this probe called this from
// setInterval and crashed the game. It now runs only inside the postUpdate
// hook below, which is the same split the shipping meter uses.
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

function itemDesc(it) {
    const cls = typeName(it) || "?";
    let kind = null, rarity = null, level = null;
    try { kind = hlStr(it.add(P.Item.kind).readPointer()); } catch (e) {}
    if (cls === "st.item.Weapon") {
        try { rarity = hlStr(it.add(P.Weapon.rarity).readPointer()); } catch (e) {}
        try { level = it.add(P.Weapon.level).readS32(); } catch (e) {}
    }
    return cls + "  kind=" + (kind || "?") +
           (rarity !== null ? "  rarity=" + rarity : "") +
           (level !== null ? "  lvl=" + level : "");
}

function slotDesc(slot) {
    return itemDesc(slot.item)
        + (slot.count !== null && slot.count !== 1 ? "  x" + slot.count : "");
}

function itemUid(it) {
    try { return it.add(P.Item.uid).readS64().toString(); } catch (e) { return null; }
}

// MEASURED (round 5): st.Inventory.content is an hl.types.ArrayObj whose
// entries are typeKind 15 = HVIRTUAL, not st.Item pointers. That is why the
// first walk reported classes of "?" and read a shader source path out of what
// it thought was `kind` — it was decoding a vvirtual header as an item.
//
// MEASURED (round 6): each entry is a standalone virtual — value and next are
// both NULL — carrying two inline fields, `{ count: Int, item: st.Item }`. So
// content is a list of inventory SLOTS, not of items. Cross-validated: the
// equipment slot [0]'s `.item` is the identical pointer to ent.Hero's
// weaponInHand.
//
// The field is looked up by NAME rather than by a hardcoded index, because
// nothing guarantees the compiler's field order, and index 0 here is `count`.
// The resolved index is cached per virtual type — this runs over ~90 slots
// twice a second.
const HVIRTUAL = 15;

// Reading a standalone virtual's fields, measured round 6.
//
// These entries have value==NULL and next==NULL, so they are not wrappers
// around an object — they carry their own inline field storage. The layouts
// below are libhl's, and the one already proven by typeName() (hl_type_obj's
// name at +16) is the evidence that this reading of hl_type is right:
//
//   hl_type          { kind @0; union ptr @8; vobj_proto @16; mark_bits @24 }
//   hl_type_virtual  { fields @0; nfields @8; dataSize @12; indexes @16 }
//   hl_obj_field     { name @0; type @8; hashed_name @16 }   (24 bytes each)
//
// and for a standalone virtual, hl_vfields(v) — the pointer array at v+24 —
// holds, per field, the ADDRESS of that field's storage inside the same
// allocation. So field i's value is a double dereference:
//   storage = *(void**)(v + 24 + i*8);  value = *(void**)storage
function virtualFields(v) {
    const out = [];
    try {
        const t = v.readPointer();
        if (t.readU32() !== HVIRTUAL) return out;
        const vt = t.add(8).readPointer();
        const fields = vt.readPointer();
        const n = vt.add(8).readS32();
        if (n < 0 || n > 64) return out;
        for (let i = 0; i < n; i++) {
            const f = fields.add(i * 24);
            let name = null;
            try { name = f.readPointer().readUtf16String(); } catch (e) {}
            let storage = null, val = null, valType = null;
            try {
                storage = v.add(24 + i * 8).readPointer();
                if (storage && !storage.isNull()) {
                    val = storage.readPointer();
                    if (val && !val.isNull() && val.compare(ptr("0x10000")) > 0)
                        valType = typeName(val);
                }
            } catch (e) {}
            out.push({ name: name, storage: storage, val: val, type: valType });
        }
    } catch (e) {}
    return out;
}

const virtFieldIdx = {};        // type ptr -> { name -> field index }

function virtualFieldIndex(t, want) {
    const key = t.toString();
    let map = virtFieldIdx[key];
    if (map === undefined) {
        map = {};
        try {
            const vt = t.add(8).readPointer();
            const fields = vt.readPointer();
            const n = vt.add(8).readS32();
            for (let i = 0; i < n && i < 64; i++) {
                let nm = null;
                try { nm = fields.add(i * 24).readPointer().readUtf16String(); }
                catch (e) {}
                if (nm) map[nm] = i;
            }
        } catch (e) {}
        virtFieldIdx[key] = map;
    }
    const idx = map[want];
    return idx === undefined ? -1 : idx;
}

// Returns { item, count } for a slot virtual, or null if it isn't one.
function readSlot(p) {
    try {
        if (!p || p.isNull()) return null;
        const t = p.readPointer();
        if (t.readU32() !== HVIRTUAL) return null;
        const ii = virtualFieldIndex(t, "item");
        if (ii < 0) return null;
        const st = p.add(24 + ii * 8).readPointer();
        if (!st || st.isNull()) return null;
        const item = st.readPointer();
        if (!item || item.isNull() || item.compare(ptr("0x10000")) <= 0) return null;
        let count = null;
        const ci = virtualFieldIndex(t, "count");
        if (ci >= 0) {
            try {
                const cs = p.add(24 + ci * 8).readPointer();
                if (cs && !cs.isNull()) count = cs.readS32();
            } catch (e) {}
        }
        return { item: item, count: count };
    } catch (e) { return null; }
}

// One watcher per container so the log says WHERE the item appeared.
const known = {};                   // container -> { uid -> desc }
let lastWeapon = null;              // weaponInHand uid, for equip edges
let firstSweepDone = {};

function sweepContainer(name, invPtr) {
    if (!invPtr || invPtr.isNull()) return;
    const content = readArray(invPtr.add(P.Inventory.content).readPointer());
    const now = {};
    for (const raw of content) {
        if (!raw) continue;
        const slot = readSlot(raw);          // entries are slot virtuals
        if (!slot) continue;
        // Only trust entries that decode as an item class. Without this, a
        // misread slot is logged as if it were an item — which is exactly how
        // round 4 came to report a shader source path as an item `kind`.
        const cls = typeName(slot.item);
        if (!cls || (cls.lastIndexOf("st.Item", 0) !== 0
                     && cls.lastIndexOf("st.item.", 0) !== 0)) continue;
        const uid = itemUid(slot.item);
        if (uid === null) continue;
        now[uid] = slot;
    }
    const prev = known[name] || {};
    if (!firstSweepDone[name]) {
        firstSweepDone[name] = true;
        log("[" + name + "] initial: " + Object.keys(now).length + " items");
        for (const uid in now) log("    " + uid + "  " + slotDesc(now[uid]));
    } else {
        for (const uid in now)
            if (!(uid in prev))
                log(">>> ADD    [" + name + "] uid=" + uid + "  " + slotDesc(now[uid]));
        for (const uid in prev)
            if (!(uid in now))
                log("<<< REMOVE [" + name + "] uid=" + uid);
    }
    const snap = {};
    for (const uid in now) snap[uid] = true;
    known[name] = snap;
}

// Timer thread: pure pointer/string reads from here down — nothing that can
// touch the HL GC.
let ticks = 0;

function heartbeat() {
    // Only ever reached while the hero is still missing. Says which half is
    // broken instead of staying silent.
    if (frames === 0) {
        log("[wait] postUpdate hook has fired 0 times after " + ticks +
            " ticks — the hook is attached but the function is not running. "
            + "Wrong findex, or the camera class in use is not BaseCamera.");
        return;
    }
    const parts = [];
    for (const k in heroOutcomes) parts.push(k + " x" + heroOutcomes[k]);
    log("[wait] frames=" + frames + " heroTries=" + heroTries +
        " — no ent.Hero yet. Outcomes: " +
        (parts.length ? parts.join(" | ") : "(none)") +
        ". If the game is at a menu / loading screen this is expected.");
}

// Round 5. The static layout is right on paper — ent.Hero.loadout@1232 IS
// declared st.Loadout, st.Inventory.content@120 IS an hl.types.ArrayObj — and
// weaponInHand off the same hero decodes perfectly. Yet the container walk came
// back with entries whose `kind` read "shaders/ColorMap.hx", i.e. some internal
// string array. So one of the hops does not hold what it's declared to hold at
// runtime. This prints the type at EVERY hop instead of assuming any of them.
function dumpChain() {
    function hop(label, p) {
        if (!p || p.isNull()) { log("  " + label + " = NULL"); return null; }
        let kindInt = "?";
        try { kindInt = p.readPointer().readU32(); } catch (e) {}
        log("  " + label + " = " + p + "  type=" + (typeName(p) || "<null>")
            + "  typeKind=" + kindInt);
        return p;
    }
    log("--- chain dump ---");
    hop("localHero", localHero);
    const loadout = hop("loadout@" + P.Hero.loadout,
                        localHero.add(P.Hero.loadout).readPointer());
    if (!loadout) return;
    for (const [nm, off] of [["inventory", P.Loadout.inventory],
                             ["equipment", P.Loadout.equipment]]) {
        const inv = hop("  " + nm + "@" + off, loadout.add(off).readPointer());
        if (!inv) continue;
        const arr = hop("    content@" + P.Inventory.content,
                        inv.add(P.Inventory.content).readPointer());
        if (!arr) continue;
        try {
            log("      ArrayObj.length@" + OFF.ArrayObj.length + " = "
                + arr.add(OFF.ArrayObj.length).readS32()
                + "   array@" + OFF.ArrayObj.array + " = "
                + arr.add(OFF.ArrayObj.array).readPointer());
        } catch (e) { log("      array header unreadable: " + e); }
        const els = readArray(arr);
        log("      " + els.length + " entries; first 3 typed:");
        for (let i = 0; i < Math.min(3, els.length); i++) {
            const e = els[i];
            if (!e) { log("        [" + i + "] null"); continue; }
            let ki = "?";
            try { ki = e.readPointer().readU32(); } catch (x) {}
            log("        [" + i + "] " + e + "  type=" + (typeName(e) || "<null>")
                + "  typeKind=" + ki);
            // typeKind 15 is HVIRTUAL. hl_vvirtual is
            // { hl_type *t; vdynamic *value; vvirtual *next; } so the concrete
            // object should be at +8 — verify, don't assume.
            const vf = virtualFields(e);
            if (!vf.length) log("             (no virtual field table)");
            for (const f of vf)
                log("             ." + (f.name || "?") + " -> " + f.val
                    + "  type=" + (f.type || "<not an object>"));
        }
    }
    // The one hop that DOES work, for contrast.
    hop("weaponInHand@" + P.Hero.weaponInHand,
        localHero.add(P.Hero.weaponInHand).readPointer());
    log("--- end chain dump ---");
}
let chainDumped = false;

function sweep() {
    try {
        ticks++;
        if (!localHero || localHero.isNull()) {
            if (ticks % 10 === 0) heartbeat();      // every 5s
            return;
        }
        if (!heroAnnounced) {
            heroAnnounced = true;
            log("localHero = " + localHero + " (acquired on the game thread"
                + " after " + frames + " frames)");
            log("PROBE ARMED — inventory, equipment and weaponInHand are being "
                + "watched.");
        }
        if (!chainDumped) { chainDumped = true; dumpChain(); }
        const loadout = localHero.add(P.Hero.loadout).readPointer();
        if (!loadout || loadout.isNull()) return;
        sweepContainer("inventory", loadout.add(P.Loadout.inventory).readPointer());
        sweepContainer("equipment", loadout.add(P.Loadout.equipment).readPointer());

        const w = localHero.add(P.Hero.weaponInHand).readPointer();
        const wUid = (w && !w.isNull() && w.compare(ptr("0x10000")) > 0)
            ? itemUid(w) : null;
        if (wUid !== lastWeapon) {
            log("=== WEAPON IN HAND: " +
                (wUid === null ? "(none)" : "uid=" + wUid + "  " + itemDesc(w)));
            lastWeapon = wUid;
        }
    } catch (e) { log("sweep ERR " + e); }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    // The one Interceptor in this probe, and it exists only to borrow the
    // game thread for the hero lookup. postUpdate runs every frame; the body
    // is a cached-pointer check once the hero is found.
    if (P.fn.postUpdate == null) {
        log("!! client.BaseCamera.postUpdate findex missing - no game-thread "
            + "anchor, refusing to call HL functions from a timer.");
        return;
    }
    Interceptor.attach(base.add(P.fn.postUpdate * 8).readPointer(), {
        onEnter: function () {
            frames++;
            refreshLocalHeroOnGameThread();
        }
    });
    log("hooked client.BaseCamera.postUpdate (findex " + P.fn.postUpdate
        + "); waiting for the hero. Nothing is armed until the ARMED line "
        + "prints.");
    setInterval(sweep, 500);
}

// Deferred so the table scan can't time out script.load() on the host side.
setTimeout(main, 0);
