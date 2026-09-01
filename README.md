# ascii-rain

Matrix-style rain for your terminal, in one file of pure Python stdlib.

![screenshot](screenshot.png)

*The default `matrix` charset in green, twenty seconds into a run on a 100×30
terminal. Every glyph, position and brightness level in that image is the
program's own output — `tools/screenshot.py` replays it through a terminal
emulator and paints it to a PNG, because the hues in real use are your
terminal's, not this repo's.*

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

**Exit status.** A keypress exit is `0`, and that is the only thing `0` means. A
signal is re-raised with its default handler once the terminal is back, so your
shell and any supervisor see the process killed by that signal rather than
exiting cleanly: `130` for Ctrl-C, `143` for `SIGTERM`, `129` for `SIGHUP`, and
`131` for `Ctrl-\`, which keeps its default disposition and will write a core
file on a system that has them enabled. Bad input is `2`. One window belongs to
nobody here: a signal that lands in the first few tens of milliseconds, before
the interpreter reaches `main()`, is CPython's to handle and not this program's —
the terminal is untouched at that point, but the exit is CPython's, not ours.

**Terminals.** A terminal with no colour renders in your default foreground
instead of failing, and so does one that cannot hide its cursor — `vt100`,
`ansi` and `xterm-mono` all run. Those three also have no `dim` attribute, so
the tail takes its step down from density instead of brightness: about half the
cells of the trailing section are not drawn, and the trail still steps down
twice. Each cell decides once whether it survives that far, so the tail
dissolves rather than flickers. A terminal with no cursor addressing at all —
`TERM=dumb` — cannot be animated by anything, so the program says so in one line
and exits `2` without writing to the screen.

**Encoding.** Under a non-UTF-8 locale — `LC_ALL=C`, which is what cron and some
CI runners hand you — glyphs that cannot be encoded are dropped, with a line on
stderr saying how many. If nothing is left, it says so and exits `2` rather than
drawing a blank screen.

**Glyph pools.** A `custom:` pool is filtered to printable, non-blank and
not double-width, since a double-width glyph shears the column grid and a pool of
spaces draws nothing at all. Three of the four `blocks` glyphs are
"ambiguous-width" and will render double on terminals configured to treat them
that way; the other named charsets are single-width throughout. A `custom:` pool
accepts ambiguous-width characters too — Cyrillic, Greek, `①`, the box-drawing
set — and they will shear the field on a CJK-configured terminal for the same
reason. They are allowed because the filter that would reject `①` rejects
Cyrillic and Greek with it, which is the worse trade; this paragraph is the
warning.

**Arguments.** `--` is the end-of-options marker and is consumed like one:
`python3 ascii_rain.py --speed 2 --` runs. The program takes no positional
arguments, so anything after `--` is an error naming the offending word.

**Requirements.** Python 3.8 or newer, and a terminal on both stdin and stdout —
redirect either and it says so and exits `2`, rather than drawing to a file or
running with no way out. On Python 3.8 Escape takes ncurses' default second to
register, because `curses.set_escdelay` arrived in 3.9; on 3.9 and newer it exits
in about 60 ms like every other key.

`curses` ships with CPython on Linux and macOS but **not** on Windows — there it
will not run without a third-party curses build, and adding one would mean a
dependency this project does not want.

**Development tools.** Two scripts under `tools/`, neither of them part of the
program. `tools/screenshot.py` regenerates `screenshot.png` from a real run and
wants `pip install pyte pillow`. `tools/checks.py` drives the program under a
pseudo-terminal and checks the things a person cannot check by looking — exit
status per signal, the `TERM=dumb` refusal, argument handling, and the
no-`dim` tail; it wants `pyte` for the last of those and runs the rest without
it.

The screenshot tool needs a monospace font in regular and bold and a CJK font
for the half-width katakana. It looks in the usual Linux and macOS locations;
`python3 tools/screenshot.py --check-fonts` prints what it resolved, and
`ASCII_RAIN_FONT_MONO`, `ASCII_RAIN_FONT_MONO_BOLD` and `ASCII_RAIN_FONT_CJK`
override any of the three. If a font is missing it names the one it could not
find and exits `2`.

---

*Day 019 of an autonomous build factory — [factory-hub](https://github.com/yinggarykairui/factory-hub)*
