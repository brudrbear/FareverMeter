// meter_hook.js — persistent hook feeding the Farever+ party meter.
// Resolves the HL functions_ptrs table, identifies the local hero via
// ui.Console.getMyHero(), hooks ent.Unit.onInflictDamage, and streams EVERY
// player's (ent.Hero dealer) damage instance to Python as {kind:'hit', ...},
// tagged with the dealer's name and whether it's the local player.
//
// DATA (resolver_data.json) and OFF (meter_offsets.json) are prepended by the
// Python host.

function log(m) { send({ kind: "log", msg: String(m) }); }

function ptrPattern(addr) {
    const b = []; let v = uint64(addr.toString());
    for (let i = 0; i < 8; i++) { b.push(("0" + v.and(0xff).toNumber().toString(16)).slice(-2)); v = v.shr(8); }
    return b.join(" ");
}
function resolveAnchors() {
    const out = [], cache = {};
    for (const a of DATA.anchors) {
        try {
            if (!(a.module in cache)) cache[a.module] = Process.findModuleByName(a.module);
            const m = cache[a.module]; if (!m) continue;
            const ad = m.findExportByName(a.symbol);
            if (ad && !ad.isNull()) out.push({ findex: a.findex, addr: ad });
        } catch (e) {}
    }
    return out;
}
function isTableAt(base, resolved) {
    // The real table holds EVERY anchor's live address at findex*8, so three
    // exact pointer matches is conclusive; any mismatch rejects immediately.
    let n = 0;
    for (const o of resolved) {
        let v; try { v = base.add(o.findex * 8).readPointer(); } catch (e) { return false; }
        if (!v.equals(o.addr)) return false;
        if (++n >= 3) return true;
    }
    return n > 0;
}

// Fast path: libhl keeps its loaded-modules registry in a static (module.c's
// cur_modules), and the hl_module struct it reaches holds functions_ptrs — so
// a pointer walk seeded from the writable (statics) sections of libhl.dll and
// the host exe reaches the table in 0–2 hops with no heap scanning:
//     static -> hl_module** -> hl_module -> functions_ptrs
// No struct layout is assumed: every private-rw pointer reachable within two
// hops is simply *tested* against the anchors via isTableAt.
function findTableFast(resolved) {
    if (!resolved.length) return null;
    const t0 = Date.now(), BUDGET_MS = 4000, MAX_NODES = 30000, EXPAND = 64;
    const heap = Process.enumerateRanges("rw-").filter(r => !r.file)
        .sort((a, b) => a.base.compare(b.base));
    if (!heap.length) return null;
    const lo = heap[0].base;
    const hi = heap[heap.length - 1].base.add(heap[heap.length - 1].size);
    function inHeap(p) {
        if (p.compare(lo) < 0 || p.compare(hi) >= 0) return false;
        let a = 0, b = heap.length - 1;
        while (a <= b) {
            const mid = (a + b) >> 1, r = heap[mid];
            if (p.compare(r.base) < 0) b = mid - 1;
            else if (p.compare(r.base.add(r.size)) >= 0) a = mid + 1;
            else return true;
        }
        return false;
    }
    // Seeds: every 8-aligned qword in the statics of libhl.dll / the exe that
    // points into private rw- memory.
    let frontier = [];
    const mods = [Process.findModuleByName("libhl.dll"), Process.enumerateModules()[0]];
    for (const m of mods) {
        if (!m) continue;
        let secs; try { secs = m.enumerateRanges("rw-"); } catch (e) { continue; }
        for (const sec of secs) {
            const CH = 65536;
            for (let off = 0; off < sec.size && frontier.length < 50000; off += CH) {
                const n = Math.min(CH, sec.size - off);
                let buf; try { buf = sec.base.add(off).readByteArray(n); } catch (e) { continue; }
                const dv = new DataView(buf);
                for (let i = 0; i + 8 <= n; i += 8) {
                    const l = dv.getUint32(i, true), h = dv.getUint32(i + 4, true);
                    if ((l === 0 && h === 0) || (l & 7)) continue;
                    const v = ptr(h).shl(32).or(l);
                    if (inHeap(v)) frontier.push(v);
                }
            }
        }
    }
    const seen = new Set();
    for (let depth = 0; depth <= 2 && frontier.length; depth++) {
        const next = [];
        for (const p of frontier) {
            const k = p.toString();
            if (seen.has(k)) continue;
            seen.add(k);
            if (seen.size > MAX_NODES || Date.now() - t0 > BUDGET_MS) return null;
            if (isTableAt(p, resolved)) return p;
            if (depth === 2) continue;
            for (let i = 0; i < EXPAND; i++) {
                let v; try { v = p.add(i * 8).readPointer(); } catch (e) { break; }
                if (v.and(7).toInt32() === 0 && inHeap(v)) next.push(v);
            }
        }
        frontier = next;
    }
    return null;
}

