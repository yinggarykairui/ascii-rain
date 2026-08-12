# ascii-rain

Matrix-style rain for your terminal, in one file of pure Python stdlib.

![screenshot](screenshot.png)

*Above: a 100×30 terminal running the default `matrix` charset in green, caught
mid-fall — 50 column heads at full brightness, each trailing a body and a dim
tail. Captured from the shipped program, not mocked.*

## What it does

Fills your terminal with columns of falling glyphs. Each column runs at its own
speed and length, the leading cell burns white, and the trail fades out behind
it. Any key exits; so does `Ctrl-C`, and the terminal is put back the way it was
either way.

Three flags:

- `--speed FLOAT` — fall speed multiplier, `0.1` to `10.0` (default `1.0`).
- `--charset NAME` — `matrix` (half-width katakana and digits, the default),
  `ascii`, `binary`, `blocks`, or `custom:<chars>` for your own glyph pool.
- `--color NAME` — `green` (default), `amber`, `ice`, `mono`.

Resizing the window mid-run is handled live: the columns already falling keep
falling, and only the new ones are born. A terminal that reports no colour falls
back to monochrome instead of failing. Bad input — an unknown flag, a speed of
`99`, a charset that does not exist — prints one line and exits `2`, never a
traceback.

Requires Python 3.8 or newer and a terminal. It uses the standard library's
`curses` module, which ships with CPython on Linux and macOS but **not** on
Windows — on Windows it will not run without a third-party curses build, and
adding one would mean a dependency this project does not want.

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

There is nothing to install. There is no `pip install`, no `requirements.txt`,
and no build step.

## Why it exists

A seeded idea from the build factory's warm-start queue
([#11](https://github.com/yinggarykairui/factory-hub/issues/11)) — a screensaver
worth writing because it is the smallest program that has to get three unglamorous
things right at once: terminal restoration, live resize, and never crashing on
input it did not expect.

---

*Day 019 of an autonomous build factory — [factory-hub](https://github.com/yinggarykairui/factory-hub)*
