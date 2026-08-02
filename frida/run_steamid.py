"""Host for steamid_probe.js — does this client hold other players' Steam IDs?

    py frida\\run_steamid.py [seconds] [path\\to\\hlboot.dat]

Best run standing in a populated hub so st.GameLayer.players has strangers in
it, not just party members. A party-only sample cannot distinguish "the server
sends identity for people you grouped with" from "the server sends it for
everyone".

What it decides, and how:

  * st.Player.uid and st.player.HeroData.accountID are both hxbit NETWORK
    properties, so the server is able to replicate them. Whether it does — and
    whether the value is a SteamID64 or an internal account id — is measured,
    not assumed, by reading MY OWN uid next to my real SteamID64 from
    steam_get_steam_id(). Matching calibrates the field for everyone else in
    the same list; not matching answers the question in the negative outright.

  * Independently, the three Steam identity entry points (steam.User.fromUID,
    steam_get_user_name, steam_request_user_information, steam_get_user_avatar)
    are hooked, so a SteamID that arrives by some path nobody enumerated still
    shows up.

Every findex and offset is resolved by NAME from the live hlboot.dat.
"""
import json
import sys
import time
from pathlib import Path

import frida

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hltools"))
from hlbc_parser import HLCode, HOBJ, HSTRUCT      # noqa: E402
from gamepath import find_hlboot                   # noqa: E402
from datafresh import assert_resolver_current      # noqa: E402

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0


def build_targets():
    code = HLCode(find_hlboot(argv_index=2)).parse()
    assert_resolver_current(code)
    byname = {t.name: t for t in code.types
              if t.kind in (HOBJ, HSTRUCT) and t.name}

    def offs(cls, *fields):
        t = byname.get(cls)
        if not t:
            return {}
        o = code.field_offsets(t.index)
        return {f: o[f][0] for f in fields if f in o}

    def proto(cls, meth):
        t = byname.get(cls)
        return next((p.findex for p in t.protos if p.name == meth), None) if t else None

    def static_fn(cls, field):
        """findex of a static function field (they live in bindings, not protos).

        A binding's field id indexes the FULL runtime field list, super chain
        included — every `$Static` extends hl.Class, which contributes five
        fields ahead of the class's own. Indexing into t.fields alone silently
        resolves to the wrong field or falls off the end (steam.$User.fromUID
        is binding fid 6, but only field 1 of its own three).
        """
        t = byname.get(cls)
        if not t:
            return None
        ordered = list(code.field_offsets(t.index).keys())
        try:
            fid = ordered.index(field)
        except ValueError:
            return None
        return next((fx for f, fx in t.bindings if f == fid), None)

    b = {
        "Player": offs("st.Player", "uid", "name", "isMe", "lobbyId", "user",
                       "heroData", "group", "layer", "hero"),
        # The Social tab wants a class per player. ent.Hero is the only place
        # it lives (HeroData is null client-side), and ent.Hero is an ENTITY —
        # it may well be absent for players who are on the layer but not
        # streamed in. That coverage is the thing to measure, not assume.
        "Hero": offs("ent.Hero", "kind", "_level", "name"),
        "GameLayer": offs("st.GameLayer", "players"),
        "Group": offs("st.Group", "players"),
        "HeroData": offs("st.player.HeroData", "accountID"),
        "User": offs("mpman.User", "name"),
        "SteamUser": offs("steam.User", "uid", "cachedName"),
        "fn": {
            "fromUID": static_fn("steam.$User", "fromUID"),
            "fromUID32": static_fn("steam.$User", "fromUID32"),
        },
        # Every steam.User instance method takes the wrapper as `this`, and the
        # wrapper's first field IS the raw SteamID. Hooking the whole set means
        # a peer identity is caught no matter which one the game happens to
        # call, without having to guess the construction path.
        "userMethods": {m: proto("steam.User", m) for m in
                        ("requestInformation", "get_name", "getAvatar",
                         "getAvatarImage", "toString", "getID32",
                         "onDataUpdated")},
    }

    # The two fields the whole question rests on. If either is gone the class
    # changed and a silent null would read as "the server withholds it" — which
    # is the wrong conclusion, so fail loudly instead.
    if "uid" not in b["Player"]:
        raise SystemExit("[!] st.Player.uid not found — class changed. "
                         "Re-run hltools\\build_targets.py and re-check.")
    if "players" not in b["GameLayer"]:
        raise SystemExit("[!] st.GameLayer.players not found — cannot enumerate "
                         "the server roster.")
    if "accountID" not in b["HeroData"]:
        print("[!] warning: st.player.HeroData.accountID not found; only "
              "st.Player.uid will be measured.")
    for k in ("layer", "isMe", "heroData"):
        if k not in b["Player"]:
            print(f"[!] warning: st.Player.{k} missing — that column will read "
                  "as absent, not as null.")
    return b


def on_message(message, data):
    if message["type"] == "error":
        print("[JS ERROR]", message.get("description"))
        return
    p = message.get("payload") or {}
    if p.get("kind") == "log":
        print(str(p["msg"]).encode("ascii", "replace").decode(), flush=True)


def main():
    b = build_targets()
    print(f"[*] st.Player.uid@{b['Player'].get('uid')} "
          f"name@{b['Player'].get('name')} layer@{b['Player'].get('layer')} "
          f"heroData@{b['Player'].get('heroData')}")
    print(f"[*] st.GameLayer.players@{b['GameLayer'].get('players')} "
          f"HeroData.accountID@{b['HeroData'].get('accountID')}")
    print(f"[*] steam.$User.fromUID findex={b['fn']['fromUID']} "
          f"fromUID32 findex={b['fn']['fromUID32']} "
          f"steam.User.uid@{b['SteamUser'].get('uid')}")
    print(f"[*] steam.User methods hooked: "
          + ", ".join(f"{k}={v}" for k, v in b["userMethods"].items() if v))

    out = HERE.parent / "analysis_out"
    data = (out / "resolver_data.json").read_text(encoding="utf-8")
    off = (out / "meter_offsets.json").read_text(encoding="utf-8")
    js = (HERE / "steamid_probe.js").read_text(encoding="utf-8")
    src = (f"const DATA = {data};\nconst OFF = {off};\n"
           f"const B = {json.dumps(b)};\n" + js)

    session = frida.attach("Farever.exe")
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print(f"[*] reading for {DURATION:.0f}s. Wait for '>>> PROBE ARMED <<<'. "
          "Ctrl+C to stop early.")
    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\n[*] stopping early.")
    finally:
        # Teardown on Farever is dangerous — unload has wedged and a hard kill
        # has crashed the game. Unload politely, then leave it alone.
        try:
            script.unload()
            session.detach()
        except Exception:
            pass
    print("[done]")


if __name__ == "__main__":
    main()