function findTableBase(resolved) {
    // functions_ptrs is a libhl heap allocation → anonymous rw- memory. Scan
    // anonymous ranges FIRST, smallest first (we return as soon as the table is
    // found, so the common case never touches the big heap segments or the
    // file-backed image/asset ranges). The size cap is a sanity bound only —
    // HL heap segments can exceed 128 MiB on some machines, and a cap below the
    // segment holding the table makes it unfindable, so keep this generous.
    const ranges = Process.enumerateRanges("rw-")
        .filter(r => r.size < 0x40000000)
        .sort((a, b) => ((a.file ? 1 : 0) - (b.file ? 1 : 0)) || (a.size - b.size));
    // On a matching build the table is found on the first seed; extra seeds
    // only ever run when the shipped data doesn't match this build, so cap low
    // to fail fast (the meter then auto-regenerates the data).
    const seeds = Math.min(resolved.length, 3);
    const total = ranges.length * seeds;
    let done = 0, lastProg = Date.now();
    for (let s = 0; s < seeds; s++) {
        const seed = resolved[s], pat = ptrPattern(seed.addr);
        for (const r of ranges) {
            // Heartbeat so the host can tell "scan in progress" from "hook
            // dead" and keep waiting instead of killing a live scan.
            done++;
            const now = Date.now();
            if (now - lastProg > 1500) {
                lastProg = now;
                send({ kind: "progress", done: done, total: total });
            }
            let mm; try { mm = Memory.scanSync(r.base, r.size, pat); } catch (e) { continue; }
            for (const m of mm) {
                const base = m.address.sub(seed.findex * 8);
                if (base.compare(r.base) < 0) continue;
                let agree = 0, checked = 0;
                for (const o of resolved) {
                    if (o === seed) continue;
                    const slot = base.add(o.findex * 8);
                    if (slot.compare(r.base) < 0 || slot.add(8).compare(r.base.add(r.size)) > 0) continue;
                    checked++;
                    let v; try { v = slot.readPointer(); } catch (e) { continue; }
                    if (v.equals(o.addr)) agree++;
                    if (checked >= 8) break;
                }
                if (agree >= 3) return base;
            }
        }
    }
    return null;
}
function hlStr(p) {
    try {
        if (!p || p.isNull()) return null;
        const b = p.add(OFF.String.bytes).readPointer();
        if (b.isNull()) return null;
        return b.readUtf16String();
    } catch (e) { return null; }
}
function typeName(p) {
    try {
        if (!p || p.isNull()) return null;
        const t = p.readPointer(); const k = t.readU32();
        if (k === 11 || k === 21) return t.add(8).readPointer().add(16).readPointer().readUtf16String();
        return "kind" + k;
    } catch (e) { return null; }
}

let base = null;
let getHeroFns = [];      // [{name, addr}]
let localHero = null;
let localName = null;
let partyNames = {};      // set of names in the local player's group (incl. self)
const heroByName = {};    // name -> {ptr: ent.Hero*, t: last-seen ms} (from hits)

function inCombat(hero) {
    try {
        if (!hero || hero.isNull()) return 0;
        return hero.add(OFF.Hero.isInCombat).readU8() ? 1 : 0;
    } catch (e) { return 0; }
}

