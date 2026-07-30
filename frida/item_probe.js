// item_probe.js — ITEM PICKUP SPIKE. Throwaway.
//
// Goal: find the hook point for "the local player just picked up an item",
// with enough detail to tell a legendary weapon from everything else. The
// bytecode names several candidates and this measures which of them actually
// fire on a pickup, and what they can see:
//
//   ui.notify.NotifyManager.queue/show  the game's own "you got an item"
//   ui.notify.ItemGain.init             toast — client-side, like the boss bar
//   st.Loadout.gainItem/onGainItems     inventory mutation underneath
//   st.Player.notifyItemLooted          loot bookkeeping
//   st.Player.makeItemPickup            spawning the pickup orb (drop, not gain?)
//   ent.Hero.addInventoryDrop           inventory drops arriving on the hero
//   st.skill.SkillObject.rpcDoPickup    walking over a ground pickup
//
// What the bytecode CANNOT say, and what this answers:
//   1. which of these fire on a plain world pickup, and which on vendor/craft/
//      quest gains too (we want pickups, not "bought 200 arrows")
//   2. what an ItemGain's `item` virtual actually wraps at runtime, and
//      whether `rarity` (a String on st.item.Gear/Weapon) reads "legendary"
//      or "Legendary" or something else entirely
//   3. whether the toast fires once per stack or once per item (count field)
//
// Every hook body is a try/catch'd read-and-log; the calls all happen on the
// game thread because they happen inside game functions.
//
// DATA + OFF + I are prepended by run_item.py.

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
const hookCounts = {};              // hook name -> fires
const seen = {};                    // "kind|rarity" -> {kind,rarity,cls,n,via:{}}
const events = [];                  // queued lines, drained by the timer

function note(hook, line) {
    hookCounts[hook] = (hookCounts[hook] || 0) + 1;
    events.push("[" + hook + "] " + line);
}

// A String field is only trusted if it reads like an identifier — reading a
// "rarity" offset off the wrong class dereferences arbitrary memory, and the
// difference between garbage and data has to be decided here, not upstream.
function saneStr(s) {
    return (typeof s === "string" && s.length >= 2 && s.length <= 40 &&
            /^[A-Za-z0-9_\-. ]+$/.test(s)) ? s : null;
}

// Unwrap a field that may hold the object directly or behind an HL virtual
// (vvirtual = { hl_type*, value, next } — the wrapped object sits at +8).
function unwrap(p) {
    if (!p || p.isNull()) return { obj: null, cls: null, via: "null" };
    let cls = typeName(p);
    if (cls) return { obj: p, cls: cls, via: "direct" };
    try {
        const inner = p.add(8).readPointer();
        cls = typeName(inner);
        if (cls) return { obj: inner, cls: cls, via: "virtual" };
    } catch (e) {}
    return { obj: p, cls: null, via: "opaque" };
}

// Describe anything item-shaped: class, kind string, rarity if it reads sanely.
function describeItem(p, hook) {
    const u = unwrap(p);
    if (!u.obj) return "(null item)";
    if (!u.cls) {
        // Show raw qwords so a surprising layout is diagnosable from the log.
        let raw = "";
        try {
            for (let i = 0; i < 4; i++)
                raw += " +".concat(i * 8, "=", u.obj.add(i * 8).readPointer());
        } catch (e) {}
        return "(unknown type, via " + u.via + ")" + raw;
    }
    let kind = null, rarity = null, level = null;
    try { kind = saneStr(hlStr(u.obj.add(I.Item.kind).readPointer())); } catch (e) {}
    // rarity exists ONLY on st.item.Weapon (measured offline) — reading that
    // offset off any other class dereferences past the object's end.
    if (u.cls === "st.item.Weapon") {
        if (I.Weapon.rarity != null) {
            try { rarity = saneStr(hlStr(u.obj.add(I.Weapon.rarity).readPointer())); } catch (e) {}
        }
        if (I.Weapon.level != null) {
            try { level = u.obj.add(I.Weapon.level).readS32(); } catch (e) {}
        }
    }
    const desc = u.cls + (u.via === "virtual" ? " (via virtual)" : "") +
        "  kind=" + (kind || "?") +
        "  rarity=" + (rarity || "-") +
        (level !== null && level >= 0 && level < 1000 ? "  lvl=" + level : "");
    if (kind) {
        const key = kind + "|" + (rarity || "-");
        const rec = seen[key] || (seen[key] = { kind: kind, rarity: rarity || "-",
                                                cls: u.cls, n: 0, via: {} });
        rec.n++;
        rec.via[hook] = (rec.via[hook] || 0) + 1;
    }
    return desc;
}

function describeArg(p, hook) {
    if (!p || p.isNull()) return "null";
    const u = unwrap(p);
    if (u.cls && (u.cls.indexOf("st.item.") === 0 || u.cls === "st.Item"))
        return describeItem(p, hook);
    if (u.cls === "String")
        return "String\"" + (hlStr(u.obj) || "?") + "\"";
    if (u.cls) return u.cls + (u.via === "virtual" ? "(via virtual)" : "");
    // Opaque: show enough structure to identify the layout next round —
    // the raw qwords, and the class of anything the first three point at.
    let out = "opaque " + p;
    try {
        for (let i = 0; i < 3; i++) {
            const q = p.add(i * 8).readPointer();
            const c = typeName(q);
            out += " +" + (i * 8) + "=" + (c ? ("[" + c + "]") : q);
        }
    } catch (e) {}
    return out;
}

