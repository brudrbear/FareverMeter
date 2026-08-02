// steamid_probe.js — can the client see other players' Steam IDs?
//
// The question is not "does the game link to Steam" (it plainly does — there is
// a whole steam.hdll with lobbies, P2P and auth tickets). It is the narrower,
// answerable one: at runtime, in a live session, does this client hold a
// SteamID for anyone other than me?
//
// There are exactly three places a peer's SteamID could arrive, and this probe
// watches all three at once so a negative result means something:
//
//   1. REPLICATED GAME STATE. st.Player.uid and st.player.HeroData.accountID
//      are both hxbit network properties (they have __net_mark_/networkProp
//      accessors), so the server CAN push them to us. Whether it does, and
//      whether the value is a SteamID64 or the game's own account GUID, is a
//      measurement. Walking st.GameLayer.players gives every player the client
//      knows about, not just my party — that is the population that matters.
//
//   2. THE STEAM USER CACHE PATH. steam.User wraps a raw SteamID (`uid:bytes`)
//      and is minted by steam.$User.fromUID/fromUID32. If the client ever
//      learns a peer's SteamID it must pass through there to do anything with
//      it, so hooking those catches the identity even if it is never stored on
//      a player object.
//
//   3. THE STEAM API ITSELF. steam_get_user_name / steam_request_user_information
//      / steam_get_user_avatar all take a SteamID as their first argument.
//      These are hooked as raw hdll exports, which means they are caught even
//      if the call comes from a code path nobody enumerated.
//
// The comparison that decides it: my OWN st.Player.uid is read alongside my
// real SteamID64 (steam_get_steam_id). If they match, `uid` is a SteamID and
// every other player's uid in the same list is one too. If they do not, uid is
// an internal account id and the answer to the question is no — regardless of
// how steam-shaped the field names look.
//
// THREAD RULE (paid for twice, see the internals notes): HL calls from a frida
// timer thread kill the game with "Can't lock GC in unregistered thread". So
// every call and every object walk happens inside a borrowed game-thread hook
// (client.BaseCamera.postUpdate); the timer only prints what was already
// collected.
//
// DATA + OFF + B are prepended by run_steamid.py.

function log(m) { send({ kind: "log", msg: String(m) }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=base.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return base;}}}return null;}

// HL String -> js string (utf16 payload behind .bytes)
function hlStr(p){try{if(!p||p.isNull())return null;const b=p.add(OFF.String.bytes).readPointer();if(b.isNull())return null;return b.readUtf16String();}catch(e){return null;}}

// A raw hl `bytes` holding a SteamID could be utf8 decimal text, utf16 text,
// or the 64-bit id itself. Guessing wrong and silently returning null is how
// "the client never sees a SteamID" gets concluded from a decoding bug, so
// this reports EVERY candidate plus the raw hex and lets the reader judge.
function hex(p, n) {
    try {
        const b = p.readByteArray(n);
        if (!b) return "(unreadable)";
        const u = new Uint8Array(b);
        let s = "";
        for (let i = 0; i < u.length; i++) s += ("0" + u[i].toString(16)).slice(-2) + " ";
        return s.trim();
    } catch (e) { return "(fault)"; }
}

function decodeId(p) {
    const r = { ptr: p ? p.toString() : "(null)", utf8: null, utf16: null,
                u64: null, u32: null, hex: null };
    if (!p || p.isNull()) return r;
    r.hex = hex(p, 24);
    try { const s = p.readCString();      if (s && /^[\x20-\x7e]+$/.test(s)) r.utf8  = s; } catch (e) {}
    try { const s = p.readUtf16String();  if (s && /^[\x20-\x7e]+$/.test(s)) r.utf16 = s; } catch (e) {}
    try { r.u64 = p.readU64().toString(); } catch (e) {}
    try { r.u32 = p.readU32() >>> 0; } catch (e) {}
    return r;
}

// The single best-guess textual id, or null if nothing decoded.
function rawBytes(p) {
    const d = decodeId(p);
    return d.utf8 || d.utf16 || null;
}

// SteamID64 = 76561197960265728 + accountID(32-bit). Reported for every
// candidate so a 32-bit id can be checked against a known account.
const STEAM_BASE = uint64("76561197960265728");
function toSteam64(accountId) {
    try { return STEAM_BASE.add(uint64(accountId >>> 0)).toString(); }
    catch (e) { return "?"; }
}

