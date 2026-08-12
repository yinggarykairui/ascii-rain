#!/usr/bin/env python3
"""ascii-rain — a terminal rain screensaver in pure Python stdlib.

One file, no dependencies. Columns of glyphs fall at their own speeds, the
leading cell burns bright, the tail fades out behind it. Any key exits.

Design constraints worth knowing before you edit:

* Only the stdlib. `curses` ships with CPython on Unix; that is the whole
  dependency list, and it is why this file will still run in five years.
* The terminal must be restored on every exit path, including a crash. That
  is what `curses.wrapper` buys us, so every curses call lives under it.
* Bad input never reaches curses. Arguments are parsed and validated first,
  so a typo exits with one line on stderr instead of a half-initialised
  terminal.
"""

import argparse
import curses
import random
import sys
import time

__version__ = "1.0.0"

PROG = "ascii-rain"

# Frame rate is fixed; --speed scales how fast drops fall, not how often we
# redraw. Decoupling them keeps the animation smooth at every speed.
FPS = 30.0

SPEED_MIN, SPEED_MAX = 0.1, 10.0

# Half-width katakana and digits: every glyph here is one cell wide, which is
# what keeps the columns aligned. Full-width kana would render two cells wide
# and shear the field.
CHARSETS = {
    "matrix": "ｦｧｨｩｪｫｬｭｮｯｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789",
    "ascii": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()-=+[]{};:,.<>/?",
    "binary": "01",
    "blocks": "░▒▓█▄▀■□▪▫",
}

# name -> (head colour, body colour, tail colour). Curses gives us eight
# reliable colours; brightness comes from A_BOLD and A_DIM on top of them.
COLORS = {
    "green": (curses.COLOR_WHITE, curses.COLOR_GREEN, curses.COLOR_GREEN),
    "amber": (curses.COLOR_WHITE, curses.COLOR_YELLOW, curses.COLOR_YELLOW),
    "ice": (curses.COLOR_WHITE, curses.COLOR_CYAN, curses.COLOR_BLUE),
    "mono": (curses.COLOR_WHITE, curses.COLOR_WHITE, curses.COLOR_WHITE),
}

PAIR_HEAD, PAIR_BODY, PAIR_TAIL = 1, 2, 3


class Parser(argparse.ArgumentParser):
    """argparse, but a mistake is one line on stderr instead of a usage dump."""

    def error(self, message):
        sys.stderr.write("%s: %s (try --help)\n" % (PROG, message))
        raise SystemExit(2)


def build_parser():
    p = Parser(
        prog=PROG,
        description="Matrix-style rain for your terminal. Any key exits.",
        epilog="Example: %s --charset binary --color ice --speed 2" % PROG,
    )
    p.add_argument(
        "--speed",
        metavar="FLOAT",
        default="1.0",
        help="fall speed multiplier, %g-%g (default: 1.0)" % (SPEED_MIN, SPEED_MAX),
    )
    p.add_argument(
        "--charset",
        metavar="NAME",
        default="matrix",
        help="glyph pool: %s, or custom:<chars> (default: matrix)"
        % ", ".join(sorted(CHARSETS)),
    )
    p.add_argument(
        "--color",
        metavar="NAME",
        default="green",
        help="palette: %s (default: green)" % ", ".join(sorted(COLORS)),
    )
    p.add_argument("--version", action="version", version="%s %s" % (PROG, __version__))
    return p


def parse_speed(raw, parser):
    try:
        value = float(raw)
    except ValueError:
        parser.error("--speed wants a number, got %r" % raw)
    if not (SPEED_MIN <= value <= SPEED_MAX):
        parser.error(
            "--speed must be between %g and %g, got %g" % (SPEED_MIN, SPEED_MAX, value)
        )
    return value


def parse_charset(raw, parser):
    if raw.startswith("custom:"):
        glyphs = raw[len("custom:") :]
        # Newlines and tabs would move the cursor rather than paint a cell.
        glyphs = "".join(dict.fromkeys(c for c in glyphs if c.isprintable()))
        if not glyphs:
            parser.error("--charset custom: needs at least one printable character")
        return glyphs
    if raw not in CHARSETS:
        parser.error(
            "--charset %r is not one of: %s, custom:<chars>"
            % (raw, ", ".join(sorted(CHARSETS)))
        )
    return CHARSETS[raw]


def parse_color(raw, parser):
    if raw not in COLORS:
        parser.error(
            "--color %r is not one of: %s" % (raw, ", ".join(sorted(COLORS)))
        )
    return raw


