# Minimap and compass options

> What the map draws, how far it reaches, and which knobs are worth touching.

A square map of what is around you, drawn from the game's own entity lists:
players, mobs, chests, orbs, soulstones, obelisks, respawn points and
activities. Group members get a blue ring; other players are chevrons pointing
where they are facing, so you can read which way a group is heading.

Hovering names the state — a chest you cannot open yet reads `Chest · Locked`.

## Style: Rotating or Fixed

**Rotating** (the default) turns the map with the **camera**, so the top of the
map is whatever you are looking at. That is deliberately not where your
character is pointing, which changes constantly as it turns to face things.

**Fixed** keeps the map still and turns the arrow instead.

## Zoom, and what it is really for

At 100% the map reaches 175 world units from the centre out. The slider takes it
down to 80u and **out to 1750u** — roughly nine times the area it used to cover.

That far end exists for **chests and orbs**, which are swept from the whole
layer at any distance. The only thing that ever stopped you seeing a distant one
was how far the map could zoom out.

Two honest caveats:

* **Enemies stop where the map does.** They are the one category with a distance
  limit, deliberately — and it moves out with the zoom, so there is no ring of
  missing mobs at the rim.
* **Zooming out is not a treasure map.** The game only keeps a handful of chests
  loaded at a time — four or five, in a zone with 47 of them. You see the ones
  the client has been told about, not every chest in the zone. If nothing is
  showing, usually everything currently loaded is already looted.

## Icon scale

Scales every marker through one multiplier, so an obelisk stays larger than a
chest and a foe dot stays smaller. The sizes are tuned against each other and
this preserves that.

## Markers settle before they appear

The game's entity lists churn at distance — things arrive for a single frame, or
drop out for a second and come back. A marker waits a quarter-second before it
is drawn, and lingers a couple of seconds after it stops being reported, so what
you see holds still.

Enemies are exempt: a mob appearing late is worse than a mob flickering.

## Chests you have opened drop off

Shut ones stay. Worth knowing why that took two goes: a chest's state does not
change when you loot it — it reads `Closed` either way, and that means "shut",
not "been here". What the meter watches is the chest's *visual* state.

## The compass

The same categories as a strip across the top of the screen, filtered
separately. Useful when you want a heading rather than a map.

## Moving the map

It has no title bar. The hover box under it only appears while the mouse is
free, and **that box is the drag handle**. Its position is remembered, and it
has its own Show / Hide setting on the Windows tab.