// Zone signature via Main.getMapId(). This allocates an HL string, so it must
// only be called from the game thread (i.e. inside the damage hook), never a
// background timer — doing so from an unregistered thread crashes the runtime.
// Changes on any loading screen / instance entry -> auto-reset.
let mapFn = null;
let lastZoneSig = null;
let lastZoneCheck = 0;
function checkZone() {
    const now = Date.now();
    if (!mapFn || now - lastZoneCheck < 1000) return;   // throttle to ~1/sec
    lastZoneCheck = now;
    try {
        const sig = hlStr(mapFn());
        if (sig === null) return;
        if (lastZoneSig !== null && sig !== lastZoneSig) {
            resetWindows();          // the whole UI is rebuilt across a load
            send({ kind: "zone", sig: sig });
        }
        lastZoneSig = sig;
    } catch (e) {}
}

// ---- the game's own window state (native UI awareness) ----
// ui.BaseUI.displayWindow(ui, win) / removeWindow(ui, win) fire for EVERY game
// window, so tracking them gives Python a live "which game windows are open"
// feed — the overlay follows the game's UI (escape menu open => unlock) instead
// of needing a hotkey. displayWindow fires TWICE per open, and several windows
// of one class can coexist, so instances are keyed by pointer and a class is
// only reported when its live count crosses zero.
const winClassOf = {};    // window instance ptr -> class name
const winOpenCount = {};  // class name -> live instance count

function windowOpened(win) {
    try {
        if (!win || win.isNull()) return;
        const key = win.toString();
        if (key in winClassOf) return;              // the duplicate displayWindow
        const nm = typeName(win);
        if (!nm || nm.lastIndexOf("kind", 0) === 0) return;
        winClassOf[key] = nm;
        winOpenCount[nm] = (winOpenCount[nm] || 0) + 1;
        if (winOpenCount[nm] === 1) send({ kind: "window", name: nm, open: 1 });
    } catch (e) {}
}

function windowClosed(win) {
    try {
        if (!win || win.isNull()) return;
        const key = win.toString();
        const nm = winClassOf[key];
        if (!nm) return;                            // not one we're tracking
        delete winClassOf[key];
        if (--winOpenCount[nm] <= 0) {
            delete winOpenCount[nm];
            send({ kind: "window", name: nm, open: 0 });
        }
    } catch (e) {}
}

function resetWindows() {
    for (const nm in winOpenCount) send({ kind: "window", name: nm, open: 0 });
    for (const k in winClassOf) delete winClassOf[k];
    for (const k in winOpenCount) delete winOpenCount[k];
}

// ---- skill display-name resolution (CDB, via libhl dynamic field access) ----
// baseSkill.inf is a vvirtual over the CDB skill row; its `texts.name` is the
// localized display name (e.g. Warrior_Rage_Strike -> "Rage Strike"). We read
// it with hl_obj_get_field + hl_hash_utf8, cached per skill id.
let hl_getField = null, hl_hashUtf8 = null;
const fieldHash = {};     // field name -> interned HL hash
const nameCache = {};     // skill id -> display name ("" if none)

function setupNameApi() {
    try {
        const m = Process.findModuleByName("libhl.dll");
        hl_getField = new NativeFunction(m.findExportByName("hl_obj_get_field"),
                                         "pointer", ["pointer", "int"]);
        hl_hashUtf8 = new NativeFunction(m.findExportByName("hl_hash_utf8"),
                                         "int", ["pointer"]);
        for (const n of ["texts", "name"]) fieldHash[n] = hl_hashUtf8(Memory.allocUtf8String(n));
        return true;
    } catch (e) { return false; }
}

function getField(obj, name) {
    try {
        if (!obj || obj.isNull()) return null;
        return hl_getField(obj, fieldHash[name]);
    } catch (e) { return null; }
}