// st.Player.uid is "S" + the Steam ACCOUNT ID as hex, written in the byte
// order it sits in memory (little-endian) rather than as a number. That is why
// it does not look like a SteamID at a glance and why a naive big-endian read
// of the same digits gives a wrong, plausible-looking value — the trap this
// probe walked into once already. Reversed and added to the base constant it
// reproduces the SteamID64 exactly; the report proves that on MY OWN uid,
// against the id the Steam API hands back, before believing it for anyone else.
function uidToSteam64(uid) {
    if (!uid || uid.charAt(0) !== "S") return null;
    let h = uid.slice(1);
    if (!/^[0-9a-fA-F]+$/.test(h)) return null;
    if (h.length % 2) h = "0" + h;
    let acct = uint64(0);
    for (let i = h.length - 2; i >= 0; i -= 2)     // consume little-endian
        acct = acct.shl(8).or(uint64(parseInt(h.substr(i, 2), 16)));
    return { account: acct.toString(), steam64: STEAM_BASE.add(acct).toString() };
}

function describeId(d) {
    const bits = [];
    if (d.utf8)  bits.push("utf8='" + d.utf8 + "' [" + shape(d.utf8) + "]");
    if (d.utf16) bits.push("utf16='" + d.utf16 + "' [" + shape(d.utf16) + "]");
    if (d.u64 !== null) bits.push("u64=" + d.u64
        + (looksLikeSteamID64(d.u64) ? " <-- STEAMID64" : ""));
    if (d.u32 !== null) bits.push("u32=" + d.u32 + " (as SteamID64 -> "
        + toSteam64(d.u32) + ")");
    bits.push("hex=" + d.hex);
    return bits.join("  ");
}

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

// SteamID64 for an individual account: 17 decimal digits in the
// 7656119... block (base 76561197960265728). Anything else is not a SteamID,
// which is exactly the distinction this probe exists to draw.
function looksLikeSteamID64(s) {
    return !!s && /^7656119[0-9]{10}$/.test(s);
}
function shape(s) {
    if (s === null || s === undefined) return "(null)";
    if (looksLikeSteamID64(s)) return "STEAMID64";
    if (/^[0-9]+$/.test(s)) return "digits(" + s.length + ")";
    if (/^[0-9a-fA-F]{16,}$/.test(s)) return "hex(" + s.length + ")";
    if (/^[0-9a-fA-F-]{32,}$/.test(s)) return "guid-ish";
    return "text(" + s.length + ")";
}

let base = null, localHero = null, armed = false, tick = 0;
let mySteamID = null, mySteamErr = null, steamTried = false, mySteamRaw = null;
const seenUsers = [];       // steam.User object pointers the client has minted
let roster = [];            // last full walk of st.GameLayer.players
let party = [];             // st.Group.players, for contrast
let walkErr = null;
const steamCalls = {};      // evidence from hooks: which SteamIDs got looked up
let steamCallCount = 0;

// `p` is the pointer that should hold a SteamID (an hl `bytes`). Every
// decoding is printed on first sight of a given (call site, value) pair, so a
// single line is enough to tell a real SteamID from a decoding failure.
function noteSteamCall(via, p) {
    const d = decodeId(p);
    const k = via + " " + d.hex;
    if (!steamCalls[k]) {
        steamCalls[k] = { via: via, d: d, n: 0 };
        log("  ** STEAM IDENTITY CALL: " + via);
        log("       " + describeId(d));
    }
    steamCalls[k].n++;
    steamCallCount++;
}

// For call sites where the argument is a plain integer id, not a pointer.
function noteSteamCallInt(via, v) {
    const id = v >>> 0;
    const k = via + " i" + id;
    if (!steamCalls[k]) {
        steamCalls[k] = { via: via, d: { u32: id }, n: 0 };
        log("  ** STEAM IDENTITY CALL: " + via + "  accountID=" + id
            + "  -> SteamID64 " + toSteam64(id));
    }
    steamCalls[k].n++;
    steamCallCount++;
}

// ---- the walk -------------------------------------------------------------
// hxbit.ArrayProxyData -> .array(@40) hl.types.ArrayDyn -> .array(@8) ArrayObj
// -> length(@8), native varray(@16); varray elements start at +24.
function readProxyArray(proxy, cap) {
    const out = [];
    if (!proxy || proxy.isNull()) return out;
    const dyn = proxy.add(OFF.ArrayProxyData.array).readPointer();
    if (!dyn || dyn.isNull()) return out;
    const arr = dyn.add(OFF.ArrayDyn.array).readPointer();
    if (!arr || arr.isNull()) return out;
    const n = arr.add(OFF.ArrayObj.length).readS32();
    const varr = arr.add(OFF.ArrayObj.array).readPointer();
    if (n < 0 || n > cap || !varr || varr.isNull()) return out;
    for (let i = 0; i < n; i++) {
        const p = varr.add(24 + i * 8).readPointer();
        if (p && !p.isNull()) out.push(p);
    }
    return out;
}

