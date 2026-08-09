# The Social tab

> Who is on your shard, and who you have seen this session.

Two views of the same people, and the difference between them matters.

## Current shard

Everyone the game client is holding state for right now. That is **far more
people than are rendered around you** — the client knows about the whole layer,
not just what is on screen.

Because those entities are live, this view can show a **class and a level**
beside each name.

Sort by **Name** for a directory, which keeps you at the top so you can check
the list is about the shard you think it is. Sort by **Level** for a ranking,
which pins nobody — a leaderboard with someone glued to the first row is not a
leaderboard.

## This session

The accumulated log of everyone the meter has seen since it started, with when
you last saw them.

This view deliberately shows **neither class nor level**. Both facts are only
true while a player is on your layer, and a level from twenty minutes ago is
worse than no level at all.

## The Refresh button

Neither page polls. Arriving at the tab loads them, and **Refresh** reloads both
at once — so the count you are looking at is never stale in a way you cannot
fix. The note under the list answers every press, whether or not anything
actually moved.

## Which shard am I on

The bottom of this panel always shows it, next to the Stop button. It is a
generated id like `Spajoda5202_9541_na`, drawn in a monospaced face on purpose:
there are no words in it to recover from a misread glyph, so it has to be a font
that tells an `l` from a `1` and an `O` from a `0`.

Read it out to somebody trying to land on the same shard as you. It changes when
you relog, and holds while you move between zones.

## Copy ID

Each row can copy that player's Steam account id. The game replicates it to
every client, which is simply a fact about how it works — the meter is showing
you something your own client already knows.
