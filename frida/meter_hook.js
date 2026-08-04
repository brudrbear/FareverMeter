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
// Cached by type pointer. HL type descriptors are static for the life of the
// process, so this is safe — and it's what makes the world sweep affordable:
// a zone holds hundreds of entities but only a couple of dozen distinct
// classes, so after warmup naming one is a map hit instead of four memory
// reads and a UTF-16 decode, several hundred times a tick.
const typeNameCache = {};
function typeName(p) {
    try {
        if (!p || p.isNull()) return null;
        const t = p.readPointer();
        const key = t.toString();
        const hit = typeNameCache[key];
        if (hit !== undefined) return hit;
        const k = t.readU32();
        const nm = (k === 11 || k === 21)
            ? t.add(8).readPointer().add(16).readPointer().readUtf16String()
            : "kind" + k;
        typeNameCache[key] = nm;
        return nm;
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

// RETIRED: the zone signature used to come from Main.getMapId(), called from
// the game thread. Measured 2026-08-01: whatever that findex resolves to now
// returns the MACHINE HOSTNAME ('CAM-PC' — the user's PC name), which never
// changes — so the zone-change reset had gone silently dead. The zone signal
// now comes from layer.world.level in checkRift() below: the loaded level's
// own name, read with plain pointer walks (timer-safe, unlike an HL call).

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

// ---- rift + zone detection ----
// hero -> st.State.layer -> st.GameLayer: isRift for rifts, .world.level for
// where you are. Pure pointer + byte reads, no HL calls, so it's safe from the
// heartbeat timer rather than having to ride along inside a game-thread hook —
// which matters, because you can enter a rift (or start the meter mid-session)
// long before you hit anything. Reported on change only.
let lastRift = null;
let lastLevel = null;
function checkRift() {
    try {
        if (!localHero || localHero.isNull() || !OFF.GameLayer) return;
        const layer = localHero.add(OFF.Hero.layer).readPointer();
        if (!layer || layer.isNull()) return;
        // Zone identity AND the zone-change signal: layer.world.level names
        // the loaded level. This replaced Main.getMapId(), which turned out
        // to return the machine hostname. Everything here is plain pointer /
        // string-bytes reads, so it stays timer-safe.
        if (OFF.GameLayer.world != null && OFF.World
                && OFF.World.level != null) {
            const w = layer.add(OFF.GameLayer.world).readPointer();
            if (w && !w.isNull()) {
                const level = hlStr(w.add(OFF.World.level).readPointer());
                if (level && level !== lastLevel) {
                    const initial = lastLevel === null;
                    lastLevel = level;
                    const out = { kind: "zone", sig: level,
                                  initial: initial ? 1 : 0 };
                    // What the neighbouring fields actually hold, reported so
                    // their meaning gets measured from normal play — names
                    // lie in this game until they've been read live.
                    try {
                        out.name = hlStr(w.add(OFF.World.name).readPointer());
                        out.branch = hlStr(
                            w.add(OFF.World.branchName).readPointer());
                        out.world_map = w.add(OFF.World._isWorldMap).readU8();
                    } catch (e2) {}
                    if (!initial) {
                        // Everything a loading screen invalidates, moved here
                        // from the retired checkZone: the game rebuilds its
                        // whole UI, the loadout is re-replicated (re-baseline
                        // instead of reporting it all as pickups), and the
                        // camera object may not survive the layer rebuild.
                        resetWindows();
                        invReady = false;
                        camPtr = null;
                    }
                    send(out);
                }
            }
        }
        const state = layer.add(OFF.GameLayer.isRift).readU8() !== 0;
        if (state !== lastRift) {
            lastRift = state;
            send({ kind: "rift", state: state ? 1 : 0 });
        }
    } catch (e) {}
}

// ---- minimap world sweep ----
// Same reasoning as checkRift(): plain pointer and scalar reads off the layer,
// no HL calls, so it's safe from a timer rather than having to ride inside the
// damage hook. Measured at ~1ms for 300 entities, which is what makes running
// it several times a second reasonable at all.
//
// The layer keeps `units`, `interactibles` and `entities` built already, so
// this is three array walks rather than a search. Activities only appear in
// `entities`, which is why all three are read.
// Navigation markers — chests, orbs, obelisks, respawn points, activities and
// players — are sent from the WHOLE layer, at any distance, because that is
// what the compass is for: the thing you are walking to is by definition the
// thing that is far away, and a radius cull made it appear only once you were
// nearly there. Affordable because the layer is small: measured in a populated
// world zone, 252 entities total, of which ~90 were within the old 600u and the
// farthest was 3305u away. The walk costs 0-1ms either way — the arrays are
// already built, so distance was never what made this cheap.
//
// Foes keep a radius, and are the only category that does. They are the
// numerous class (95 of those 252), they are worthless on a compass that
// deliberately doesn't draw them, and the minimap cannot zoom past 600 —
// MINIMAP_RANGE_MAX is pinned to this number for exactly that reason, so the
// map's widest view and the last foe we send end at the same place. Raising
// this without raising that just sends foes nobody draws; raising that without
// raising this puts a ring of missing mobs around the edge of the map.
const SWEEP_RADIUS_FOE = 600;    // world units; MINIMAP_RANGE_MAX matches it
const SWEEP_MAX = 400;           // hard cap on entities reported, worst case
// Foes get their own slice of SWEEP_MAX. Without it, `units` being swept first
// means a crowded fight can spend the whole budget on mobs and push the chests
// and party members off the far end — which, now that those are unbounded in
// range, would empty the compass exactly when it's carrying the most.
const SWEEP_FOE_MAX = 150;
let foeCount = 0;                // reset per sweep, in sweepWorld()
// Foes this far above or below are dropped outright rather than sent and
// faded. In the vertical zones this is for, a mob two floors down is not
// something you can fight or avoid, and there can be a great many of them —
// so this also stops them crowding chests and obelisks out of SWEEP_MAX.
// Measured elevations: your own floor lands within ~12 units (slopes and
// ledges) and a genuinely different level at 150+, so anything in between
// separates the two. The overlay fades from 30 (MINIMAP_Z_FADE); between that
// and this, a foe is dimmed rather than hidden.
// Deliberately foes only: a chest or obelisk below you is still somewhere to
// head for, and dropping those would cost navigation rather than clean it up.
const SWEEP_Z_CULL = 60;
let worldTickMs = 150;           // replaced by the host's Refresh setting
let worldTimer = null;

function setWorldTick(ms) {
    ms = Math.max(30, Math.min(1000, ms | 0));
    if (ms === worldTickMs && worldTimer !== null) return;
    worldTickMs = ms;
    if (worldTimer !== null) clearInterval(worldTimer);
    worldTimer = setInterval(sweepWorld, worldTickMs);
    log("minimap sweep every " + worldTickMs + "ms");
}

// ---- the active camera ----
// The minimap turns with the camera rather than the character, which means
// knowing where the camera is pointing. Nothing hands out the camera object,
// so client.BaseCamera.postUpdate is hooked purely to keep `this` — it runs
// every frame, and being on the BASE class it captures whichever camera is
// currently driving the view (game, cinematic, character-edit).
let camPtr = null;
const CAM_CLASS = "client.GameCamera";

// ---- foe display names ----
// The nameplate name lives in the CDB, reached the same way skill names are:
// unit.inf -> texts -> name. That needs hl_obj_get_field, which is an HL call
// and so must not happen on the sweep's timer thread. The camera's postUpdate
// runs on the GAME thread every frame, so the sweep only queues an id and the
// lookup is drained there — a handful per frame, since it's once per foe TYPE
// and the cache serves every one of them after that.
const unitNameCache = {};       // kind -> display name ("" once tried and empty)
let unitNamePending = [];       // [{kind, ptr}] awaiting a game-thread lookup
const UNIT_NAME_PER_FRAME = 4;
const UNIT_NAME_QUEUE_MAX = 64;

function queueUnitName(kind, ptr) {
    if (!kind || kind in unitNameCache) return;
    if (unitNamePending.length >= UNIT_NAME_QUEUE_MAX) return;
    for (let i = 0; i < unitNamePending.length; i++)
        if (unitNamePending[i].kind === kind) return;
    unitNamePending.push({ kind: kind, ptr: ptr });
}

function drainUnitNames() {
    if (!unitNamePending.length || !hl_getField) return;
    for (let i = 0; i < UNIT_NAME_PER_FRAME && unitNamePending.length; i++) {
        const job = unitNamePending.shift();
        if (job.kind in unitNameCache) continue;
        let nm = "";
        try {
            const inf = job.ptr.add(OFF.Unit.inf).readPointer();
            const texts = getField(inf, "texts");
            nm = hlStr(getField(texts, "name")) || "";
        } catch (e) {}
        // Cached even when empty, so a foe with no CDB name isn't retried on
        // every frame it's on screen.
        unitNameCache[job.kind] = nm;
    }
}

function hookCamera(base) {
    const fi = DATA.cam_targets && DATA.cam_targets["client.BaseCamera.postUpdate"];
    if (fi === undefined) { log("!! camera target missing; minimap will not follow the view"); return; }
    try {
        Interceptor.attach(base.add(fi * 8).readPointer(), {
            onEnter: function (args) {
                // Hooked on the base class so it survives the game swapping
                // cameras, but only the gameplay camera is worth following —
                // a cinematic or character-edit camera points somewhere the
                // player isn't looking. Measured: in normal play only
                // GameCamera calls this, so the check costs a cached lookup.
                if (typeName(args[0]) === CAM_CLASS) camPtr = args[0];
                // Game thread: the only safe place for the CDB lookups the
                // sweep queues up — and for the getHero calls below, which are
                // HL calls like any other findex invocation.
                drainUnitNames();
                drainGatherKinds();
                drainGliderEquip();
                if (heroRefreshDue) { heroRefreshDue = false; refreshLocalHero(); }
            }
        });
    } catch (e) {
        log("!! camera hook failed (" + e + "); minimap will not follow the view");
    }
}

// ---- boss / elite healthbar ----
// ui.hud.BossesInfo.bossInfos is the game's own list of on-screen boss bars.
// Measured against a live King Ratsar pull and an elite:
//
//   * fetchBosses runs at a steady 2/s — a timer, not a per-frame call — so
//     this hook body can afford to walk the array rather than defer it.
//   * the array is not a fixed pool: it is empty with no bar and holds one
//     entry with a bar up, so its length alone answers "is a bar on screen".
//   * a bar comes up for ELITES too (a plain ent.Foe raised one), which is why
//     boss-only rules go through isBoss rather than through the bar.
//   * the bar tracks ENGAGEMENT, not existence — it dropped with the boss
//     alive at 22824 HP when the player walked off and the boss reset. That is
//     exactly the pull-start/pull-end boundary the meter wants.
//
// Kill vs disengage is decided from the last health seen while the bar was up:
// on the real kill the final sample read 0, on the walk-away it did not.
const bossClass = {};            // unit kind -> {boss, elite}, classified once
let bossBars = {};               // unit ptr string -> {kind, boss, elite, hp}
let bossLast = "";               // last state signature, to send only on change
let bossFnIsBoss = null, bossFnIsElite = null;

function bossUnitHealth(u) {
    try {
        if (!OFF.Unit || OFF.Unit.attr == null || !OFF.UnitAttributes) return null;
        const attr = u.add(OFF.Unit.attr).readPointer();
        if (!attr || attr.isNull() || attr.compare(ptr("0x10000")) <= 0) return null;
        return attr.add(OFF.UnitAttributes.health).readDouble();
    } catch (e) { return null; }
}

// Runs on the GAME thread (inside the fetchBosses hook), the only safe place
// for HL calls. Cached per unit KIND: a zone has few boss/elite types and the
// answer can't change for a given one.
function classifyBossUnit(u, kind) {
    let hit = bossClass[kind];
    if (hit) return hit;
    hit = { boss: false, elite: false };
    try { if (bossFnIsBoss) hit.boss = !!bossFnIsBoss(u); } catch (e) {}
    try { if (bossFnIsElite) hit.elite = !!bossFnIsElite(u); } catch (e) {}
    bossClass[kind] = hit;
    return hit;
}

function pollBossBars(bi) {
    if (!bi || bi.isNull() || !OFF.BossesInfo || !OFF.BossInfo || !OFF.ArrayObj)
        return;
    const A = OFF.ArrayObj;
    const now = {};
    try {
        const arr = bi.add(OFF.BossesInfo.bossInfos).readPointer();
        if (arr && !arr.isNull()) {
            const n = arr.add(A.length).readS32();
            if (n > 0 && n < 64) {
                const data = arr.add(A.array).readPointer();
                if (data && !data.isNull()) {
                    for (let i = 0; i < n; i++) {
                        let slot;
                        try { slot = data.add(A.data + i * 8).readPointer(); }
                        catch (x) { continue; }
                        if (!slot || slot.isNull() || slot.compare(ptr("0x10000")) <= 0)
                            continue;
                        if (!slot.add(OFF.BossInfo.active).readU8()) continue;
                        const u = slot.add(OFF.BossInfo.unit).readPointer();
                        if (!u || u.isNull() || u.compare(ptr("0x10000")) <= 0) continue;
                        const kind = hlStr(u.add(OFF.Unit.kind).readPointer());
                        if (!kind) continue;
                        const cls = classifyBossUnit(u, kind);
                        const hp = bossUnitHealth(u);
                        const key = u.toString();
                        const prev = bossBars[key];
                        now[key] = { kind: kind, boss: cls.boss, elite: cls.elite,
                                     // Keep the last non-null reading: at
                                     // teardown the unit may already be gone.
                                     hp: hp === null ? (prev ? prev.hp : null) : hp };
                    }
                }
            }
        }
    } catch (e) { return; }

    const up = [], down = [];
    for (const k in now) if (!(k in bossBars)) up.push(now[k]);
    for (const k in bossBars) {
        if (k in now) continue;
        const b = bossBars[k];
        // hp === null means we never got a reading; don't claim a kill.
        down.push({ kind: b.kind, boss: b.boss, elite: b.elite,
                    killed: b.hp !== null && b.hp <= 0 });
    }
    bossBars = now;

    let anyBoss = false, anyElite = false, count = 0;
    for (const k in now) {
        count++;
        if (now[k].boss) anyBoss = true;
        if (now[k].elite) anyElite = true;
    }
    // Only talk when something changed. At 2/s an unconditional send would be
    // 2 messages a second forever, for a state that changes twice a pull.
    const sig = count + "|" + anyBoss + "|" + anyElite;
    if (!up.length && !down.length && sig === bossLast) return;
    bossLast = sig;
    send({ kind: "bossbar", n: count, boss: anyBoss, elite: anyElite,
           up: up, down: down });
}

function hookBossBar(base) {
    const fi = DATA.boss_targets && DATA.boss_targets["ui.hud.BossesInfo.fetchBosses"];
    if (fi == null) {
        log("!! boss target missing (ui.hud.BossesInfo.fetchBosses); boss-bar "
            + "detection disabled — re-run hltools/build_targets.py");
        return;
    }
    if (!OFF.BossesInfo || !OFF.BossInfo) {
        log("!! boss offsets missing (BossesInfo/BossInfo); boss-bar detection "
            + "disabled. The offsets file predates this build — delete "
            + "analysis_out and restart to regenerate it.");
        return;
    }
    const fns = DATA.boss_fns || {};
    function nf(nm) {
        const f = fns[nm];
        if (f == null) return null;
        try {
            return new NativeFunction(base.add(f * 8).readPointer(),
                                      "uint8", ["pointer"]);
        } catch (e) { return null; }
    }
    bossFnIsBoss = nf("ent.Unit.isBoss");
    bossFnIsElite = nf("ent.Unit.isElite");
    if (!bossFnIsBoss)
        log("!! ent.Unit.isBoss unavailable; every bar will count as an elite "
            + "and boss-only rules will never fire");
    try {
        Interceptor.attach(base.add(fi * 8).readPointer(), {
            onEnter: function () { pollBossBars(this.context.rcx); }
        });
        log("boss bar tracking active");
    } catch (e) {
        log("!! boss bar hook failed (" + e + ")");
    }
}

function cameraDirection() {
    if (!camPtr || camPtr.isNull() || !OFF.Camera) return null;
    // Re-check the type on every read. A zone change (which is also what a
    // rift entry is) can retire the camera object, and HL will happily reuse
    // that memory for something else — at which point curDirection is whatever
    // the new occupant keeps at that offset, and the map swings to a heading
    // out of nowhere. Cheap: typeName is cached by type pointer.
    try {
        if (typeName(camPtr) !== CAM_CLASS) { camPtr = null; return null; }
        return camPtr.add(OFF.Camera.curDirection).readDouble();
    } catch (e) { camPtr = null; return null; }
}

// What we draw, keyed by runtime class name. An allowlist rather than "whatever
// is in the array": `interactibles` is ~60% generic ent.Element scenery, which
// would bury the things worth seeing.
const SWEEP_CLASS = {
    "ent.Hero": "hero",
    "ent.Foe": "foe",
    "ent.interactible.Chest": "chest",
    // NOT an orb. An "instance orb" is the entrance to an instance — the
    // dungeon portal you press F at — and drawing it as a collectible sent
    // people to pick up a doorway. The collectibles are handled below.
    "ent.interactible.Obelisk": "obelisk",
    "ent.interactible.RespawnPoint": "respawn",
};

// The collectible orbs are NOT ent.interactible.InstanceOrb — that class
// exists but isn't what's scattered around the world. They're plain
// ent.Element with an id like "RedOrb_World_140", which the allowlist
// otherwise skips as scenery, since most ent.Element is exactly that.
const ORB_KIND = /orb/i;
// ...and a real one is Enabled. The dungeon entrance reads "None" here, which
// is what gave it away — so the state is checked as well as the name rather
// than trusting either on its own.
const ORB_STATE = "Enabled";

// Soulstones are the same shape of thing: plain ent.Element, `kind` like
// "Soulstone_Demon_5". Measured on a live one — stateId "None",
// currentVisualState null, no script, and radius/hitRadius/height all zero, so
// the layer treats it as scenery and nothing here can be filtered on state.
// The name is all there is to go on, which is why no state check follows it.
const SOULSTONE_KIND = /soulstone/i;

// Ore and herb nodes are all ONE class, unlike the orbs. Not in SWEEP_CLASS
// because one class maps to TWO categories: the split is the CDB row's
// texts.type ("Ore" | "Plant"), which needs the game thread — so the sweep
// reads Element.kind (a plain read) and classifies through this cache, queueing
// unresolved kinds the same way foe nameplates are. An entry of "" means the
// CDB answered something other than Ore/Plant; the node stays hidden and the
// oddity is logged once rather than guessed at.
// The kind is the ELEMENT id ("CopperOre_Small_Generic", "R2Plant2_Small_3"),
// not the Gatherable row id — placements alias freely, which is why the cache
// is per element-kind and the answer comes from gatherInf, not string-matching.
const GATHER_CLASS = "ent.interactible.Gatherable";
const gatherKindCache = {};    // Element kind -> {c, n} | "" once resolved
let gatherPending = [];        // [{kind, ptr}] awaiting a game-thread lookup
const GATHER_QUEUE_MAX = 32;

function queueGatherKind(kind, ptr) {
    if (!kind || kind in gatherKindCache) return;
    if (gatherPending.length >= GATHER_QUEUE_MAX) return;
    for (let i = 0; i < gatherPending.length; i++)
        if (gatherPending[i].kind === kind) return;
    gatherPending.push({ kind: kind, ptr: ptr });
}

function drainGatherKinds() {
    if (!gatherPending.length || !hl_getField) return;
    for (let i = 0; i < 2 && gatherPending.length; i++) {
        const job = gatherPending.shift();
        if (job.kind in gatherKindCache) continue;
        let out = "";
        try {
            const inf = job.ptr.add(OFF.Gatherable.gatherInf).readPointer();
            const texts = getField(inf, "texts");
            const ty = hlStr(getField(texts, "type"));
            const nm = hlStr(getField(texts, "name")) || "";
            // The Gatherable ROW id ("Ore_Tin_Large", "Tungstene") — the
            // stable material identity the overlay styles by. Element kinds
            // won't do: placements alias ("R2Plant2_Small_3" is a Lavendula).
            const rid = hlStr(getField(inf, "id")) || "";
            if (ty === "Ore") out = { c: "ore", n: nm, g: rid };
            else if (ty === "Plant") out = { c: "herb", n: nm, g: rid };
            else log("gatherable " + job.kind + ": CDB texts.type="
                     + JSON.stringify(ty) + " is not Ore/Plant — hidden");
        } catch (e) {}
        gatherKindCache[job.kind] = out;
    }
}

// ent.Element.stateId, measured on the live game:
//   chests  Closed | Locked        obelisks  Closed        orbs  Enabled
// A state on this list means the thing is spent and not worth drawing. Kept as
// a list rather than "anything but the known-good value", so an unfamiliar
// state still shows on the map instead of silently vanishing from it.
const SPENT_STATES = { "Opened": 1, "Disabled": 1, "Collected": 1,
                       "Used": 1, "Empty": 1, "Done": 1 };

// Per-category overrides on stateId. Empty, and the entry that used to be here
// is worth keeping as a warning:
//
//   chest: { "Closed": 1 }
//
// went in because looted chests kept showing and read "Closed". They do — but
// so does every chest that has never been opened, and filtering on it hid
// nearly every chest in the world. A chest's stateId does not move when you
// loot it. Measured on ONE chest, before and after pressing F:
//
//   before   stateId Closed   currentVisualState Closed
//   after    stateId Closed   currentVisualState Opened
//
// So the visual state is the signal, and stateId only ever says whether the
// thing needs a key ("Locked").
const SPENT_BY_CAT = {};

// Categories whose currentVisualState is worth believing. It is a RENDERING
// state, so it means different things to different objects, and the general
// rule remains "don't trust it" — an obelisk and a respawn point both read
// "Opened" while plainly standing there, and filtering those on it emptied two
// whole categories once already.
//
// Chests earn their place here by the measurement above. Orbs earned theirs the
// same way: a pickup flips the visual to "Disabled" while stateId stays
// "Enabled". Nothing goes on this list without watching the field change.
const VISUAL_SPENT_CATS = { orb: 1, chest: 1 };

// Runtime type names are resolved through typeName(), which caches by type
// pointer — a zone holds hundreds of entities but only a couple of dozen
// distinct classes, so classification is a map hit after the first of each.
function sweepArray(arrPtr, out, me, isEntities, seen) {
    const A = OFF.ArrayObj;
    if (!arrPtr || arrPtr.isNull()) return;
    const n = arrPtr.add(A.length).readS32();
    if (n <= 0 || n > 20000) return;              // never trust a raw length
    const data = arrPtr.add(A.array).readPointer();
    if (data.isNull()) return;
    for (let i = 0; i < n && out.length < SWEEP_MAX; i++) {
        let e;
        try { e = data.add(A.data + i * 8).readPointer(); } catch (x) { continue; }
        if (!e || e.isNull() || e.compare(ptr("0x10000")) <= 0) continue;
        // The three arrays overlap — `entities` also holds what's in `units`
        // and `interactibles`, so without this every player and chest is
        // reported twice and the minimap draws each of them on top of itself.
        const id = e.toString();
        if (seen[id]) continue;
        seen[id] = 1;
        const cls = typeName(e);
        if (!cls) continue;
        let cat = SWEEP_CLASS[cls];
        // Activities are a family (Ascension, WorldCamp, WorldElite, ChestOrb,
        // TimerCollectRun, ...) rather than one class, so they're matched by
        // prefix and only in `entities`, where they actually live.
        if (!cat && isEntities && cls.lastIndexOf("st.activity.", 0) === 0)
            cat = "activity";
        let elemKind = null;
        if (!cat && cls === "ent.Element" && OFF.Element) {
            elemKind = hlStr(e.add(OFF.Element.kind).readPointer());
            if (elemKind && ORB_KIND.test(elemKind)) {
                const est = hlStr(e.add(OFF.Element.stateId).readPointer());
                if (est === ORB_STATE) cat = "orb";
            } else if (elemKind && SOULSTONE_KIND.test(elemKind)) {
                cat = "soulstone";
            }
        }
        let gatherInfo = null;
        if (!cat && cls === GATHER_CLASS && OFF.Gatherable) {
            try {
                // "Mineable only" is the category's contract, and hitPoints is
                // the mineable signal (measured 2026-08-01): each gather tick
                // steps it down, 0 means depleted-awaiting-respawn — the node
                // STAYS in the arrays with enabled 0 and stateId still "None",
                // so without this check the map would keep a marker on every
                // stump. The respawn replicates hp back to max and the marker
                // simply returns.
                if (e.add(OFF.Gatherable.hitPoints).readDouble() <= 0) continue;
                const gk = hlStr(e.add(OFF.Element.kind).readPointer());
                gatherInfo = gk ? gatherKindCache[gk] : "";
                // Unresolved kind: queue it and skip this tick — the CDB
                // answer lands within a frame or two, once per kind ever.
                if (gatherInfo === undefined) { queueGatherKind(gk, e); continue; }
                if (!gatherInfo) continue;      // resolved to not-a-node; logged once
                cat = gatherInfo.c;
            } catch (x) { continue; }
        }
        if (!cat) continue;
        try {
            if (e.add(OFF.State.removed).readU8()) continue;   // despawned
            // Pets and summons are ent.Foe like any mob — the only thing that
            // separates them is an owner. Dropped here rather than sent and
            // filtered, since nothing downstream wants them.
            if (cat === "foe" && OFF.Foe) {
                const owner = e.add(OFF.Foe.summonOwner).readPointer();
                if (owner && !owner.isNull()) continue;
            }
            const x = e.add(OFF.Entity.posx).readDouble();
            const y = e.add(OFF.Entity.posy).readDouble();
            const z = e.add(OFF.Entity.posz).readDouble();
            if (cat === "foe") {
                const dx = x - me.x, dy = y - me.y;
                if (dx * dx + dy * dy > SWEEP_RADIUS_FOE * SWEEP_RADIUS_FOE) continue;
                if (Math.abs(z - me.z) > SWEEP_Z_CULL) continue;
                if (foeCount >= SWEEP_FOE_MAX) continue;
                foeCount++;
            }
            // Foes are culled here rather than overlay-side so the payload
            // stays small regardless of how crowded the zone is. Rounded for
            // the same reason: sub-unit precision is invisible on a minimap —
            // except on things that MOVE, which get a decimal below, because
            // whole units make a walking marker step rather than glide.
            // z rides along so the overlay can fade things on a different
            // floor: a mob directly below you is not the same news as one you
            // can walk to, and on the flat map they look identical.
            // One decimal for players and mobs, whole units for the scenery.
            // A static marker's rounding is a fixed offset you never see; a
            // moving one's changes every frame, which is the same jitter the
            // player's own position had.
            const q = (cat === "hero" || cat === "foe") ? 10 : 1;
            const ent = { c: cat, x: Math.round(x * q) / q,
                          y: Math.round(y * q) / q, z: Math.round(z * q) / q };
            if (cat === "hero") {
                // Named so the overlay can ring party members. Party membership
                // is matched by name against the group roster the meter already
                // reads — one source of truth, not two.
                const nm = hlStr(e.add(OFF.Hero.name).readPointer());
                if (nm) ent.n = nm;
                // A hero's Unit.kind is its class — "Warrior", "Mage". Same
                // field that gives a foe its creature id, which is why it
                // costs nothing extra to read.
                if (OFF.Unit) {
                    const cl = hlStr(e.add(OFF.Unit.kind).readPointer());
                    if (cl) ent.k = cl;
                }
                // ...and their facing, so they can be drawn as chevrons that
                // point where they're going rather than as anonymous dots.
                ent.r = Math.round(
                    e.add(OFF.Entity.rotationZ).readDouble() * 1000) / 1000;
            } else if (cat === "foe") {
                // Send the id always and the display name once resolved, so
                // the overlay has something to show on the first frame a foe
                // appears rather than waiting for the lookup.
                const kind = hlStr(e.add(OFF.Unit.kind).readPointer());
                if (kind) {
                    ent.k = kind;
                    const nm = unitNameCache[kind];
                    if (nm) ent.n = nm;
                    else queueUnitName(kind, e);
                }
            } else if (cat !== "activity") {
                try { ent.e = e.add(OFF.Interactible.enabled).readU8() ? 1 : 0; }
                catch (x) {}
                if (OFF.Element) {
                    // The state machine is what actually says whether a chest
                    // is still shut or an orb still there. Sent as well as
                    // filtered on, so the hover line can show it and an
                    // unfamiliar value is visible rather than invisible.
                    const st = hlStr(e.add(OFF.Element.stateId).readPointer());
                    const vis = hlStr(
                        e.add(OFF.Element.currentVisualState).readPointer());
                    // Two filters, and which field each one reads is the whole
                    // subject of the notes on SPENT_BY_CAT and
                    // VISUAL_SPENT_CATS above. Short version: stateId for
                    // everything, plus the visual state for the two categories
                    // measured to move it — chests and orbs.
                    const catSpent = SPENT_BY_CAT[cat];
                    if (SPENT_STATES[st] || (catSpent && catSpent[st])) continue;
                    if (VISUAL_SPENT_CATS[cat] && SPENT_STATES[vis]) continue;
                    if (st) ent.s = st;
                    if (vis && vis !== st) ent.v = vis;
                    const k = elemKind ||
                        hlStr(e.add(OFF.Element.kind).readPointer());
                    if (k) ent.k = k;
                }
                // Node display name off the CDB cache ("Copper Lode"), so the
                // hover line names the thing rather than its placement id —
                // and the row id, which is what the overlay picks a material
                // colour (and the rare halo) by.
                if (gatherInfo && gatherInfo.n) ent.n = gatherInfo.n;
                if (gatherInfo && gatherInfo.g) ent.g = gatherInfo.g;
            }
            out.push(ent);
        } catch (x) {}
    }
}

function sweepWorld() {
    try {
        if (!localHero || localHero.isNull() || !OFF.Entity || !OFF.ArrayObj) return;
        const layer = localHero.add(OFF.Hero.layer).readPointer();
        if (!layer || layer.isNull()) return;
        const me = {
            x: localHero.add(OFF.Entity.posx).readDouble(),
            y: localHero.add(OFF.Entity.posy).readDouble(),
            z: localHero.add(OFF.Entity.posz).readDouble(),
            r: localHero.add(OFF.Entity.rotationZ).readDouble(),   // radians
        };
        const cam = cameraDirection();
        const out = [], seen = {};
        foeCount = 0;
        const G = OFF.GameLayer;
        // Order matters with the dedupe: the narrow, purpose-built lists first,
        // then `entities` last to pick up activities and anything the other two
        // don't carry.
        sweepArray(layer.add(G.units).readPointer(), out, me, false, seen);
        sweepArray(layer.add(G.interactibles).readPointer(), out, me, false, seen);
        sweepArray(layer.add(G.entities).readPointer(), out, me, true, seen);
        // Two decimals on YOUR position, where every other coordinate gets
        // whole units. It matters here and nowhere else: a static marker's
        // rounded position never changes, but yours does, so rounding it put a
        // half-unit of error into every relative offset and flipped its sign
        // each time you crossed a unit boundary. On a 405px map covering 350
        // units that is about a pixel of side-to-side shake, most obvious when
        // you walk straight at something — the one case where the marker is
        // supposed to hold still. One object per frame, so the precision is
        // free.
        send({ kind: "world", me: { x: Math.round(me.x * 100) / 100,
                                    y: Math.round(me.y * 100) / 100,
                                    z: Math.round(me.z * 100) / 100,
                                    r: Math.round(me.r * 1000) / 1000,
                                    // Camera yaw, or null until the camera has
                                    // been seen. The overlay falls back to r.
                                    c: cam === null ? null
                                       : Math.round(cam * 1000) / 1000 },
               ents: out });
    } catch (e) {}
}

function resetWindows() {
    for (const nm in winOpenCount) send({ kind: "window", name: nm, open: 0 });
    for (const k in winClassOf) delete winClassOf[k];
    for (const k in winOpenCount) delete winOpenCount[k];
    // A bar that was up when the agent went away must not leave the compass
    // hidden forever. No up/down events: the pull isn't ending, we're just
    // no longer able to see it, and a phantom "boss died" fanfare on unload
    // would be worse than saying nothing.
    if (Object.keys(bossBars).length) send({ kind: "bossbar", n: 0, boss: false,
                                             elite: false, up: [], down: [] });
    bossBars = {};
    bossLast = "";
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
        for (const n of ["texts", "name", "type", "id"]) fieldHash[n] = hl_hashUtf8(Memory.allocUtf8String(n));
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

// ---- the shard roster (Social tab) ----
// st.GameLayer.players is EVERY player the client holds state for, not just
// the ones streamed in around you — which is the whole point, since `units`
// (what the minimap sweeps) only ever contains your neighbours.
//
// Each entry carries the player's Steam account id in `uid`, as
// "S" + the id's bytes in LITTLE-ENDIAN hex with trailing zero bytes trimmed.
// It is sent on as-is and converted host-side; doing the arithmetic here would
// put a second implementation of a fiddly byte-order rule in a second language.
//
// The class lives on the player's ent.Hero, not on st.player.HeroData — that
// object is null client-side for everyone, including you.
//
// Plain pointer reads throughout, so this is safe on a timer thread: no HL
// call, no allocation, nothing that needs the GC lock.
const SHARD_MAX = 256;          // a sane ceiling on a corrupt length read
let shardTimer = null;
let shardSig = "";              // last payload signature, to skip idle resends

function readShard(hero) {
    const out = [];
    const P = OFF.Player, H = OFF.Hero, G = OFF.GameLayer;
    if (!P || !H || !G || P.uid == null || G.players == null) return out;
    const layer = hero.add(H.layer).readPointer();
    if (!layer || layer.isNull()) return out;
    const proxy = layer.add(G.players).readPointer();
    if (!proxy || proxy.isNull()) return out;
    const arrDyn = proxy.add(OFF.ArrayProxyData.array).readPointer();
    if (!arrDyn || arrDyn.isNull()) return out;
    const arrObj = arrDyn.add(OFF.ArrayDyn.array).readPointer();
    if (!arrObj || arrObj.isNull()) return out;
    const length = arrObj.add(OFF.ArrayObj.length).readS32();
    const varr = arrObj.add(OFF.ArrayObj.array).readPointer();
    if (length < 0 || length > SHARD_MAX || !varr || varr.isNull()) return out;
    for (let i = 0; i < length; i++) {
        try {
            const p = varr.add(24 + i * 8).readPointer();
            if (!p || p.isNull()) continue;
            const nm = hlStr(p.add(P.name).readPointer());
            if (!nm) continue;                  // a slot mid-population
            const row = { n: nm, uid: hlStr(p.add(P.uid).readPointer()) };
            try { row.me = p.add(P.isMe).readU8() !== 0; } catch (e) {}
            // The hero entity is absent for a player who is on the layer but
            // not yet built — a real state, so the row still ships, just
            // without a class. The tab shows "-" rather than dropping them.
            const h = p.add(P.hero).readPointer();
            if (h && !h.isNull()) {
                if (H.kind != null) row.k = hlStr(h.add(H.kind).readPointer());
                if (H.level != null) row.lvl = h.add(H.level).readS32();
            }
            out.push(row);
        } catch (e) {}
    }
    return out;
}

function sweepShard() {
    try {
        if (!localHero || localHero.isNull()) return;
        const list = readShard(localHero);
        if (!list.length) return;
        // A hub roster is re-read every couple of seconds but changes rarely;
        // resending an identical list would repaint the tab under the cursor
        // for nothing. Level is in the signature so a ding still lands.
        const sig = list.map(function (r) {
            return r.n + "|" + (r.uid || "") + "|" + (r.k || "") + "|" + (r.lvl || "");
        }).sort().join(";");
        if (sig === shardSig) return;
        shardSig = sig;
        send({ kind: "shard", list: list });
    } catch (e) {}
}

// ---- legendary pickup cue ----
// Plain pointer reads only, so this is safe on the sweep's timer thread.
//
// st.Inventory.content is an ArrayObj whose entries are NOT items: each is a
// standalone hl vvirtual (kind 15, value/next both NULL) carrying inline
// fields {count:Int, item:st.Item}. Reading an entry as an st.Item does not
// throw, it just yields garbage — a probe round decoded a shader source path
// as an item `kind` that way. So the `item` field is read out of the virtual's
// own field table, by NAME (index 0 is `count`).
//
//   hl_type         { kind@0, union@8, vobj_proto@16 }
//   hl_type_virtual { fields@0, nfields@8, dataSize@12, indexes@16 }
//   hl_obj_field    { name@0, type@8, hashed@16 }   — 24 bytes each
//
// and for a standalone virtual the pointer array at v+24 holds each field's
// storage ADDRESS, so a field value is a double deref.
const HVIRTUAL = 15;
const virtFieldIdx = {};        // virtual type ptr -> { field name -> index }

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

function slotItem(p) {
    try {
        if (!p || p.isNull()) return null;
        const t = p.readPointer();
        if (t.readU32() !== HVIRTUAL) return null;
        const ii = virtualFieldIndex(t, "item");
        if (ii < 0) return null;
        const st = p.add(24 + ii * 8).readPointer();
        if (!st || st.isNull()) return null;
        const it = st.readPointer();
        return (it && !it.isNull() && it.compare(ptr("0x10000")) > 0) ? it : null;
    } catch (e) { return null; }
}

// uid churns on every container move, so it is a slot discriminator only —
// never an identity. `kind` is what the pickup rule actually compares.
function itemInfo(it) {
    const cls = typeName(it);
    if (!cls || (cls.lastIndexOf("st.Item", 0) !== 0
                 && cls.lastIndexOf("st.item.", 0) !== 0)) return null;
    const out = { cls: cls, kind: null, rarity: null, level: null, uid: null };
    try { out.uid = it.add(OFF.Item.uid).readS64().toString(); } catch (e) {}
    try { out.kind = hlStr(it.add(OFF.Item.kind).readPointer()); } catch (e) {}
    // rarity is declared only on st.item.Weapon; at any other class offset 160
    // is past the end of the object.
    if (cls === "st.item.Weapon" && OFF.Weapon) {
        try { out.rarity = hlStr(it.add(OFF.Weapon.rarity).readPointer()); } catch (e) {}
        try { out.level = it.add(OFF.Weapon.level).readS32(); } catch (e) {}
    }
    return out;
}

// The count key is kind + rarity, NOT kind alone, and that is the whole cue.
// `kind` is the template (`Mace_Benediction`), rarity varies per copy, and a
// player accumulates several copies of a kind over a session — the live log has
// the same Mace_Benediction arriving twice hours apart. Keyed by kind alone the
// counts still moved correctly, but the payload didn't: the sweep described the
// gain with the FIRST item of that kind it happened to walk past, which is
// normally the copy already in the bag. Loot a Legendary of a kind you already
// own and the event went out saying "Epic", so the cue never fired.
//
// Rarity is safe in a key where uid is not: it is a property of the copy, and
// it does not churn when the copy moves between containers. So this keeps the
// uid-churn immunity described below intact and only sharpens the payload.
const invKey = function (inf) { return inf.kind + "|" + (inf.rarity || ""); };

let invSeen = null;             // kind|rarity -> count, inventory + equipment
let invReady = false;           // first sweep only baselines, never fires

// Returns false if the container could not be read. That distinction matters:
// a failed read looks exactly like an empty bag, and treating one as the other
// would drop the baseline to nothing and then report every item the hero owns
// as a fresh pickup on the next good sweep.
function readContainerKinds(invPtr, into, byKey) {
    if (!invPtr || invPtr.isNull()) return false;
    let arr;
    try { arr = invPtr.add(OFF.Inventory.content).readPointer(); }
    catch (e) { return false; }
    if (!arr || arr.isNull()) return false;
    const A = OFF.ArrayObj;
    let n, data;
    try {
        n = arr.add(A.length).readS32();
        data = arr.add(A.array).readPointer();
    } catch (e) { return false; }
    if (n < 0 || n > 4096 || data.isNull()) return false;
    if (n === 0) return true;                    // genuinely empty, and read fine
    for (let i = 0; i < n; i++) {
        let raw;
        try { raw = data.add(A.data + i * 8).readPointer(); } catch (e) { continue; }
        if (!raw || raw.isNull() || raw.compare(ptr("0x10000")) <= 0) continue;
        const it = slotItem(raw);
        if (!it) continue;
        const inf = itemInfo(it);
        if (!inf || !inf.kind) continue;
        const key = invKey(inf);
        into[key] = (into[key] || 0) + 1;
        // Every item under one key shares kind AND rarity, so first-wins is no
        // longer a coin toss over which copy describes the gain.
        if (!(key in byKey)) byKey[key] = inf;
    }
    return true;
}

// Counting by KIND (+ rarity, see invKey) across BOTH containers is what makes
// this survive the uid churn: unequipping a weapon and re-equipping it moves it
// from equipment to inventory and back, minting a new uid each time, but the
// number of that kind-and-rarity the hero is carrying never changes. Only a
// genuine gain moves the count up — which is exactly the event worth a cue.
function sweepInventory() {
    try {
        if (!localHero || localHero.isNull()
            || !OFF.Loadout || !OFF.Inventory || !OFF.Item || !OFF.ArrayObj)
            return;
        const loadout = localHero.add(OFF.Hero.loadout).readPointer();
        if (!loadout || loadout.isNull()) return;
        const now = {}, info = {};
        // BOTH containers or neither: a half-read snapshot loses whatever the
        // failed side held, and every one of those items then reads as a gain
        // the moment it comes back.
        const okInv = readContainerKinds(
            loadout.add(OFF.Loadout.inventory).readPointer(), now, info);
        const okEq = readContainerKinds(
            loadout.add(OFF.Loadout.equipment).readPointer(), now, info);
        if (!okInv || !okEq) return;
        if (!invReady) {
            // A zone change or a respawn re-reads the whole loadout; without
            // this the first sweep after one would report every item the hero
            // owns as a fresh pickup.
            invSeen = now; invReady = true; return;
        }
        for (const key in now) {
            const gained = now[key] - (invSeen[key] || 0);
            if (gained <= 0) continue;
            const inf = info[key];
            if (!inf) continue;
            // inf.kind, not `key` — the key carries the rarity suffix and is
            // an internal bookkeeping string, never a name to show anyone.
            send({ kind: "pickup", item: inf.kind, cls: inf.cls,
                   rarity: inf.rarity, level: inf.level, count: gained });
        }
        invSeen = now;
    } catch (e) {}
}

// Set by a timer, consumed on the game thread by the camera hook. The lookup
// itself must NOT run on the timer: getHero is an HL call, and an HL call off
// the game thread kills the game with "Can't lock GC in unregistered thread".
// This shipped calling refreshLocalHero() straight from a setInterval, which
// is the same pattern that killed a probe on its ~10th tick — it survived only
// because the window is narrow, not because it was safe.
let heroRefreshDue = false;

function refreshLocalHero() {
    for (const f of getHeroFns) {
        try {
            const h = new NativeFunction(f.addr, "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") {
                // A different hero object means a different loadout, so the
                // pickup baseline is meaningless — rebuild it rather than
                // announcing the new hero's whole bag as picked up.
                if (!localHero || !localHero.equals(h)) invReady = false;
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

// ---------------------------------------------------------------------------
// Random favorite mount (measured 2026-08-01, mount_probe.js + swap probe)
// ---------------------------------------------------------------------------
// ent.Hero.setMount(id) is the LOCAL summon request; everything downstream
// (setMount__impl, setupMount, the replicated set_mountId) inherits its
// argument, so replacing args[1] in place is the whole feature. The
// replacement String pointers come from the account collection's own mounts
// array — GC-rooted by the collection, and HL's GC doesn't move objects, so
// no allocation is ever needed. Nothing is called; the hook body is plain
// reads plus one pointer swap.
//
// Two rules paid for in probe rounds:
//   * NO cached hero pointers. Round 1 gated on a once-latched localHero and
//     logged nothing — a re-replicated hero (any zone change) stales the
//     pointer silently. Every check walks the hooked object itself.
//   * The local test is this -> player -> isMe, plain reads.
// mode "random" rolls per summon; "cycle" walks the favorites in the order
// the host sent them, one step per summon. cycleIdx is session state and
// resets whenever the config changes — a rotation that survived an edited
// list would start mid-way through a different sequence.
let mountCfg = { enabled: false, favorites: [], mode: "random", cycleIdx: 0 };
let lastMountsSig = null;

function mountOffsetsOk() {
    return OFF.Player && OFF.Player.accountProgress !== undefined
        && OFF.AccountProgress && OFF.Collection
        && OFF.ArrayProxyData && OFF.ArrayDyn;
}

function heroIsMe(hero) {
    try {
        const player = hero.add(OFF.Hero.player).readPointer();
        if (!player || player.isNull()) return false;
        return player.add(OFF.Player.isMe).readU8() === 1;
    } catch (e) { return false; }
}

// {kind -> String ptr} for the hero's unlocked mounts; empty on any failure.
function readMountKinds(hero) {
    const out = {};
    if (!mountOffsetsOk()) return out;
    try {
        const player = hero.add(OFF.Hero.player).readPointer();
        const acct = player.add(OFF.Player.accountProgress).readPointer();
        const coll = acct.add(OFF.AccountProgress.collection).readPointer();
        const proxy = coll.add(OFF.Collection.mounts).readPointer();
        const dyn = proxy.add(OFF.ArrayProxyData.array).readPointer();
        const inner = dyn.add(OFF.ArrayDyn.array).readPointer();
        if (typeName(inner) !== "hl.types.ArrayObj") return out;
        const n = inner.add(OFF.ArrayObj.length).readS32();
        if (n < 0 || n > 4096) return out;
        const data = inner.add(OFF.ArrayObj.array).readPointer();
        for (let i = 0; i < n; i++) {
            const s = data.add(OFF.ArrayObj.data + i * 8).readPointer();
            const k = hlStr(s);
            if (k) out[k] = s;
        }
    } catch (e) {}
    return out;
}

function hookMountSwap(base) {
    const fi = (DATA.mount_targets || {})["ent.Hero.setMount"];
    if (fi === undefined) {
        log("!! ent.Hero.setMount missing from resolver data — random mount "
            + "disabled. The data file is older than this build; delete "
            + "analysis_out and restart.");
        return;
    }
    if (!mountOffsetsOk()) {
        log("!! mount offsets missing — random mount disabled. The offsets "
            + "file is older than this build; delete analysis_out and restart.");
        return;
    }
    Interceptor.attach(base.add(fi * 8).readPointer(), {
        onEnter: function (args) {
            try {
                if (!mountCfg.enabled || !mountCfg.favorites.length) return;
                const orig = hlStr(args[1]);
                if (orig === null) return;         // dismount — never touched
                if (!heroIsMe(args[0])) return;    // replication traffic
                const pool = readMountKinds(args[0]);
                const owned = mountCfg.favorites.filter(
                    function (k) { return k in pool; });
                if (!owned.length) return;
                let pick;
                if (mountCfg.mode === "cycle") {
                    // Strict rotation, one step per summon, in the host's
                    // list order. The equipped mount is not excluded — a
                    // cycle that skipped it would be a different sequence
                    // than the checklist reads.
                    pick = owned[mountCfg.cycleIdx % owned.length];
                    mountCfg.cycleIdx = (mountCfg.cycleIdx + 1) % owned.length;
                } else {
                    // Random: prefer a different one than what was asked
                    // for, so the swap always reads as random, but a single-
                    // favorite list still wins over the selection.
                    let picks = owned.filter(
                        function (k) { return k !== orig; });
                    if (!picks.length) picks = owned;
                    pick = picks[Math.floor(Math.random() * picks.length)];
                }
                args[1] = pool[pick];
                log("mount swap (" + mountCfg.mode + "): " + orig + " -> "
                    + pick);
            } catch (e) {}
        }
    });
}

// ---------------------------------------------------------------------------
// Random favorite glider (measured 2026-08-02, glider probes 1-4)
// ---------------------------------------------------------------------------
// Gliders have NO setMount-style summon to arg-swap: the deploy chain is
// bool-only (ent.Hero.toggleGlide -> UnitView.toggleGlider) and the model is
// pre-spawned at equip time. Brudr chose the real-re-equip design instead:
// at each glide end this performs the UI's own persistent equip —
// st.player.Collection.equipItem(kind) — with a random favorite, so the
// change replicates and other players see it. This is the meter's one
// deliberate state-changing call; it is off until the host enables it.
//
// The call takes only the kind String, which the collection walk already
// provides. Probe round 3 spent itself on the theory that args[2] was the
// item's CDB row and built an index walk to produce one; round 3d's type
// dump refuted it — args[2] is kind=10 (HFUN), a closure the UI allocates
// per click (a different pointer for the same kind each time), i.e. the
// optional result callback. args[3] (65535) is caller register leftover,
// not a parameter — round 3c saw args[4] mirror args[2] the same way. So
// null is passed for the callback and nothing else is needed: no CDB index,
// no heap scan (an early background-thread scan raced the collection UI's
// allocations and crashed the game — see TESTING.md).
let gliderCfg = { enabled: false, favorites: [], mode: "random", cycleIdx: 0 };
let lastGlidersSig = null;
let gliderEquipAddr = null;       // Collection.equipItem entry, cached once
let lastGliderKind = null;        // last kind seen through equipItem
let pendingGliderEquip = null;    // {kind, str, hero} set at glide end
let lastGliderEquipMs = 0;
let localGlideUp = false;

function readGliderKinds(hero) {
    const out = {};
    if (!mountOffsetsOk() || OFF.Collection.gliders === undefined) return out;
    try {
        const player = hero.add(OFF.Hero.player).readPointer();
        const acct = player.add(OFF.Player.accountProgress).readPointer();
        const coll = acct.add(OFF.AccountProgress.collection).readPointer();
        const proxy = coll.add(OFF.Collection.gliders).readPointer();
        const dyn = proxy.add(OFF.ArrayProxyData.array).readPointer();
        const inner = dyn.add(OFF.ArrayDyn.array).readPointer();
        if (typeName(inner) !== "hl.types.ArrayObj") return out;
        const n = inner.add(OFF.ArrayObj.length).readS32();
        if (n < 0 || n > 4096) return out;
        const data = inner.add(OFF.ArrayObj.array).readPointer();
        for (let i = 0; i < n; i++) {
            const s = data.add(OFF.ArrayObj.data + i * 8).readPointer();
            const k = hlStr(s);
            if (k) out[k] = s;
        }
    } catch (e) {}
    return out;
}

// Game thread (camera hook). Fires at most one queued equip per frame.
function drainGliderEquip() {
    if (!pendingGliderEquip) return;
    const job = pendingGliderEquip;
    pendingGliderEquip = null;
    try {
        if (!gliderEquipAddr) return;
        if (!heroIsMe(job.hero)) return;
        const player = job.hero.add(OFF.Hero.player).readPointer();
        const acct = player.add(OFF.Player.accountProgress).readPointer();
        const coll = acct.add(OFF.AccountProgress.collection).readPointer();
        if (!coll || coll.isNull()) return;
        // (collection, kind, onResult=null) — see the section header for why
        // the third argument is a callback and why null is what the game's
        // own generated code treats as "not passed".
        new NativeFunction(gliderEquipAddr, "pointer",
                           ["pointer", "pointer", "pointer"])(
            coll, job.str, ptr(0));
        // Frida routes a NativeFunction built from a hooked address through
        // the original trampoline, so the equipItem hook above does NOT see
        // our own call (measured: 5/5 auto-equips produced no hook line).
        // Without this the "don't repeat the current one" filter would keep
        // comparing against the last MANUAL equip forever.
        lastGliderKind = job.kind;
        log("glider equip (" + gliderCfg.mode + "): -> " + job.kind);
    } catch (e) { log("glider equip failed: " + e); }
}

function hookGliderEquip(base) {
    const gt = DATA.glider_targets || {};
    const fiEquip = gt["st.player.Collection.equipItem"];
    const fiToggle = gt["ent.Hero.toggleGlide"];
    if (fiEquip === undefined || fiToggle === undefined) {
        log("!! glider targets missing from resolver data — random glider "
            + "disabled. The data file is older than this build; delete "
            + "analysis_out and restart.");
        return;
    }
    if (OFF.Collection.gliders === undefined) {
        log("!! glider offsets missing — random glider disabled. The offsets "
            + "file is older than this build; delete analysis_out and restart.");
        return;
    }
    try { gliderEquipAddr = base.add(fiEquip * 8).readPointer(); }
    catch (e) { log("!! equipItem unreadable: " + e.message); return; }
    // Passive: remember the current glider through EVERY equip (manual or
    // ours) so the random pick can avoid repeating it.
    Interceptor.attach(gliderEquipAddr, {
        onEnter: function (args) {
            try {
                const kind = hlStr(args[1]);
                if (kind && kind.lastIndexOf("Glider_", 0) === 0)
                    lastGliderKind = kind;
            } catch (e) {}
        }
    });
    // The trigger: local glide END (toggleGlide args[1] null after a start).
    // Same no-cached-pointers rule as the mount swap: everything is walked
    // from the hooked hero itself.
    Interceptor.attach(base.add(fiToggle * 8).readPointer(), {
        onEnter: function (args) {
            try {
                const up = args[1] && !args[1].isNull();
                if (!heroIsMe(args[0])) return;
                if (up) { localGlideUp = true; return; }
                if (!localGlideUp) return;
                localGlideUp = false;
                if (!gliderCfg.enabled || !gliderCfg.favorites.length) return;
                // Short cooldown, not a cap: gliding is often a rapid
                // tap-on-tap-off, and every equip is a real server RPC. Long
                // enough to swallow a double-tap, short enough that ordinary
                // landings all count (the proof probe's 5s felt like the
                // feature was misfiring — measured 2026-08-02).
                const now = Date.now();
                if (now - lastGliderEquipMs < 1500) return;
                const pool = readGliderKinds(args[0]);
                const owned = gliderCfg.favorites.filter(function (k) {
                    return k in pool;
                });
                if (!owned.length) return;
                let pick;
                if (gliderCfg.mode === "cycle") {
                    pick = owned[gliderCfg.cycleIdx % owned.length];
                    gliderCfg.cycleIdx =
                        (gliderCfg.cycleIdx + 1) % owned.length;
                } else {
                    let picks = owned.filter(function (k) {
                        return k !== lastGliderKind;
                    });
                    if (!picks.length) picks = owned;
                    pick = picks[Math.floor(Math.random() * picks.length)];
                }
                pendingGliderEquip = { kind: pick, str: pool[pick],
                                       hero: args[0] };
                lastGliderEquipMs = now;
            } catch (e) {}
        }
    });
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

    // DATA.map_fn (Main.getMapId) is no longer resolved or called — measured
    // returning the machine hostname; the zone signal reads layer.world.level.

    for (const nm in DATA.funcs) {
        try { getHeroFns.push({ name: nm, addr: base.add(DATA.funcs[nm] * 8).readPointer() }); } catch (e) {}
    }
    // Both the first lookup and the 3s refresh (survive respawn / zone
    // changes) are deferred to the camera hook's game thread — see
    // heroRefreshDue.
    heroRefreshDue = true;
    setInterval(function () { heroRefreshDue = true; }, 3000);

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
        checkRift();
        sweepInventory();     // plain reads; see the legendary-pickup section
    }, 400);

    // The shard roster, on its own slow clock. A hub list of 30 people is not
    // worth rebuilding at the minimap's 150ms, and sweepShard() suppresses
    // resends of an unchanged list anyway — so this costs one array walk every
    // two seconds and usually sends nothing.
    if (OFF.Player && OFF.Player.uid != null
        && OFF.GameLayer && OFF.GameLayer.players != null) {
        shardTimer = setInterval(sweepShard, 2000);
    } else {
        // Same failure mode the inventory sweep warns about: a stale
        // analysis_out silently has no `uid`, readShard() returns [] on its
        // first line forever, and the Social tab just looks empty rather than
        // broken. Say so once.
        log("!! Player.uid / GameLayer.players missing from analysis_out — "
            + "the Social tab will stay empty. Regenerate offsets.");
    }

    // Same reasoning as the minimap warning below: sweepInventory() bails on
    // its first line when an offset is absent, silently and forever. An
    // upgrade that keeps an older %LOCALAPPDATA%\analysis_out is exactly how
    // that happens.
    const INV_NEED = ["Loadout", "Inventory", "Item", "Weapon"];
    const invAbsent = INV_NEED.filter(function (k) { return !OFF[k]; });
    if (invAbsent.length)
        log("!! inventory offsets missing (" + invAbsent.join(", ") +
            ") — the legendary pickup cue will never fire. The offsets file " +
            "is older than this build; delete analysis_out and restart.");

    // The minimap wants a faster cadence than the combat heartbeat: at 400ms
    // dots visibly step rather than move. The sweep costs ~1ms, so its own
    // timer is cheaper than it looks. The host resets this from the Refresh
    // setting as soon as it connects.
    hookCamera(base);
    hookBossBar(base);
    hookMountSwap(base);
    hookGliderEquip(base);
    // The unlocked mount/glider lists, for the menus' favorites checklists.
    // Plain reads on a slow timer; re-sent only when a set changes (a new
    // unlock mid-session shows up within a tick).
    setInterval(function () {
        try {
            if (!localHero || localHero.isNull()) return;
            const kinds = Object.keys(readMountKinds(localHero)).sort();
            if (kinds.length) {
                const sig = kinds.join(",");
                if (sig !== lastMountsSig) {
                    lastMountsSig = sig;
                    send({ kind: "mounts", list: kinds });
                }
            }
            const gkinds = Object.keys(readGliderKinds(localHero)).sort();
            if (gkinds.length) {
                const gsig = gkinds.join(",");
                if (gsig !== lastGlidersSig) {
                    lastGlidersSig = gsig;
                    send({ kind: "gliders", list: gkinds });
                }
            }
        } catch (e) {}
    }, 5000);
    // Say so if the sweep can't run. sweepWorld bails on its first line when
    // an offset it needs is absent, and it does that inside a try/catch on a
    // timer — so without this the minimap simply stays on "waiting for the
    // game" with nothing anywhere explaining why. That shipped once: an
    // upgrade left offsets in %LOCALAPPDATA% that predated the minimap, and
    // the only clue was a camera warning about something else.
    const NEED = ["Entity", "ArrayObj", "GameLayer", "Element", "Interactible",
                  "State", "Unit", "Foe", "Hero"];
    const absent = NEED.filter(function (k) { return !OFF[k]; });
    if (absent.length)
        log("!! minimap offsets missing (" + absent.join(", ") +
            ") — the map will stay empty. The offsets file is older than this " +
            "build; delete analysis_out and restart to regenerate it.");
    setWorldTick(worldTickMs);

    // Host -> agent config. re-armed after each message, which is how frida's
    // recv() works: a handler fires once.
    function onConfig(msg) {
        try {
            if (msg && msg.worldTick) setWorldTick(msg.worldTick);
            if (msg && msg.mounts) {
                mountCfg.enabled = !!msg.mounts.enabled;
                mountCfg.favorites = Array.isArray(msg.mounts.favorites)
                    ? msg.mounts.favorites : [];
                mountCfg.mode = msg.mounts.mode === "cycle" ? "cycle" : "random";
                mountCfg.cycleIdx = 0;
                log("mount config: " + (mountCfg.enabled ? "on" : "off")
                    + " (" + mountCfg.mode + "), "
                    + mountCfg.favorites.length + " favorites");
            }
            if (msg && msg.gliders) {
                gliderCfg.enabled = !!msg.gliders.enabled;
                gliderCfg.favorites = Array.isArray(msg.gliders.favorites)
                    ? msg.gliders.favorites : [];
                gliderCfg.mode = msg.gliders.mode === "cycle" ? "cycle"
                                                              : "random";
                gliderCfg.cycleIdx = 0;
                log("glider config: " + (gliderCfg.enabled ? "on" : "off")
                    + " (" + gliderCfg.mode + "), "
                    + gliderCfg.favorites.length + " favorites");
            }
        }
        catch (e) { log("config failed: " + e); }
        recv("config", onConfig);
    }
    recv("config", onConfig);

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
        const out = {
            amount: amount,
            skill: skill || "?",
            name: sname,
            element: hlStr(dr.add(DR.affinity).readPointer()) || "?",
            crit: dr.add(DR._critical).readU8() ? 1 : 0,
            kill: dr.add(DR._kill).readU8() ? 1 : 0,
        };
        // Nullified-hit diagnostic. The meter counts `amount` whether or not
        // the target took it, so a boss in an immunity phase inflates the
        // parse. These three fields are the candidates for marking that, and
        // they ride along ONLY when one of them is actually set — on an
        // ordinary hit this adds nothing to the message.
        try {
            if (DR.blocker != null && DR.effect != null) {
                const blk = dr.add(DR._block).readDouble();
                const who = hlStr(dr.add(DR.blocker).readPointer());
                const eff = dr.add(DR.effect).readS32();
                if (blk > 0 || who || eff !== 0) {
                    out.block = blk;
                    out.blocker = who || "";
                    out.effect = eff;
                }
            }
        } catch (e) {}
        return out;
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
    // A client is never told how much a heal healed for. Measured 2026-08-03
    // (frida/run_heal.py, 40 heal events across 6 healers and 4 skills): of the
    // fifteen heal entry points in this build, ONLY ent.Unit.playHitHealFX runs
    // on a client, and its HitData.amount reads 0.000. receiveHeal, computeHeal,
    // evalHeal, the four *HealEval callbacks, applyHeal, rpcDisplayHeal(__impl)
    // and ui.hud.EffectsFeed.displayHeal never fire here at all.
    //
    // So healing is captured from the two things that ARE replicated:
    //   * ent.Unit.playHitHealFX(target=rcx, hitData=rdx): fires on every
    //     healed unit; HitData.baseSkill names the healing skill and its owner
    //     (the healer). This is the heal EVENT — it happens whether or not the
    //     target had any health to restore.
    //   * ent.UnitAttributes.set_health(attrs=rcx, v): the replicated health
    //     value. A RISE in a hero's health is how much of that heal LANDED.
    //
    // Every FX is emitted exactly once, with `landed` = the health rise it
    // produced or 0 if it produced none — a heal on a full-health target is a
    // real heal and the meter has to see it. (Before this, only rises were
    // sent, so a healer topping people off scored nothing and the parse
    // measured who healed FASTEST rather than who healed HARDEST.) The host
    // turns `landed` into a raw amount and an overheal share; the agent stays
    // out of that so the estimator can change without a re-inject.
    //
    // FX-less rises while in combat are natural regen (self-heal), and
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
        // target unit ptr -> queue of heal FX awaiting a health rise. A QUEUE,
        // not a slot: two healers can land on the same target inside the match
        // window, and the old single-slot map dropped the first one outright.
        const pendingHealFx = {};
        const HEAL_MATCH_MS = 1500;   // how long an FX waits for its rise
        const HEAL_QUEUE_MAX = 32;    // a HoT storm must not grow without bound

        // `est` marks an event whose size the host is allowed to estimate —
        // i.e. one that came from a heal FX, where `landed` may be a capped
        // view of a bigger heal. Natural regen is NOT estimable: it has no FX,
        // it is only ever observed AS the health rise, and running it through
        // the estimator would credit every small tick at the largest tick's
        // size.
        function emitHeal(fx, landed, estimable) {
            send(Object.assign({ kind: "heal", skill: fx.skill, name: fx.name,
                                 element: "Heal", amount: landed,
                                 landed: landed, est: estimable ? 1 : 0,
                                 self: fx.slf ? 1 : 0,
                                 step: fx.step, dyn: fx.dyn, atb: fx.atb,
                                 crit: 0, kill: 0 }, fx.who));
        }

        // What a heal was WORTH, as far as a client can see it. The amount is
        // never sent (measured — see the note above), but the cdb says how the
        // game computes each skill's heal, and both of its ingredients ARE
        // here: BaseSkill.dynVal1-3 are replicated, and a scaling heal is a
        // ratio on one of the caster's attributes. So the raw numbers ride
        // along with every heal event and the host does the arithmetic against
        // analysis_out/heal_specs.json.
        function healInputs(bs, hitData, fx) {
            try {
                const B2 = OFF.BaseSkill;
                if (B2.dynVal1 != null) {
                    fx.dyn = [bs.add(B2.dynVal1).readDouble(),
                              bs.add(B2.dynVal2).readDouble(),
                              bs.add(B2.dynVal3).readDouble()];
                }
                // Which step fired: a skill can heal from more than one step
                // at different rates (Sword_Swarm_Combo does), and the spec is
                // per step index.
                if (HD.step != null && OFF.SkillStep) {
                    const st = hitData.add(HD.step).readPointer();
                    if (st && !st.isNull())
                        fx.step = st.add(OFF.SkillStep.index).readS32();
                }
                // The caster's attributes. `owner` is the healing unit, which
                // for a totem or a summon is the totem — its stats, not the
                // summoner's. Accepted: those skills are a small minority and
                // the landed-heal fallback still covers them.
                const owner = bs.add(OFF.BaseSkill.owner).readPointer();
                if (owner && !owner.isNull() && OFF.Unit.attr != null) {
                    const at = owner.add(OFF.Unit.attr).readPointer();
                    if (at && !at.isNull() && at.compare(ptr("0x10000")) > 0) {
                        fx.atb = {
                            Faith: at.add(UA.faith).readDouble(),
                            Intellect: at.add(UA.intellect).readDouble(),
                            Strength: at.add(UA.strength).readDouble(),
                            Dexterity: at.add(UA.dexterity).readDouble(),
                        };
                    }
                }
            } catch (e) {}
        }

        // Did the healer heal THEMSELVES? Pointer identity settles it when the
        // skill's owner is the healed hero. The name fallback exists for the
        // skills a player owns but does not personally cast — a totem or a
        // summon healing the player who put it down is still that player
        // healing themselves, and there the owner pointer is the totem.
        function isSelfHeal(bs, target, who) {
            try {
                const owner = bs.add(OFF.BaseSkill.owner).readPointer();
                if (owner && !owner.isNull() && owner.equals(target)) return 1;
                const tn = hlStr(target.add(OFF.Hero.name).readPointer());
                return (tn && who.player === tn) ? 1 : 0;
            } catch (e) { return 0; }
        }

        function flushExpired(q, now) {
            // Nothing rose within the window, so this heal restored nothing.
            // It still happened — emit it with landed 0 rather than drop it.
            while (q.length && now - q[0].t > HEAL_MATCH_MS) emitHeal(q.shift(), 0, true);
        }

        setInterval(function () {
            const now = Date.now();
            for (const k in pendingHealFx) {
                flushExpired(pendingHealFx[k], now);
                if (!pendingHealFx[k].length) delete pendingHealFx[k];
            }
        }, 250);

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
                    const key = target.toString();
                    const q = pendingHealFx[key] || (pendingHealFx[key] = []);
                    if (q.length >= HEAL_QUEUE_MAX) emitHeal(q.shift(), 0, true);
                    const fx = { t: Date.now(), who: who, skill: skill,
                                 slf: isSelfHeal(bs, target, who),
                                 name: skill !== "?" ? skillDisplayName(bs, skill) : "" };
                    // Read NOW, not when the event is emitted: an FX that never
                    // lands is flushed up to 1.5 s later, by which time the
                    // skill may have been re-cast (dynVal1 rewritten) or freed.
                    healInputs(bs, c.rdx, fx);
                    q.push(fx);
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
                    const q = pendingHealFx[key];
                    let fx = null;
                    if (q) {
                        // Anything past the window can't own this rise; emit
                        // those as the zero-landing heals they are, then take
                        // the oldest survivor.
                        flushExpired(q, Date.now());
                        if (q.length) fx = q.shift();
                        if (!q.length) delete pendingHealFx[key];
                    }
                    if (fx) { emitHeal(fx, delta, true); return; }
                    // FX-less rise: natural regen. Only meaningful in
                    // combat — out-of-combat regen is constant noise.
                    if (!unit.add(OFF.Hero.isInCombat).readU8()) return;
                    // Regen is self-healing by definition: the unit whose
                    // health rose is both the healer and the healed.
                    emitHeal({ who: heroIdent(unit), skill: "Regen",
                               name: "Regen", slf: 1 }, delta, false);
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

    log("meter hook active (local hero " + (localName ? "identified" : "pending")
        + ")");
    send({ kind: "ready", ok: true });
}
// Defer setup off the load() call so script.load() returns immediately. Running
// the memory scan synchronously inside load() blocks the injection handshake and
// can stall the game's thread mid-inject; deferring lets injection settle first.
setTimeout(main, 150);