function readPlayer(p) {
    const r = { ptr: p.toString(), cls: typeName(p) };
    try { r.name = hlStr(p.add(B.Player.name).readPointer()); } catch (e) {}
    try { r.uid = hlStr(p.add(B.Player.uid).readPointer()); } catch (e) {}
    try { r.isMe = p.add(B.Player.isMe).readU8() !== 0; } catch (e) {}
    try { r.lobbyId = hlStr(p.add(B.Player.lobbyId).readPointer()); } catch (e) {}
    // mpman.User is the platform-account wrapper hanging off the player.
    try {
        const u = p.add(B.Player.user).readPointer();
        if (u && !u.isNull()) {
            r.userCls = typeName(u);
            if (B.User.name != null) r.userName = hlStr(u.add(B.User.name).readPointer());
        }
    } catch (e) {}
    // The class. ent.Hero is an entity, so "absent" here is a real state
    // (player on the layer but not streamed in), not a read failure — the two
    // are reported differently because only one of them is a bug.
    try {
        const h = B.Player.hero != null ? p.add(B.Player.hero).readPointer() : null;
        if (!h || h.isNull()) r.cls = "(no hero entity)";
        else {
            r.heroCls = typeName(h);
            if (B.Hero.kind != null) r.cls = hlStr(h.add(B.Hero.kind).readPointer());
            if (B.Hero._level != null) r.lvl = h.add(B.Hero._level).readS32();
        }
    } catch (e) { r.cls = "(fault)"; }

    // HeroData.accountID — the other replicated identity string. "the object
    // isn't replicated to us at all" and "it is, but the field is empty" are
    // different answers to the question, so they are reported differently.
    try {
        const hd = p.add(B.Player.heroData).readPointer();
        if (!hd || hd.isNull()) r.accountID = "(no heroData)";
        else if (B.HeroData.accountID == null) r.accountID = "(offset missing)";
        else {
            r.heroDataCls = typeName(hd);
            r.accountID = hlStr(hd.add(B.HeroData.accountID).readPointer());
        }
    } catch (e) { r.accountID = "(fault)"; }
    return r;
}

function walk() {
    walkErr = null;
    try {
        const player = localHero.add(OFF.Hero.player).readPointer();
        if (!player || player.isNull()) { walkErr = "hero.player null"; return; }

        // Server-wide roster: every player this client has state for.
        const layer = player.add(B.Player.layer).readPointer();
        if (layer && !layer.isNull()) {
            roster = readProxyArray(layer.add(B.GameLayer.players).readPointer(), 256)
                        .map(readPlayer);
        } else {
            walkErr = "player.layer null";
        }

        const group = player.add(B.Player.group).readPointer();
        if (group && !group.isNull())
            party = readProxyArray(group.add(B.Group.players).readPointer(), 64)
                        .map(readPlayer);
    } catch (e) { walkErr = String(e); }
}

// My real SteamID64, straight from the Steam API. This is the yardstick the
// replicated uid gets measured against. Called on the game thread only: it
// allocates HL bytes, so a timer thread would trip the GC assert.
function fetchMySteamID() {
    if (steamTried) return;
    steamTried = true;
    try {
        const m = Process.findModuleByName("steam.hdll");
        if (!m) { mySteamErr = "steam.hdll not loaded"; return; }
        const f = m.findExportByName("steam_get_steam_id");
        if (!f || f.isNull()) { mySteamErr = "steam_get_steam_id not exported"; return; }
        const r = new NativeFunction(f, "pointer", [])();
        mySteamRaw = decodeId(r);
        mySteamID = mySteamRaw.utf8 || mySteamRaw.utf16 || null;
        if (!mySteamID && mySteamRaw.u64 && looksLikeSteamID64(mySteamRaw.u64))
            mySteamID = mySteamRaw.u64;
        if (!mySteamID) mySteamErr = "no decoding yielded a SteamID";
    } catch (e) { mySteamErr = String(e); }
}

function refreshLocalHero() {
    for (const nm in DATA.funcs) {
        try {
            const h = new NativeFunction(base.add(DATA.funcs[nm] * 8).readPointer(),
                                         "pointer", [])();
            if (h && !h.isNull() && typeName(h) === "ent.Hero") { localHero = h; return; }
        } catch (e) {}
    }
}