function skillDisplayName(baseSkill, id) {
    if (id in nameCache) return nameCache[id];
    let nm = "";
    if (hl_getField) {
        try {
            const inf = baseSkill.add(OFF.BaseSkill.inf).readPointer();
            const texts = getField(inf, "texts");
            nm = hlStr(getField(texts, "name")) || "";
        } catch (e) {}
    }
    nameCache[id] = nm;
    return nm;
}

// Walk the local player's group roster -> {name: 1}. groupId is unreliable (0),
// but group.players lists the actual party members. Traversal:
//   Player.group -> st.Group
//   Group.players -> hxbit.ArrayProxyData (.array @40 -> hl.types.ArrayDyn)
//   ArrayDyn.array(@8) -> ArrayObj; .length(@8), native varray(@16)
//   varray elements start at +24 (hl_varray header), each an st.Player*
function readParty(hero) {
    const names = {};
    try {
        const player = hero.add(OFF.Hero.player).readPointer();
        if (!player || player.isNull()) return names;
        const group = player.add(OFF.Player.group).readPointer();
        if (!group || group.isNull()) return names;
        const proxy = group.add(OFF.Group.players).readPointer();
        const arrDyn = proxy.add(40).readPointer();
        const arrObj = arrDyn.add(8).readPointer();
        const length = arrObj.add(8).readS32();
        const varr = arrObj.add(16).readPointer();
        if (length < 0 || length > 64) return names;
        for (let i = 0; i < length; i++) {
            const p = varr.add(24 + i * 8).readPointer();
            const nm = hlStr(p.add(OFF.Player.name).readPointer());
            if (nm) names[nm] = 1;
        }
    } catch (e) {}
    return names;
}

function refreshLocalHero() {
    for (const f of getHeroFns) {
        try {
            const h = new NativeFunction(f.addr, "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") {
                localHero = h;
                partyNames = readParty(h);
                const nm = hlStr(h.add(OFF.Hero.name).readPointer());
                if (nm) partyNames[nm] = 1;   // always include self
                localName = nm;
                send({ kind: "hero", name: localName,
                       party: Object.keys(partyNames) });
                return;
            }
        } catch (e) {}
    }
}

