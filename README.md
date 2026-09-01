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

**Getting out.** Every key exits, including the ones a terminal usually eats.
The tty runs in raw mode, so Ctrl-S and Ctrl-Q reach the program rather than
freezing it — and so do `Ctrl-C` and `Ctrl-\`, which arrive as ordinary bytes
rather than as signals and exit the way every other key does. `SIGINT`,
`SIGQUIT`, `SIGHUP` and `SIGTERM` *sent from elsewhere* — `kill`, a supervisor,
a terminal closing — are caught instead, and put the terminal back the way they
found it even when two arrive at once. `SIGKILL` cannot be caught by anything,
so that one is on you. Whatever was still in the input buffer is flushed on the
way out, so a paste that happens to start with a key does not end up running in
your shell.

**Exit status.** A keypress exit is `0`, and that is the only thing `0` means.
`Ctrl-C` and `Ctrl-\` typed at the keyboard are keypresses here, not signals, so
they exit `0` as well. The statuses a shell reports for those two belong to the
signals themselves, which reach this program only when something else sends
them: a signal is re-raised with its default handler once the terminal is back,
so a shell or a supervisor sees the process killed by it — `130` for `SIGINT`,
`143` for `SIGTERM`, `129` for `SIGHUP`, and `131` for `SIGQUIT`, which keeps its
default disposition and will write a core file on a system that has them
enabled. Bad input is `2`.

**The startup window.** A few milliseconds at the very beginning belong to
CPython rather than to this program, and `SIGINT` is the signal that notices.
While the interpreter is still importing its own `site` module — between about
five and fifteen milliseconds after `exec` here, before the first line of
`ascii_rain.py` runs — a `SIGINT` does not become `130`. It aborts the
interpreter with `Fatal Python error: init_import_site` and exit `1`, with a
traceback on stderr; signalling at a random point in the first 30 ms, 5 runs in
30 landed there. Nothing in the script can fix that, because no script is
running yet. Nothing is left broken either: the screen has not been touched.
`SIGTERM` has no such window — 30 runs in 30 killed the process correctly — so a
caller that needs certainty at startup should send `SIGTERM`, or `SIGKILL`.
Earlier still, in the gap between a launcher's `fork` and its `exec`, the signal
is not this process's at all: if the launcher is itself a Python program, its own
handler absorbs the `SIGINT` and `exec` throws the record away, and the rain
keeps falling.

**Terminals.** A terminal with no colour renders in your default foreground
instead of failing, and so does one that cannot hide its cursor — `vt100`,
`ansi` and `xterm-mono` all run. Those three have no `dim` attribute at all, and
the Linux console has one its terminfo entry forbids combining with colour
(`ncv#18`), which comes to the same thing. On all four the tail takes its step
down from density instead of brightness: about half the cells of the trailing
section are not drawn, and the trail still steps down twice. Each cell decides
once whether it survives that far, so the tail dissolves rather than flickers. A
terminal with no cursor addressing at all — `TERM=dumb` — cannot be animated by
anything, so the program says so in one line and exits `2` without writing to
the screen. An unset `TERM` and an empty one each get their own line.

**Window size.** The size comes from the terminal. ncurses reads `COLUMNS` and
`LINES` ahead of it, so a value bigger than the real window is dropped here —
`COLUMNS=9999 LINES=9999`, which scripts and CI export routinely, otherwise
hangs the program before it owns an exit path. A smaller value is kept, since
drawing into part of a window is a thing people ask for. The field is also
capped at 1000 columns by 400 rows, which is past what any terminal window
reaches.

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
warning. Emoji are not part of that trade and are rejected as double-width, even
the ones Unicode's width table calls neutral, and so is a lone variation
selector such as U+FE0F, which would draw as an invisible cell.

**Arguments.** `--` is the end-of-options marker and is consumed like one:
`python3 ascii_rain.py --speed 2 --` runs. The program takes no positional
arguments, so anything after `--` is an error naming the offending word — the
same one line, in the same wording, for a stray word anywhere on the command
line. Unambiguous abbreviations work (`--sp 2` is `--speed 2`) and behave
exactly like the spelling they stand for. `--help` and `--version` print and
exit `0`, but not over the top of a mistake standing next to them.

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
it. Both scripts take `--help`.

The screenshot tool needs a monospace font in regular and bold and a CJK font
for the half-width katakana. It looks in the usual Linux and macOS locations;
`python3 tools/screenshot.py --check-fonts` prints what it resolved, and
`ASCII_RAIN_FONT_MONO`, `ASCII_RAIN_FONT_MONO_BOLD` and `ASCII_RAIN_FONT_CJK`
override any of the three. If a font is missing it names the one it could not
find and exits `2`.

Assets: `screenshot.png` is painted by `tools/screenshot.py` from the program's
own output; the glyph outlines in it come from DejaVu Sans Mono and DejaVu Sans
Mono Bold (Bitstream Vera Fonts License) and Noto Sans CJK (SIL Open Font
License 1.1).

---

*Day 019 of an autonomous build factory — [factory-hub](https://github.com/yinggarykairui/factory-hub)*
