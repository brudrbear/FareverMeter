"""
event_parser.py — Farever ff 05 combat-event parser.

Format (decoded against session_20260521-154545.bin with known values 37, 18):

    ff 05 [u64 target] 10 00 00 00
    04 [len] "<EventName>\0"            ← anchor
    01 [u64 source] ff 05 [u64 actor]
    [6-byte subtype constant]
    [u64 target_again] 01 [u64 source_again] [u64 actor_again]
    01 [len] "<DamageType>\0"
    00                                  padding
    [u8 seq_byte]                       1-indexed tick/hit counter
    [4-byte float-LE]                   ★ the value (applied amount)
    [trailer: 8 bytes flags/zeros]

The parser anchors on the event-name string (most reliable), walks back 14
bytes for ff 05, then walks forward to find the damage-type string and the
value. Tolerates minor offset variations.

Usage:
    from event_parser import scan_events
    for ev in scan_events(payload):
        print(ev.name, ev.dmg_type, ev.value, ev.source, ev.target, ev.actor)
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

# Event-name strings observed in real captures. Add new ones as they're seen.
# Used for inventory/labeling — parser anchors on the regex below.
KNOWN_EVENT_NAMES = {
    "Damage",
    "Heal",
    "AreaHeal",
    "BonusDamage",
    "Projectile",
    "Attack",
    "OnHit",
    "OnWellTimed",
    "ApplyStun",
    "Code",
    "Hit",
    "Stun",
    "SunlightBuff",
    "AdditionalArea",
    "Trigger",
    "WaterOrb",
    "StartBaseArea",
    "StopStep",
}

KNOWN_DAMAGE_TYPES = {
    "Magic", "Physical", "Fire", "Spark", "Earth",
    "Water", "Faith", "Light", "Raw", "Cheese", "None",
}

# Event names that affect a combat meter (we count these and sum their values).
COMBAT_EVENT_NAMES = {
    "Damage": "damage",
    "BonusDamage": "damage",
    "Projectile": "damage",   # tentative — needs validation
    "Heal": "heal",
    "AreaHeal": "heal",
}

# Anchor patterns
EVENT_NAME_RE = re.compile(rb"\x04([\x04-\x20])([A-Za-z0-9_]+)\x00")
DAMAGE_TYPE_RE = re.compile(rb"\x01([\x04-\x10])([A-Za-z]+)\x00")

# Subtype constants — observed at a fixed offset within each event regardless
# of whether the event-name string is present.
SUBTYPES = {
    b"\x36\x00\x00\x00\x0a\x03": "Damage",
    b"\x33\x00\x00\x00\x0a\x03": "Heal",
    b"\x31\x00\x00\x00\x0a\x03": "Heal",     # Raw heal variant
    b"\x32\x00\x00\x00\x0a\x03": "Heal",     # another heal variant — verify later
}

# Buff name patterns that indicate absorb shields. The buff arrives as a
# length-prefixed name inside a buff application packet. We detect by
# substring match for these tokens.
ABSORB_BUFF_TOKENS = (
    "ShieldOfSpark",
    "Spark_Shield",
    "ShieldOfFire",
    "ShieldOfFaith",
    "ShieldOfWater",
    "Abyssal",          # Abyssal Rise — observed in earlier captures
    "Shield_Orbit",     # Water shield variants from this user's class
)

# Regex matching length-prefixed buff/status names. Format is `01 [len] [ascii]`
# where len = char_count + 1 (no trailing null for long buff names — verified
# against `01 1a Mage_ShieldOfSpark_Status` which has 25 chars + len 0x1a).
# We use this to scan for buff names; the parser caller filters by
# ABSORB_BUFF_TOKENS and validates length.
BUFF_NAME_RE = re.compile(rb"\x01([\x10-\x40])([A-Za-z_][A-Za-z0-9_]+)")


@dataclass
class Event:
    """One parsed combat event."""
    name: str          # event name string ("Damage", "Heal", ...)
    dmg_type: str      # damage/heal type ("Magic", "Physical", "Raw", ...)
    value: float       # the float-LE value (applied amount, usually)
    target: int        # u64 target entity ID
    source: int        # u64 source entity ID
    actor: int         # u64 actor entity ID (= source for direct, summon for proxy)
    seq: int           # sequence byte (1-indexed tick/hit counter)
    offset: int        # byte offset of event-name string within payload
    raw_after: bytes   # raw bytes after the value field (for debugging)


def scan_events(payload: bytes) -> list[Event]:
    """Find all parseable ff 05 combat events in a buffer.

    Anchored exclusively on the subtype constants (`36 00 00 00 0a 03` for
    Damage, `33 00 00 00 0a 03` etc. for Heal). The post-subtype block carries
    the canonical (source, target, damage_type, value) so this is the only
    reliable anchor. Form A (anchor on the event-name string) was earlier
    found to read source/target from a different field that disagrees with
    the subtype block — discarded.

    When the event has an event-name string preceding the subtype, we use that
    name; otherwise we fall back to the subtype's default name ("Damage" or
    "Heal").
    """
    events: list[Event] = []
    seen_offsets: set[int] = set()

    for sub_bytes, default_name in SUBTYPES.items():
        pos = 0
        while True:
            p = payload.find(sub_bytes, pos)
            if p == -1:
                break
            pos = p + 1
            # Pick the event name: prefer a `04 [len] [ascii] \0` string within
            # 40 bytes before the subtype (e.g., "BonusDamage", "Projectile",
            # "AreaHeal"). Otherwise default to the subtype's name.
            name = default_name
            for m in EVENT_NAME_RE.finditer(payload, max(0, p - 40), p):
                try:
                    candidate = m.group(2).decode("ascii", errors="ignore")
                except Exception:
                    continue
                length_byte = m.group(1)[0]
                if length_byte != len(candidate) + 1:
                    continue
                # Only override when the name belongs to the same family as
                # the subtype (Damage subtype → Damage/BonusDamage/Projectile;
                # Heal subtype → Heal/AreaHeal).
                if default_name == "Damage" and candidate in ("Damage", "BonusDamage", "Projectile"):
                    name = candidate
                elif default_name == "Heal" and candidate in ("Heal", "AreaHeal"):
                    name = candidate

            ev = _try_parse_from_subtype(payload, p, name)
            if ev is None:
                continue
            if any(abs(ev.offset - o) < 10 for o in seen_offsets):
                continue
            events.append(ev)
            seen_offsets.add(ev.offset)

    events.sort(key=lambda e: e.offset)
    return events


def _try_parse_from_event_name(buf: bytes, ev_off: int, name: str, length_byte: int) -> Event | None:
    """Parse one event anchored at ev_off (position of the 0x04 type byte)."""
    # Walk back 14 bytes for ff 05 [u64 target]
    ff05_off = ev_off - 14
    if ff05_off < 0 or buf[ff05_off:ff05_off + 2] != b"\xff\x05":
        return None
    try:
        target = struct.unpack_from("<Q", buf, ff05_off + 2)[0]
    except struct.error:
        return None

    # Walk forward through: 04 [len] name \0 padding-null 01 [u64 source]
    pos = ev_off + 2 + length_byte
    # There's usually a single padding null between the event-name string and the
    # 0x01 source-tag. Tolerate 0-2 nulls.
    while pos < len(buf) and buf[pos] == 0x00 and pos - (ev_off + 2 + length_byte) < 3:
        pos += 1
    if pos >= len(buf) or buf[pos] != 0x01:
        # Some events embed a different structure here; not parseable as combat
        return None
    pos += 1
    if pos + 8 > len(buf):
        return None
    source = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8

    # ff 05 [u64 actor]
    if pos + 10 > len(buf) or buf[pos:pos + 2] != b"\xff\x05":
        return None
    actor = struct.unpack_from("<Q", buf, pos + 2)[0]
    pos += 10

    # 6-byte subtype constant + u64 target_again + 01 + u64 source_again + u64 actor_again
    # Then 01 [len] damage_type \0
    # Just scan forward for the next damage-type string (within 64 bytes).
    scan_end = min(len(buf), pos + 64)
    dt_match = DAMAGE_TYPE_RE.search(buf, pos, scan_end)
    if dt_match is None:
        return None
    dt_length = dt_match.group(1)[0]
    dt_name = dt_match.group(2).decode("ascii")
    if dt_length != len(dt_name) + 1:
        return None

    # After the damage-type string + null:
    # 1 padding-null byte, then 1 sequence byte, then 4 float bytes
    after_dt = dt_match.start() + 2 + dt_length
    # The format is `[00][seq_byte][float-LE]` based on gold-reference rec#1160.
    if after_dt + 6 > len(buf):
        return None
    # Tolerate the leading null being absent in some variants
    pad = buf[after_dt]
    if pad == 0x00:
        seq = buf[after_dt + 1]
        val_off = after_dt + 2
    else:
        seq = pad
        val_off = after_dt + 1

    if val_off + 4 > len(buf):
        return None
    value = struct.unpack_from("<f", buf, val_off)[0]
    # Sanity-check value: combat values are positive, finite, and rarely huge.
    if not (0.0 <= value < 1e7):
        return None

    raw_after = buf[val_off + 4:min(len(buf), val_off + 4 + 16)]
    return Event(
        name=name,
        dmg_type=dt_name,
        value=value,
        target=target,
        source=source,
        actor=actor,
        seq=seq,
        offset=ev_off,
        raw_after=bytes(raw_after),
    )


@dataclass
class AbsorbBuff:
    """A detected absorb-shield buff application."""
    name: str           # internal name e.g. "Mage_ShieldOfSpark_Status"
    source: int         # u64 caster
    target: int         # u64 buff target (the player or sub-entity)
    offset: int         # byte offset of the buff-name string in the payload


@dataclass
class ShieldValue:
    """A current-shield-value broadcast (ff 01 [eid] 10 [float])."""
    entity: int
    value: float
    offset: int


def scan_absorb_buffs(payload: bytes) -> list[AbsorbBuff]:
    """Find absorb-shield buff applications anywhere in the payload."""
    found = []
    seen_offsets: set[int] = set()
    for m in BUFF_NAME_RE.finditer(payload):
        length_byte = m.group(1)[0]
        raw_name = m.group(2)
        # Take exactly `length_byte - 1` chars (no trailing null in some variants)
        expected_chars = length_byte - 1
        if expected_chars < 9 or expected_chars > 60:
            continue
        if len(raw_name) < expected_chars:
            continue
        name = raw_name[:expected_chars].decode("ascii", errors="ignore")
        if not any(tok in name for tok in ABSORB_BUFF_TOKENS):
            continue
        if m.start() in seen_offsets:
            continue
        seen_offsets.add(m.start())
        # Walk back from the name to find a `01 [u64 source]` block followed
        # by 1-2 flag bytes. Conservative: take the closest u64 within 16 bytes
        # preceding the name's length byte.
        scan_start = max(0, m.start() - 32)
        # Heuristic: source is typically 12-22 bytes before the name
        source = 0
        target = 0
        for back in range(8, 24):
            off = m.start() - back
            if off < 0:
                break
            v = struct.unpack_from("<Q", payload, off)[0]
            if 1000 <= v <= 100_000_000:
                source = v
                # Target is sometimes another u64 right before the source — walk back further
                for back2 in range(off - 32, off - 8):
                    if back2 < 0:
                        continue
                    v2 = struct.unpack_from("<Q", payload, back2)[0]
                    if 1000 <= v2 <= 100_000_000 and v2 != v:
                        target = v2
                        break
                break
        found.append(AbsorbBuff(name=name, source=source, target=target, offset=m.start()))
    return found


def scan_shield_values(payload: bytes) -> list[ShieldValue]:
    """Find `ff 01 [u64 entity] 10 [float-LE]` shield-value broadcasts.

    Not strictly absorb-only — selector byte 0x10 is used for other stats too —
    but the shield value is the dominant single-byte-`10` selector use we've
    observed. Caller should track values over time per entity and look for
    decreases to attribute to absorbed damage.
    """
    found = []
    pos = 0
    while True:
        p = payload.find(b"\xff\x01", pos)
        if p == -1:
            break
        pos = p + 1
        if p + 15 > len(payload):
            continue
        # selector must be a single byte 0x10, NOT part of a 4-byte `80 10 00 00` selector
        if payload[p + 10] != 0x10:
            continue
        # The byte right before the selector should be the high byte of the u64
        # entity id, which we already know is 0 (8-byte u64 with small id).
        # The 4 bytes after the selector are the float.
        try:
            value = struct.unpack_from("<f", payload, p + 11)[0]
        except struct.error:
            continue
        # Sanity check: real shield values are positive and < a few thousand
        if not (0.0 < value < 1e5):
            continue
        entity = struct.unpack_from("<Q", payload, p + 2)[0]
        found.append(ShieldValue(entity=entity, value=value, offset=p))
    return found


def _try_parse_from_subtype(buf: bytes, sub_off: int, ev_name: str) -> Event | None:
    """Parse a Form B event anchored at the subtype constant.

    Layout from the subtype onward:
        [subtype 6 bytes]
        [u64 target_again] 01 [u64 source] [u64 actor]
        01 [len] "<DamageType>\\0"
        00 [u8 seq] [4-byte float-LE]
    """
    # Walk back from subtype: there's a u64 (target) before it, then ff 05
    # somewhere before. Conservatively look only at the 32 bytes prior for ff 05.
    ff05_off = None
    target = None
    # The target u64 sits right before the subtype (in Form B captures observed)
    if sub_off >= 8:
        target_alt = struct.unpack_from("<Q", buf, sub_off - 8)[0]
    else:
        target_alt = None
    # Also try walking back further looking for an ff 05 marker
    search_start = max(0, sub_off - 32)
    p = buf.rfind(b"\xff\x05", search_start, sub_off)
    if p != -1 and p + 10 <= sub_off:
        ff05_off = p
        target = struct.unpack_from("<Q", buf, p + 2)[0]
    elif target_alt is not None and 1000 <= target_alt < 1 << 40:
        target = target_alt

    # After subtype: [u64 target_again] 01 [u64 source] [u64 actor]
    pos = sub_off + 6
    if pos + 8 > len(buf):
        return None
    target_again = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8
    if pos >= len(buf) or buf[pos] != 0x01:
        return None
    pos += 1
    if pos + 8 > len(buf):
        return None
    source = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8
    if pos + 8 > len(buf):
        return None
    actor = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8

    # Next: 01 [len] [type] \0
    scan_end = min(len(buf), pos + 32)
    dt_match = DAMAGE_TYPE_RE.search(buf, pos, scan_end)
    if dt_match is None:
        return None
    dt_length = dt_match.group(1)[0]
    dt_name = dt_match.group(2).decode("ascii")
    if dt_length != len(dt_name) + 1:
        return None

    after_dt = dt_match.start() + 2 + dt_length
    if after_dt + 6 > len(buf):
        return None
    pad = buf[after_dt]
    if pad == 0x00:
        seq = buf[after_dt + 1]
        val_off = after_dt + 2
    else:
        seq = pad
        val_off = after_dt + 1

    if val_off + 4 > len(buf):
        return None
    value = struct.unpack_from("<f", buf, val_off)[0]
    if not (0.0 <= value < 1e7):
        return None

    raw_after = buf[val_off + 4:min(len(buf), val_off + 4 + 16)]
    return Event(
        name=ev_name,
        dmg_type=dt_name,
        value=value,
        target=target if target is not None else target_again,
        source=source,
        actor=actor,
        seq=seq,
        offset=dt_match.start(),  # use damage-type-string position as event offset
        raw_after=bytes(raw_after),
    )


def get_entity_id_from_tx(tx_payload: bytes) -> int | None:
    """Extract the entity ID referenced in a TX packet header.

    TX format observed: `82 [XX] 01 [u64 entity_id] ...`
    The u64 at offset 3 is the entity-id the packet pertains to. For movement
    or rotation packets this is the player's own ID; for action/target packets
    it can be the entity being targeted. Use detect_player_id() to aggregate
    across the session and find the most-frequent (which should be the player).
    """
    if len(tx_payload) < 11:
        return None
    if tx_payload[0] != 0x82:
        return None
    if tx_payload[2] != 0x01:
        return None
    return struct.unpack_from("<Q", tx_payload, 3)[0]


def detect_player_id(tx_payloads: list[bytes]) -> tuple[int | None, list[tuple[int, int]]]:
    """Pick the player's main entity ID by frequency of u64 at TX offset 3.

    The player's own ID appears in keep-alive and rotation packets continuously,
    so it should dominate the histogram. Targets and sub-entities appear less
    often.

    Returns (best_id, ranked_list) where ranked_list is [(u64, count), ...]
    sorted descending. best_id is None if no candidate found.
    """
    from collections import Counter
    counter: Counter[int] = Counter()
    for payload in tx_payloads:
        eid = get_entity_id_from_tx(payload)
        if eid is not None:
            counter[eid] += 1
    if not counter:
        return None, []
    ranked = counter.most_common(10)
    return ranked[0][0], ranked


def detect_player_block(tx_payloads: list[bytes],
                        cluster_radius: int = 1000) -> tuple[int | None, set[int]]:
    """Identify the cluster of entity IDs that belong to the player.

    Players have multiple sub-entities (control entity, casting entity, ability
    sub-entities, etc.) that all sit within a small range of each other. The
    "player block" is the set of IDs in TX traffic that cluster within
    `cluster_radius` of the most-frequent ID.

    Returns (top_id, block_set). block_set is empty if no candidates.
    """
    from collections import Counter
    counter: Counter[int] = Counter()
    for payload in tx_payloads:
        eid = get_entity_id_from_tx(payload)
        if eid is not None:
            counter[eid] += 1
    if not counter:
        return None, set()
    top_id, top_count = counter.most_common(1)[0]
    # Include any TX-observed ID within cluster_radius of the top and with at
    # least 5% of the top's frequency (to filter rare incidental references).
    threshold = max(3, top_count // 20)
    block = {eid for eid, n in counter.items()
             if abs(eid - top_id) <= cluster_radius and n >= threshold}
    return top_id, block


# Backwards-compat alias (other callers may use the old name)
get_player_id_from_tx = get_entity_id_from_tx