function gainFields(n, hook) {
    let k = null, cnt = null;
    try { k = hlStr(n.add(I.ItemGain.kind).readPointer()); } catch (e) {}
    try { cnt = n.add(I.ItemGain.count).readS32(); } catch (e) {}
    let it = null;
    try { it = n.add(I.ItemGain.item).readPointer(); } catch (e) {}
    return "kind=" + (k || "?") + " count=" + cnt +
           "  item: " + describeItem(it, hook);
}

function hook(name, findex, handler) {
    if (findex == null) { log("  (no findex for " + name + " - skipped)"); return; }
    try {
        Interceptor.attach(base.add(findex * 8).readPointer(), handler);
        log("hooked " + name + " (findex " + findex + ")");
    } catch (e) {
        log("!! hook failed for " + name + ": " + e);
    }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);

    // The toast pipeline. Round 1 measured queue/show receiving a String as
    // arg1 — a notify KIND, not the notify object — firing constantly even
    // with no pickups. So: log the string's content (rate-limited) to learn
    // the kind vocabulary, and describe arg2, which is presumably the args.
    const kindCounts = {};
    for (const nm of ["NotifyManager.queue", "NotifyManager.show"]) {
        hook(nm, I.fn[nm], {
            onEnter: function () {
                try {
                    const n = this.context.rdx;
                    const cls = typeName(n) || "?";
                    if (cls === "String") {
                        const s = hlStr(n) || "?";
                        // The chatter kind would drown everything: log each
                        // distinct string the first 3 times only.
                        kindCounts[s] = (kindCounts[s] || 0) + 1;
                        if (kindCounts[s] <= 3)
                            note(nm, "kind=\"" + s + "\" (seen " + kindCounts[s] +
                                     "x)  arg2=" + describeArg(this.context.r8, nm) +
                                     "  arg3=" + describeArg(this.context.r9, nm));
                    } else if (cls === "ui.notify.ItemGain") {
                        note(nm, "ItemGain " + gainFields(n, nm));
                    } else {
                        note(nm, "notify=" + cls);
                    }
                } catch (e) { note(nm, "ERR " + e); }
            }
        });
    }

    // The factory: whatever object the manager builds for a queued kind comes
    // back through getNotify's return value.
    hook("NotifyManager.getNotify", I.fn["NotifyManager.getNotify"], {
        onLeave: function (retval) {
            try {
                const cls = typeName(retval) || "?";
                if (cls === "ui.notify.ItemGain")
                    note("NotifyManager.getNotify", "-> ItemGain " +
                         gainFields(retval, "NotifyManager.getNotify"));
                else
                    note("NotifyManager.getNotify", "-> " + cls);
            } catch (e) { note("NotifyManager.getNotify", "ERR " + e); }
        }
    });

    // Rendering the toast. Round 2 measured rcx NOT being an ItemGain (reads
    // came back as random heap objects and even code bytes), so no register
    // is assumed to be `this` — log all four and let the run say which one
    // is item-shaped, if any.
    hook("ItemGain.getItemFormat", I.fn["ItemGain.getItemFormat"], {
        onEnter: function () {
            try {
                note("ItemGain.getItemFormat",
                     "rcx=" + describeArg(this.context.rcx, "ItemGain.getItemFormat") +
                     "  rdx=" + describeArg(this.context.rdx, "ItemGain.getItemFormat") +
                     "  r8=" + describeArg(this.context.r8, "ItemGain.getItemFormat") +
                     "  r9=" + describeArg(this.context.r9, "ItemGain.getItemFormat"));
            } catch (e) { note("ItemGain.getItemFormat", "ERR " + e); }
        }
    });

    // The state layer underneath. Args unknown, so log the register types and
    // let the run tell us which position carries the item.
    // Round 1: Loadout.addItem is dropped — it fired in a constant alternating
    // pattern with hxbit.NetworkSerializer args, i.e. periodic network sync,
    // and never anything item-shaped. None of the others fired on a real
    // drop->pickup->equip, so HeroData/Progress joined the list as the
    // client-side loot bookkeeping candidates.
    for (const nm of ["Loadout.gainItem", "Loadout.onGainItems",
                      "Player.notifyItemLooted", "Player.makeItemPickup",
                      "Hero.addInventoryDrop", "SkillObject.rpcDoPickup",
                      "HeroData.addLootEntry", "Progress.incrementItemLoot",
                      "ItemGain.init", "SideNotify.init"]) {
        hook(nm, I.fn[nm], {
            onEnter: function () {
                try {
                    note(nm, "rdx=" + describeArg(this.context.rdx, nm) +
                             "  r8=" + describeArg(this.context.r8, nm) +
                             "  r9=" + describeArg(this.context.r9, nm));
                } catch (e) { note(nm, "ERR " + e); }
            }
        });
    }

    log("pick things up - junk, gear, and ideally a legendary. Vendor buys and");
    log("crafting are worth one try each too, to see which hooks those share.");
    setInterval(function () {
        while (events.length) log(events.shift());
    }, 500);
    recv("summary", function () {
        log("");
        log("================ SUMMARY ================");
        log("hook fire counts:");
        const hs = Object.keys(hookCounts).sort();
        if (!hs.length) log("  (nothing fired)");
        for (const h of hs) log("  " + h + ": " + hookCounts[h]);
        log("items seen (kind | rarity | class | times | which hooks):");
        const ks = Object.keys(seen).sort();
        if (!ks.length) log("  (none)");
        for (const k of ks) {
            const r = seen[k];
            log("  " + r.kind + " | " + r.rarity + " | " + r.cls + " | x" + r.n +
                " | " + Object.keys(r.via).sort().join(", "));
        }
        log("=========================================");
    });
}

// Deferred, not called inline: the table scan can take long enough that a
// synchronous run times out script.load() on the host side — the shipping
// hook defers its setup for the same reason.
setTimeout(main, 0);