class Drop:
    """One falling column of glyphs.

    `head` is a float row so speed is smooth between frames; only its integer
    part decides which cells are lit. Glyphs are generated once per cell and
    then occasionally churned, which is what makes the trail shimmer.
    """

    def __init__(self, height, glyphs, speed, warm_start=False):
        self.glyphs = glyphs
        self.reset(height, speed, warm_start)

    def reset(self, height, speed, warm_start=False):
        self.length = random.randint(max(3, height // 6), max(4, height))
        self.speed = speed * random.uniform(0.45, 1.6) * FPS / 12.0
        # Stagger the field so it never starts as one flat wave.
        self.head = -random.uniform(0, height * 1.5) if not warm_start else \
            random.uniform(0, height)
        self.cells = {}

    def advance(self, height):
        previous = int(self.head)
        self.head += self.speed / FPS
        current = int(self.head)
        for row in range(previous + 1, current + 1):
            if 0 <= row < height:
                self.cells[row] = random.choice(self.glyphs)
        # Churn a live cell now and then so the tail is never static.
        if self.cells and random.random() < 0.28:
            row = random.choice(list(self.cells))
            self.cells[row] = random.choice(self.glyphs)
        # Cells that have fallen out of the trail are dead; drop them so the
        # dict cannot grow without bound on a long-running screensaver.
        cutoff = int(self.head) - self.length
        for row in [r for r in self.cells if r < cutoff or r >= height]:
            del self.cells[row]
        return self.head - self.length > height

    def draw(self, win, x, height, width):
        head_row = int(self.head)
        for row, glyph in self.cells.items():
            distance = head_row - row
            if distance < 0 or distance >= self.length:
                continue
            if distance == 0:
                attr = curses.color_pair(PAIR_HEAD) | curses.A_BOLD
            elif distance <= max(1, self.length // 5):
                attr = curses.color_pair(PAIR_BODY) | curses.A_BOLD
            elif distance <= self.length * 0.6:
                attr = curses.color_pair(PAIR_BODY)
            else:
                attr = curses.color_pair(PAIR_TAIL) | curses.A_DIM
            _put(win, row, x, glyph, attr, height, width)


def _put(win, y, x, glyph, attr, height, width):
    """Write one cell, ignoring the bottom-right corner curses refuses to fill.

    Writing the last cell of the last line scrolls or raises on most curses
    builds; there is no portable way to do it, so that one cell stays dark.
    """
    if not (0 <= y < height and 0 <= x < width):
        return
    if y == height - 1 and x == width - 1:
        return
    try:
        win.addstr(y, x, glyph, attr)
    except curses.error:
        # A narrow terminal can still refuse a write mid-resize. A missing
        # glyph is not worth tearing the screen down for.
        pass


def init_colors(name):
    """Set up the three pairs. Returns True if the terminal gave us colour."""
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return False
    if not curses.has_colors() or curses.COLORS < 8:
        return False
    head, body, tail = COLORS[name]
    for pair, fg in ((PAIR_HEAD, head), (PAIR_BODY, body), (PAIR_TAIL, tail)):
        try:
            curses.init_pair(pair, fg, -1)
        except curses.error:
            return False
    return True


def build_field(width, height, glyphs, speed, warm_start, previous=None):
    """Grow or shrink the field, keeping the drops that already exist.

    A resize that rebuilt every column would blank the screen and start the
    rain over as one flat wave — the one moment the illusion is most visible.
    Surviving columns keep falling; only the new ones are born.
    """
    field = list(previous[: max(1, width)]) if previous else []
    while len(field) < max(1, width):
        field.append(Drop(height, glyphs, speed, warm_start))
    for drop in field:
        # A taller trail than the screen would never clear; a shorter screen
        # also means the old trail length no longer makes sense.
        drop.length = min(drop.length, max(4, height))
    return field


def run(stdscr, glyphs, speed, color):
    curses.curs_set(0)
    stdscr.nodelay(True)
    if not init_colors(color):
        # No colour available: the mono palette is monochrome by definition,
        # so falling back costs nothing but the hue. Not an error (§8).
        init_colors("mono")

    height, width = stdscr.getmaxyx()
    field = build_field(width, height, glyphs, speed, warm_start=True)
    frame = 1.0 / FPS
    next_frame = time.monotonic()

    while True:
        key = stdscr.getch()
        if key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            field = build_field(
                width, height, glyphs, speed, warm_start=True, previous=field
            )
            continue
        if key != -1:
            return

        stdscr.erase()
        for x, drop in enumerate(field):
            if x >= width:
                break
            if drop.advance(height):
                drop.reset(height, speed)
            drop.draw(stdscr, x, height, width)
        stdscr.refresh()

        next_frame += frame
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            # Fell behind (a resize storm, a suspended process). Re-anchor
            # rather than sprinting to catch up on a backlog of frames.
            next_frame = time.monotonic()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    speed = parse_speed(args.speed, parser)
    glyphs = parse_charset(args.charset, parser)
    color = parse_color(args.color, parser)

    if not sys.stdout.isatty():
        sys.stderr.write(
            "%s: stdout is not a terminal — there is nothing to draw on.\n" % PROG
        )
        return 2

    try:
        curses.wrapper(run, glyphs, speed, color)
    except curses.error as exc:
        sys.stderr.write("%s: terminal error: %s\n" % (PROG, exc))
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