function row(r) {
    const c = uidToSteam64(r.uid);
    return "     " + (r.isMe ? "* " : "  ")
        + String(r.name || "?").padEnd(16)
        + " uid=" + String(r.uid === null || r.uid === undefined ? "(null)" : r.uid).padEnd(12)
        + " -> " + (c ? c.steam64 : "(not convertible)")
        + "  class=" + String(r.cls === null ? "(null)" : r.cls).padEnd(18)
        + " lvl=" + (r.lvl === undefined ? "?" : r.lvl);
}

function report() {
    tick++;
    if (!armed) {
        if (tick % 3 === 0) log("[waiting] hero not latched yet — load into the world.");
        return;
    }
    log("");
    log("---- tick " + tick + " ----------------------------------------------");
    log("  MY REAL STEAMID (steam_get_steam_id): "
        + (mySteamID ? mySteamID + "  [" + shape(mySteamID) + "]"
                     : "UNAVAILABLE (" + mySteamErr + ")"));
    if (mySteamRaw) log("     raw return: " + describeId(mySteamRaw));
    // Every steam.User the client has minted, dumped straight from the object.
    if (seenUsers.length) {
        log("  -- steam.User objects the client holds: " + seenUsers.length + " --");
        for (const s of seenUsers.slice(0, 10)) {
            try {
                const u = ptr(s);
                const nm = B.SteamUser.cachedName != null
                    ? hlStr(u.add(B.SteamUser.cachedName).readPointer()) : null;
                log("     " + s + (nm ? "  personaName=" + nm : "")
                    + "  uid: " + describeId(u.add(B.SteamUser.uid).readPointer()));
            } catch (e) { log("     " + s + "  (unreadable: " + e + ")"); }
        }
    }
    if (walkErr) log("  !! roster walk error: " + walkErr);

    log("  -- st.GameLayer.players (everyone this client has state for): "
        + roster.length + " --");
    for (const r of roster.slice(0, 40)) log(row(r));
    if (roster.length > 40) log("     ... " + (roster.length - 40) + " more");

    log("  -- st.Group.players (my party): " + party.length + " --");
    for (const r of party) log(row(r));

    // The verdict, recomputed each tick so it reflects the live population.
    const me = roster.filter(function (r) { return r.isMe; })[0];
    const others = roster.filter(function (r) { return !r.isMe; });
    log("  -- VERDICT --");

    // The calibration. Everything else is inference; this is the measurement.
    let calibrated = false;
    if (me && mySteamID) {
        const c = uidToSteam64(me.uid);
        if (c && c.steam64 === mySteamID) {
            calibrated = true;
            log("     CALIBRATED: my uid " + me.uid + " -> " + c.steam64
                + " == the SteamID64 the Steam API returns for me.");
            log("     => st.Player.uid IS a Steam account id (little-endian hex, 'S' prefix).");
        } else {
            log("     my uid " + me.uid + " -> " + (c ? c.steam64 : "(no conversion)")
                + " does NOT equal my real SteamID " + mySteamID
                + " -> uid is not a Steam account id.");
        }
    } else if (!me) {
        log("     no roster entry flagged isMe — cannot calibrate this tick.");
    } else {
        log("     my real SteamID unavailable — cannot calibrate this tick.");
    }

    const convertible = others.filter(function (r) { return !!uidToSteam64(r.uid); });
    log("     other players visible: " + others.length
        + "   whose uid converts to a SteamID64: " + convertible.length);
    if (calibrated && convertible.length)
        log("     => this client currently holds the Steam identity of "
            + convertible.length + " other player(s).");
    // Class coverage — the number the Social tab's design depends on.
    const withCls = roster.filter(function (r) {
        return r.cls && r.cls.charAt(0) !== "(";
    }).length;
    log("     class known for " + withCls + "/" + roster.length
        + " players (missing = on the layer but entity not streamed in)");
    log("     steam.User objects minted by the client: " + seenUsers.length
        + "   identity API calls: " + steamCallCount);
}

function hookByFindex(findex, label, argIsBytes) {
    if (findex == null || !base) return;
    try {
        const addr = base.add(findex * 8).readPointer();
        if (!addr || addr.isNull()) return;
        Interceptor.attach(addr, {
            onEnter: function () {
                if (argIsBytes) noteSteamCall(label, this.context.rcx);
                else noteSteamCallInt(label, this.context.rcx.toUInt32());
            }
        });
        log("hooked " + label + " (findex " + findex + ")");
    } catch (e) { log("could not hook " + label + ": " + e); }
}

