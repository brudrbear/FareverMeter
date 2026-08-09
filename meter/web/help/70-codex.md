# Codex and hunting log

> Map settings that show you what you still owe the codex, and what the popups mean.

Your codex is per character, and the game replicates it to the client — so the
meter knows exactly which mobs you have finished and which you have not.

## The map filter

On the **Map** tab, the Enemies row cycles through three states:

`Enemies: all` → `Enemies: only missing from codex` → `Enemies: hidden`

**Only missing from codex** is the interesting one. Set it and the map draws
only the mobs that still owe you codex progress. Anything mastered drops off,
and so do the 23 mobs the game marks as having no codex entry at all — training
dummies and the like.

Walking a zone with it on shows you what is left to hunt instead of a wall of
red dots.

Until the meter has heard from the game it shows everything rather than an empty
map, for the same reason the critter filter does.

This filter is **separate from the popups below** and keeps working whether or
not you have those switched on.

## What the popups say

Two of them, on their own line under the boss kill time:

* **Every kill that counts** shows a running total — `Skunk — Codex 7/8`. The
  number on the right is the *next* rank, not the final one, so it tells you how
  close the next tick is. On the last stretch the wording changes to
  `Skunk — Codex Mastery 12/20`, so you can tell at a glance whether it is worth
  staying. Mobs with no codex entry say nothing at all.
* **Finishing an entry** puts `CODEX COMPLETE — SKUNK` up in gold and plays a
  cue.
* **After that it becomes a tally** — `47th Skunk masterfully slain`. The count
  the game keeps is a *lifetime* total per mob type and carries on climbing long
  after the codex has stopped caring, so a finished entry reports the running
  total instead of repeating `20/20` forever.

**Boss kills show that number too**, under the kill time — `Boss slain 12
times`. Only for a single boss: when several are pulled together there is no
honest single number for it, so it says nothing rather than something wrong.

## How many kills a rank takes

Entries run to three ranks, and the thresholds depend on the mob:

* **1 / 8 / 20** for ordinary mobs
* **1 / 4 / 10** for the big ones — ogres and the like
* **one kill** for elites and named mobs, which master outright

The meter reads those from the game's own data and cross-checks them against
your actual rank, so if a mob's group is ever miscategorised, the game wins and
the count shown stays right.

## Turning the popups off

**Codex alerts** on the General tab covers both — the running count and the
completion fanfare — because they are one thing to want or not want. The map
filter above is deliberately separate.

The completion cue rides the single **Enable sounds** setting like every other
cue; there is no separate switch.

Worth saying: the game already shows its own quiet notifications for these
moments. These are the loud version, not a new capability.

## Suggested setup

* Enemies: **only missing from codex**
* Codex alerts: **on** — the running count is what tells you whether to stay
* Critters: **hidden**, so pawprints are not competing with what you are hunting
