// combatmeter_probe.js — spike: can we drive Farever's OWN UI from a hook?
//
// Turns on the game's built-in damage meter (ui.hud.CombatMeter, gated by the
// displayCombatMeter option) without touching options.ini or restarting:
//
//   1. walk GameApp -> gui -> gameRoot -> hud -> combatMeter, reporting the
//      runtime type at every hop so a wrong offset shows up as a bad type name
//      rather than a crash further down;
//   2. call the static $Options.set_displayCombatMeter(true);
//   3. call hud.updateVisibility() so the HUD re-reads the option.
//
// Everything runs INSIDE a hook on GameApp.update, i.e. on the game thread.
// Calling into HL from a Frida timer thread crashes the runtime — the same
// constraint meter_hook.js documents for Main.getMapId.
//
// DATA (resolver_data.json), FN (findex by name) and OFF (byte offsets) are
// prepended by the Python runner, which reads them out of hlboot.dat BY NAME so
// this survives a patch.

function log(m) { send({ kind: "log", msg: String(m) }); }
function report(o) { send(Object.assign({ kind: "report" }, o)); }

// ---- functions_ptrs resolver (statics walk, as in meter_hook.js) ----
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
    let n = 0;
    for (const o of resolved) {
        let v; try { v = base.add(o.findex * 8).readPointer(); } catch (e) { return false; }
        if (!v.equals(o.addr)) return false;
        if (++n >= 3) return true;
    }
    return n > 0;
}
function findTableFast(resolved) {
    if (!resolved.length) return null;
    const t0 = Date.now(), BUDGET_MS = 8000, MAX_NODES = 30000, EXPAND = 64;
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
function typeName(p) {
    try {
        if (!p || p.isNull()) return null;
        const t = p.readPointer(); const k = t.readU32();
        if (k === 11 || k === 21) return t.add(8).readPointer().add(16).readPointer().readUtf16String();
        return "kind" + k;
    } catch (e) { return null; }
}

// Follow one pointer field, naming both sides so a bad offset is obvious.
function hop(obj, off, label, steps) {
    if (!obj || obj.isNull()) { steps.push(label + " = <null parent>"); return null; }
    let v;
    try { v = obj.add(off).readPointer(); }
    catch (e) { steps.push(label + " = <unreadable @" + off + ">"); return null; }
    steps.push(label + " @" + off + " -> " + (v.isNull() ? "null" : typeName(v)));
    return (v && !v.isNull()) ? v : null;
}

let done = false;

function main() {
    const resolved = resolveAnchors();
    const base = findTableFast(resolved);
    if (!base) { log("!! functions_ptrs table not found"); report({ ok: false }); return; }
    log("functions_ptrs resolved");

    function fnAddr(name) {
        const fi = FN[name];
        if (fi == null) { log("!! no findex for " + name); return null; }
        try { return base.add(fi * 8).readPointer(); } catch (e) { return null; }
    }

    const updAddr = fnAddr("GameApp.update");
    if (!updAddr) { report({ ok: false }); return; }

    const setOptAddr = fnAddr("$Options.set_displayCombatMeter");
    const updVisAddr = fnAddr("ui.Hud.updateVisibility");
    const rebuildAddr = fnAddr("ui.Hud.rebuild");
    const saveAddr = fnAddr("$Options.save");

    // Runs once, on the game thread, from inside GameApp.update.
    Interceptor.attach(updAddr, {
        onLeave() {
            if (done) return;
            done = true;
            const steps = [];
            const out = { ok: false, steps: steps };
            try {
                const app = this.gameApp;
                steps.push("GameApp (rcx) -> " + typeName(app));
                const gui = hop(app, OFF.GameApp.gui, "GameApp.gui", steps);
                const root = hop(gui, OFF.GameUI.gameRoot, "GameUI.gameRoot", steps);
                const hud = hop(root, OFF.GameUiRoot.hud, "GameUiRoot.hud", steps);
                let cm = hop(hud, OFF.Hud.combatMeter, "Hud.combatMeter", steps);

                if (cm) {
                    out.visible_before = cm.add(OFF.Object.visible).readU8();
                    out.alpha_before = cm.add(OFF.Object.alpha).readDouble();
                }

                // 1) flip the option (static: the bool is the only argument)
                if (setOptAddr) {
                    new NativeFunction(setOptAddr, "uint8", ["uint8"])(1);
                    steps.push("set_displayCombatMeter(true) returned");
                }
                // 2) The HUD only builds the meter when the option is on at
                //    init, so if it isn't there yet updateVisibility has
                //    nothing to show — rebuild the HUD subtree first.
                if (!cm && hud && rebuildAddr) {
                    new NativeFunction(rebuildAddr, "void", ["pointer"])(hud);
                    steps.push("hud.rebuild() returned");
                    cm = hop(hud, OFF.Hud.combatMeter, "Hud.combatMeter (after rebuild)",
                             steps);
                    if (cm) {
                        out.visible_before = cm.add(OFF.Object.visible).readU8();
                        out.alpha_before = cm.add(OFF.Object.alpha).readDouble();
                    }
                }
                // 3) make the HUD re-read the option
                if (hud && updVisAddr) {
                    new NativeFunction(updVisAddr, "void", ["pointer"])(hud);
                    steps.push("hud.updateVisibility() returned");
                }
                if (cm) {
                    out.visible_after = cm.add(OFF.Object.visible).readU8();
                    out.alpha_after = cm.add(OFF.Object.alpha).readDouble();
                    // Last resort if updateVisibility didn't do it for us.
                    if (!out.visible_after) {
                        cm.add(OFF.Object.visible).writeU8(1);
                        steps.push("forced combatMeter.visible = 1");
                        out.visible_forced = cm.add(OFF.Object.visible).readU8();
                    }
                }
                if (PERSIST && saveAddr) {
                    new NativeFunction(saveAddr, "void", [])();
                    steps.push("Options.save() returned (persisted to options.ini)");
                }
                out.ok = true;
            } catch (e) {
                out.error = String(e);
            }
            report(out);
        },
        onEnter() { this.gameApp = this.context.rcx; }
    });

    log("armed — waiting for the next GameApp.update tick");
}
setTimeout(main, 150);