// For steam.User instance methods `this`(rcx) is the wrapper, whose first
// field is the raw SteamID — so the identity is read off the object rather
// than guessed out of an argument register whose meaning varies per method.
function hookThisIsUser(findex, label) {
    if (findex == null || !base || B.SteamUser.uid == null) return;
    try {
        const addr = base.add(findex * 8).readPointer();
        if (!addr || addr.isNull()) return;
        Interceptor.attach(addr, {
            onEnter: function () {
                let uidPtr = null, nm = null;
                try {
                    const self = this.context.rcx;
                    if (self && !self.isNull() && typeName(self) === "steam.User") {
                        uidPtr = self.add(B.SteamUser.uid).readPointer();
                        if (B.SteamUser.cachedName != null)
                            nm = hlStr(self.add(B.SteamUser.cachedName).readPointer());
                        if (seenUsers.indexOf(self.toString()) < 0)
                            seenUsers.push(self.toString());
                    }
                } catch (e) {}
                noteSteamCall(label + (nm ? "  personaName=" + nm : ""), uidPtr);
            }
        });
        log("hooked " + label + " (findex " + findex + ")");
    } catch (e) { log("could not hook " + label + ": " + e); }
}

// getID32 returns the 32-bit account id in rax — the cleanest possible read of
// a SteamID, no encoding guesswork at all. Hooked separately, on RETURN.
function hookGetID32(findex) {
    if (findex == null || !base) return;
    try {
        const addr = base.add(findex * 8).readPointer();
        if (!addr || addr.isNull()) return;
        Interceptor.attach(addr, {
            onLeave: function (ret) {
                noteSteamCallInt("steam.User.getID32 -> ", ret.toInt32());
            }
        });
        log("hooked steam.User.getID32 return (findex " + findex + ")");
    } catch (e) { log("could not hook getID32: " + e); }
}

function hookExport(sym, label) {
    try {
        const m = Process.findModuleByName("steam.hdll");
        if (!m) return;
        const f = m.findExportByName(sym);
        if (!f || f.isNull()) { log("  (" + sym + " not exported)"); return; }
        Interceptor.attach(f, {
            onEnter: function () { noteSteamCall(label, this.context.rcx); }
        });
        log("hooked steam.hdll!" + sym);
    } catch (e) { log("could not hook " + sym + ": " + e); }
}

function main() {
    base = findTableBase(resolveAnchors());
    if (!base) { log("!! functions_ptrs table not found"); return; }
    log("table base " + base);
    log("offsets: Player.uid@" + B.Player.uid + " Player.name@" + B.Player.name
        + " Player.layer@" + B.Player.layer + " Player.user@" + B.Player.user
        + " Player.heroData@" + B.Player.heroData
        + " GameLayer.players@" + B.GameLayer.players
        + " HeroData.accountID@" + B.HeroData.accountID);

    // Path 2: the client minting a steam.User for somebody, then using it.
    hookByFindex(B.fn.fromUID, "steam.$User.fromUID", true);
    hookByFindex(B.fn.fromUID32, "steam.$User.fromUID32", false);  // arg is an i32 id
    for (const m in B.userMethods) {
        if (m === "getID32") continue;            // hooked on return instead
        hookThisIsUser(B.userMethods[m], "steam.User." + m);
    }
    hookGetID32(B.userMethods.getID32);
    // Path 3: the Steam API being asked about an account, from anywhere.
    hookExport("steam_get_user_name", "steam_get_user_name");
    hookExport("steam_request_user_information", "steam_request_user_information");
    hookExport("steam_get_user_avatar", "steam_get_user_avatar");

    const camFi = DATA.cam_targets && DATA.cam_targets["client.BaseCamera.postUpdate"];
    if (camFi == null) { log("!! postUpdate findex missing"); return; }
    let due = true;
    setInterval(function () { due = true; }, 2000);
    Interceptor.attach(base.add(camFi * 8).readPointer(), {
        onEnter: function () {
            if (!due) return;
            due = false;
            fetchMySteamID();
            refreshLocalHero();
            if (!localHero) return;
            walk();
            if (!armed) {
                armed = true;
                log(">>> PROBE ARMED <<< reading the player roster. "
                    + "Stand somewhere with other players in view.");
            }
        }
    });
    setInterval(report, 5000);
}

setTimeout(main, 150);