function main() {
    const resolved = resolveAnchors();
    const t0 = Date.now();
    base = findTableFast(resolved);
    if (base) {
        log("functions_ptrs via statics walk (" + (Date.now() - t0) + " ms)");
    } else {
        log("statics walk missed; falling back to memory scan ...");
        base = findTableBase(resolved);
        if (base) log("functions_ptrs via memory scan (" + (Date.now() - t0) + " ms)");
    }
    if (!base) { log("!! HL functions_ptrs table not found"); send({ kind: "ready", ok: false }); return; }

    if (!setupNameApi()) log("skill-name API unavailable; showing raw ids");

    if (DATA.map_fn != null) {
        try { mapFn = new NativeFunction(base.add(DATA.map_fn * 8).readPointer(),
                                         "pointer", []); }
        catch (e) { log("map fn resolve failed; zone auto-reset disabled"); }
    }

    for (const nm in DATA.funcs) {
        try { getHeroFns.push({ name: nm, addr: base.add(DATA.funcs[nm] * 8).readPointer() }); } catch (e) {}
    }
    refreshLocalHero();
    setInterval(refreshLocalHero, 3000);   // survive respawn / zone changes

    // Combat-state heartbeat: report isInCombat for the local hero and every
    // player we've seen deal damage, so Python can drive the capture timer.
    setInterval(function () {
        const now = Date.now();
        const state = {};
        if (localHero && localName) state[localName] = inCombat(localHero);
        for (const nm in heroByName) {
            if (now - heroByName[nm].t > 60000) { delete heroByName[nm]; continue; }
            state[nm] = inCombat(heroByName[nm].ptr);
        }
        send({ kind: "combat", state: state });
    }, 400);

    const fi = DATA.count_targets["ent.Unit.onInflictDamage"];
    const daddr = base.add(fi * 8).readPointer();
    const DR = OFF.DamageResult, BS = OFF.BaseSkill;

    // Read the common hit fields off a st.skill.DamageResult* (heals reuse the
    // same struct — evalHeal/onInflictHealEval mirror the damage pipeline).
    function readResult(dr) {
        const amount = dr.add(DR._amount).readDouble();
        let skill = null, sname = "";
        const bs = dr.add(DR.baseSkill).readPointer();
        if (bs && !bs.isNull()) {
            skill = hlStr(bs.add(BS.kind).readPointer());
            if (skill) sname = skillDisplayName(bs, skill);
        }
        return {
            amount: amount,
            skill: skill || "?",
            name: sname,
            element: hlStr(dr.add(DR.affinity).readPointer()) || "?",
            crit: dr.add(DR._critical).readU8() ? 1 : 0,
            kill: dr.add(DR._kill).readU8() ? 1 : 0,
        };
    }

    function heroIdent(hero) {
        const name = hlStr(hero.add(OFF.Hero.name).readPointer());
        const is_me = (localHero && hero.equals(localHero)) ? 1 : 0;
        const in_party = (is_me || partyNames[name] === 1) ? 1 : 0;
        if (name) heroByName[name] = { ptr: hero, t: Date.now() };
        return { player: name || "?", is_me: is_me, in_party: in_party };
    }

    Interceptor.attach(daddr, {
        onEnter() {
            try {
                checkZone();   // safe here (game thread); throttled internally
                const dealer = this.context.rcx;
                // Only players (ent.Hero) — excludes monster/boss/summon dealers.
                if (typeName(dealer) !== "ent.Hero") return;
                const dr = this.context.rdx;
                const r = readResult(dr);
                if (!(r.amount > 0)) return;
                const who = heroIdent(dealer);
                send(Object.assign({ kind: "hit" }, who, r));
            } catch (e) {}
        }
    });

    // ---- healing ----
    // Heals are computed server-side only (ent.Unit.receiveHeal, computeHeal
    // and the *HealEval callbacks never run on clients), and the display RPC
    // is rare — so healing is captured from what IS replicated:
    //   * ent.Unit.playHitHealFX(target=rcx, hitData=rdx): fires on every
    //     healed unit; HitData.baseSkill names the healing skill and its
    //     owner (the healer). No amount (HitData.amount reads 0 on clients).
    //   * ent.UnitAttributes.set_health(attrs=rcx, v): the replicated health
    //     value. A RISE in a hero's health is the effective heal amount.
    // Each health rise is attributed to the most recent heal FX seen on that
    // unit; FX-less rises while in combat are natural regen (self-heal), and
    // out-of-combat regen / spawn replication (old health 0) is dropped.
    function skillOwnerIdent(bs) {
        const owner = bs.add(OFF.BaseSkill.owner).readPointer();
        if (typeName(owner) === "ent.Hero") return heroIdent(owner);
        const pl = bs.add(OFF.BaseSkill.ownerPlayer).readPointer();
        if (typeName(pl) === "st.Player") {
            const nm = hlStr(pl.add(OFF.Player.name).readPointer());
            if (nm) {
                const is_me = pl.add(OFF.Player.isMe).readU8() ? 1 : 0;
                return { player: nm, is_me: is_me,
                         in_party: (is_me || partyNames[nm] === 1) ? 1 : 0 };
            }
        }
        return null;
    }

    const fxFi = DATA.count_targets["ent.Unit.playHitHealFX"]
        || (DATA.candidates && DATA.candidates["ent.Unit.playHitHealFX"]);
    const shFi = DATA.count_targets["ent.UnitAttributes.set_health"]
        || (DATA.candidates && DATA.candidates["ent.UnitAttributes.set_health"]);
    const UA = OFF.UnitAttributes, HD = OFF.HitData;
    if (fxFi == null || shFi == null || !UA || !HD || OFF.BaseSkill.owner == null) {
        log("heal data missing (playHitHealFX/set_health findex or offsets); "
            + "healing capture disabled — re-run hltools/build_targets.py "
            + "and hltools/emit_offsets.py");
    } else {
        const pendingHealFx = {};   // target unit ptr -> {t, who, skill, name}
        setInterval(function () {   // prune stale FX (overheal never lands)
            const now = Date.now();
            for (const k in pendingHealFx)
                if (now - pendingHealFx[k].t > 5000) delete pendingHealFx[k];
        }, 2000);

        Interceptor.attach(base.add(fxFi * 8).readPointer(), {
            onEnter() {
                try {
                    const c = this.context;
                    const target = c.rcx;
                    if (typeName(target) !== "ent.Hero") return;
                    if (typeName(c.rdx) !== "st.skill.HitData") return;
                    const bs = c.rdx.add(HD.baseSkill).readPointer();
                    if (!bs || bs.isNull()) return;
                    const who = skillOwnerIdent(bs);
                    if (!who) return;
                    const skill = hlStr(bs.add(BS.kind).readPointer()) || "?";
                    pendingHealFx[target.toString()] = {
                        t: Date.now(), who: who, skill: skill,
                        name: skill !== "?" ? skillDisplayName(bs, skill) : "",
                    };
                } catch (e) {}
            }
        });

        Interceptor.attach(base.add(shFi * 8).readPointer(), {
            onEnter() {
                this.attrs = this.context.rcx;
                try { this.oldHp = this.attrs.add(UA.health).readDouble(); }
                catch (e) { this.oldHp = null; }
            },
            onLeave() {
                try {
                    // old <= 0 => spawn/initial replication, not a heal
                    if (this.oldHp === null || !(this.oldHp > 0)) return;
                    const nv = this.attrs.add(UA.health).readDouble();
                    const delta = nv - this.oldHp;
                    if (!(delta > 0)) return;
                    const unit = this.attrs.add(UA.unit).readPointer();
                    if (typeName(unit) !== "ent.Hero") return;
                    const key = unit.toString();
                    const fx = pendingHealFx[key];
                    let who, skill, sname;
                    if (fx && Date.now() - fx.t < 1500) {
                        who = fx.who; skill = fx.skill; sname = fx.name;
                        delete pendingHealFx[key];
                    } else {
                        // FX-less rise: natural regen. Only meaningful in
                        // combat — out-of-combat regen is constant noise.
                        if (!unit.add(OFF.Hero.isInCombat).readU8()) return;
                        who = heroIdent(unit);
                        skill = "Regen"; sname = "Regen";
                    }
                    send(Object.assign({ kind: "heal", skill: skill,
                                         name: sname, element: "Heal",
                                         amount: delta, crit: 0, kill: 0 },
                                       who));
                } catch (e) {}
            }
        });
    }
    // ---- native UI awareness: stream game window open/close ----
    const UIT = DATA.ui_targets || {};
    const dispFi = UIT["ui.BaseUI.displayWindow"];
    const remFi = UIT["ui.BaseUI.removeWindow"];
    const onRemFi = UIT["ui.win.BaseWindow.onRemove"];
    if (dispFi == null || remFi == null) {
        log("ui_targets missing (displayWindow/removeWindow findex); game-menu "
            + "awareness disabled — re-run hltools/build_targets.py");
    } else {
        // Both take (baseUI = rcx, window = rdx).
        Interceptor.attach(base.add(dispFi * 8).readPointer(), {
            onEnter() { windowOpened(this.context.rdx); }
        });
        Interceptor.attach(base.add(remFi * 8).readPointer(), {
            onEnter() { windowClosed(this.context.rdx); }
        });
        if (onRemFi != null) {
            // Safety net: a window disposed without going through removeWindow
            // would otherwise leave the overlay stuck unlocked. (window = rcx)
            Interceptor.attach(base.add(onRemFi * 8).readPointer(), {
                onEnter() { windowClosed(this.context.rcx); }
            });
        }
        log("game window tracking active");
    }

    log("meter hook active (local hero: " + (localName || "pending") + ")");
    send({ kind: "ready", ok: true });
}
// Defer setup off the load() call so script.load() returns immediately. Running
// the memory scan synchronously inside load() blocks the injection handshake and
// can stall the game's thread mid-inject; deferring lets injection settle first.
setTimeout(main, 150);
