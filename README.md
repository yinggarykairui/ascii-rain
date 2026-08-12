# ascii-rain

Matrix-style rain for your terminal, in one file of pure Python stdlib.

![screenshot](screenshot.png)

*The default `matrix` charset in green, twenty seconds into a run on a 100×30
terminal: 653 lit cells, 45 of them column heads at full brightness. Every glyph,
position and brightness level in that image is the program's own output —
`tools/screenshot.py` replays it through a terminal emulator and paints it to a
PNG, because the hues in real use are your terminal's, not this repo's.*

## What it does

Fills your terminal with columns of falling glyphs. Each column runs at its own
speed and length, the leading cell burns bright, and the trail fades out behind
it. Any key exits, Escape included.

Three flags, plus the usual `--help` and `--version`:

- `--speed FLOAT` — fall speed multiplier, `0.1` to `10.0` (default `1.0`).
- `--charset NAME` — `matrix` (half-width katakana and digits, the default),
  `ascii`, `binary`, `blocks`, or `custom:<chars>` for your own glyph pool.
- `--color NAME` — `green` (default), `amber`, `ice`, `mono`.

Resizing the window mid-run is handled live: the columns already falling keep
falling, only the new ones are born, and trail lengths rescale with the new
height. Bad input — an unknown flag, a speed of `99`, a charset that does not
exist — prints one line and exits `2`, never a traceback.

## How to run

```
git clone https://github.com/yinggarykairui/ascii-rain.git
cd ascii-rain
python3 ascii_rain.py
```

Or, once you have the file, anywhere:

```
python3 ascii_rain.py --charset binary --color ice --speed 2
```

There is nothing to install: no `pip install`, no `requirements.txt`, no build
step.

## Why it exists

A seeded idea from the build factory's warm-start queue
([#11](https://github.com/yinggarykairui/factory-hub/issues/11)) — a screensaver
worth writing because it is the smallest program that has to get three unglamorous
things right at once: terminal restoration, live resize, and never crashing on
input it did not expect.

## Details

**Getting out.** Every key exits, including the ones a terminal usually eats:
Ctrl-S and Ctrl-Q reach the program rather than freezing it. So do `Ctrl-C` and
`Ctrl-\`, and `SIGTERM` and `SIGHUP` from elsewhere — all of them put the
terminal back the way they found it, even when two arrive at once. `SIGKILL`
cannot be caught by anything, so that one is on you. Whatever was still in the
input buffer is flushed on the way out, so a paste that happens to start with a
key does not end up running in your shell.

**Terminals.** A terminal with no colour renders in your default foreground
instead of failing, and so does one that cannot hide its cursor — `vt100`,
`ansi` and `xterm-mono` all run. On terminals with no `dim` attribute the tail
renders at body brightness, so the trail steps down once instead of twice. On
`TERM=dumb` the program starts and exits cleanly but cannot animate: there is no
cursor addressing to animate with.

**Encoding.** Under a non-UTF-8 locale — `LC_ALL=C`, which is what cron and some
CI runners hand you — glyphs that cannot be encoded are dropped, with a line on
stderr saying how many. If nothing is left, it says so and exits `2` rather than
drawing a blank screen.

**Glyph pools.** A `custom:` pool is filtered to printable, non-blank, non-double-
width characters, since a double-width glyph shears the column grid and a pool of
spaces draws nothing at all. Three of the four `blocks` glyphs are
"ambiguous-width" and will render double on terminals configured to treat them
that way; the other charsets are single-width throughout.

**Requirements.** Python 3.8 or newer, and a terminal on both stdin and stdout —
redirect either and it says so and exits `2`, rather than drawing to a file or
running with no way out. On Python 3.8 Escape takes ncurses' default second to
register, because `curses.set_escdelay` arrived in 3.9; on 3.9 and newer it exits
in about 60 ms like every other key.

`curses` ships with CPython on Linux and macOS but **not** on Windows — there it
will not run without a third-party curses build, and adding one would mean a
dependency this project does not want.

**Regenerating the screenshot.** `tools/screenshot.py` is a development tool, not
part of the program. It needs `pyte` and `pillow`, and it reads DejaVu Sans Mono
and Noto Sans CJK from the paths listed at the top of the file — edit those if
your fonts live elsewhere.

---

*Day 019 of an autonomous build factory — [factory-hub](https://github.com/yinggarykairui/factory-hub)*
