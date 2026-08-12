# ascii-rain

Matrix-style rain for your terminal, in one file of pure Python stdlib.

![screenshot](screenshot.png)

*Above: the default `matrix` charset in green, twenty seconds into a run on a
100×30 terminal — 54 column heads at full brightness, each trailing a body and a
dim tail. Every glyph, position and brightness level is the program's own output,
replayed through a terminal emulator and painted to a PNG by
`tools/screenshot.py`; the hues are that script's choice, because in real use they
are your terminal's. Run the tool to regenerate it.*

## What it does

Fills your terminal with columns of falling glyphs. Each column runs at its own
speed and length, the leading cell burns bright, and the trail fades out behind
it. Any key exits — including Escape, which takes about 60 ms, not the full
second ncurses waits by default.

Three flags, plus the usual `--help` and `--version`:

- `--speed FLOAT` — fall speed multiplier, `0.1` to `10.0` (default `1.0`).
- `--charset NAME` — `matrix` (half-width katakana and digits, the default),
  `ascii`, `binary`, `blocks`, or `custom:<chars>` for your own glyph pool.
- `--color NAME` — `green` (default), `amber`, `ice`, `mono`.

Resizing the window mid-run is handled live: the columns already falling keep
falling, only the new ones are born, and trail lengths rescale with the new
height. A terminal with no colour renders in your default foreground instead of
failing, and so does one that cannot hide its cursor — `vt100`, `ansi`,
`xterm-mono` and `dumb` all run.

The terminal is put back the way it was on every exit path that can be caught:
a keypress, `Ctrl-C`, `Ctrl-\`, `SIGTERM`, `SIGHUP`. `SIGKILL` cannot be caught
by anything, so that one is on you. Leftover input is flushed on the way out, so
a paste that happens to start with a key does not end up running in your shell.

Bad input — an unknown flag, a speed of `99`, a charset that does not exist —
prints one line and exits `2`, never a traceback. A `custom:` pool is filtered to
printable, single-width, non-blank characters, since a double-width glyph shears
the column grid and a pool of spaces draws nothing at all. The four named
charsets are single-width except `blocks`, whose shading glyphs are
"ambiguous-width" and will render double on terminals configured to treat them
that way.

It needs a terminal on both stdin and stdout — redirect either and it says so
and exits `2`, rather than drawing to a file or running with no way out.

Requires Python 3.8 or newer. It uses the standard library's `curses` module,
which ships with CPython on Linux and macOS but **not** on Windows — on Windows
it will not run without a third-party curses build, and adding one would mean a
dependency this project does not want.

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

There is nothing to install. No `pip install`, no `requirements.txt`, no build
step. (`tools/screenshot.py` is the one exception, and it is a development tool,
not the program: regenerating the image above needs `pyte` and `pillow`.)

## Why it exists

A seeded idea from the build factory's warm-start queue
([#11](https://github.com/yinggarykairui/factory-hub/issues/11)) — a screensaver
worth writing because it is the smallest program that has to get three unglamorous
things right at once: terminal restoration, live resize, and never crashing on
input it did not expect.

---

*Day 019 of an autonomous build factory — [factory-hub](https://github.com/yinggarykairui/factory-hub)*
