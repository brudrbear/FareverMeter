# Starting it with the game, from Steam

> Let Steam launch both at once and stop thinking about it.

Because the meter waits for Farever, you can have Steam start the pair together.

In Steam, right-click **Farever** → **Properties** → **General** → **Launch
Options**, and paste this on one line:

```
cmd /c start "" "C:\Users\YOU\AppData\Local\Programs\FareverMeter\FareverMeter.exe" & %command%
```

Replace `YOU` with your Windows username.

To get the path exactly right without typing it: find **Farever+ Meter** in the
Start menu, right-click → **More** → **Open file location**, then right-click
the shortcut → **Properties** and copy the **Target** box.

Now launching Farever from Steam starts the meter first, then the game.

## Why it is shaped like that

Steam replaces `%command%` with the game and its arguments, but on Windows it
does not run launch options through a shell — so `cmd /c` is what makes the `&`
mean anything.

`start ""` launches the meter *without waiting* for it, and the empty `""` is
the window title `start` expects before a quoted path. Leave it out and it
treats the path as the title.

Steam then waits on `cmd`, and `cmd` waits on the game, so **playtime, the
Steam overlay and Rich Presence all keep working** — which they do not in the
recipes that leave `start` off the game as well.

## Two things to know

* **When the game closes, the meter notices.** The overlay disappears and a
  small window asks whether to exit the meter too — one click and both are
  gone, so the pair behaves like a single program.
* **Launching Farever outside Steam** — from a desktop shortcut, say — skips
  the launch options entirely, so start the meter yourself that time.

If this feels like more machinery than you want, it is entirely optional. The
meter waits for the game, so starting it whenever you like works just as well.
