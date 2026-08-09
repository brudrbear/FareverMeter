# Critter collecting

> Map settings that turn a zone full of frogs into a list of what you still need.

The game calls them Companions — frogs, rabbits, squirrels, goats. They are
drawn as **green pawprints**, not red dots: they were previously drawn as
enemies, which they are not.

## The one setting that matters

On the **Map** tab, the Critters row cycles through three states:

`Critters: all` → `Critters: only uncollected` → `Critters: hidden`

**Only uncollected is the one to use while collecting.** Your account's caught
companions are replicated to the client, so the meter knows exactly which kinds
you have already netted. Set it, and the map draws only the critters you still
need — and a catch takes its dot off the map within moments.

`Critters: hidden` clears a zone carpeted in frogs off the map without hiding
anything that can actually hurt you.

Until the collection has been read — a few seconds after the meter attaches —
everything shows rather than hiding the world. A collection it has not read yet
and a collection you have finished look identical from here, and hiding the
world would be the worse mistake.

## Critters have no distance limit

Unlike enemies, critters are swept **without one**. A critter three hills away
still reaches the map, as far out as the game will tell the client about it.

That is what makes zooming out worth doing while collecting: turn the Zoom
slider up and the uncollected filter on, and the map becomes a list of what is
left in the zone.

## Sparkling critters

The game flags a handful of units as **sparkling** — the rare variants. 36 units
carry the flag, ten of them critters.

Any sparkling unit is drawn **larger with a gold halo**, whatever it is, so it
stands out among its ordinary siblings.

**Sparkly Tracker** (on the General tab, on by default) is narrower on purpose:
it points at **sparkling critters only**. The other sparkling units are rare
variants of ordinary mobs — a Sparkling Builder is a bee — and they are things
you meet by walking into them, not things you cross a zone to find. Having the
panel announce one made it useless for what it is for.

They still get the halo on the map, because marking something you can see is
information; sending you after it is not.

The tracker puts a small panel on screen whenever one is in range, with an arrow
that turns with your camera exactly as the minimap does — they are computed from
the same projection, so the arrow and the map marker can never disagree.

The two distances are separate on purpose: something 20 units away and 40 below
is not 45 units of walking, it is a different floor.

**How far it reaches is the game's decision, not the meter's.** The client is
only told about entities near enough to matter to it, and that is the real limit
on "scan the whole map".

## Suggested setup

* Critters: **only uncollected**
* Sparkly Tracker: **on**
* Zoom: well out — critters have no distance cap, so the extra range is free
* Enemies: **hidden**, if you are collecting rather than fighting
