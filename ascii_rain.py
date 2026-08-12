#!/usr/bin/env python3
"""ascii-rain — a terminal rain screensaver in pure Python stdlib.

One file, no dependencies. Columns of glyphs fall at their own speeds, the
leading cell burns bright, the tail fades out behind it. Any key exits.

Design constraints worth knowing before editing:

* Only the stdlib. `curses` ships with CPython on Unix; that is the whole
  dependency list, and it is why this file should still run in five years.
* The terminal must be restored on every exit path. `curses.wrapper` handles
  the normal ones; SIGHUP, SIGQUIT and SIGTERM are turned into exceptions so
  they unwind through the same restore.
* No terminal capability is assumed. A terminal without a hideable cursor, or
  without colour, loses that feature and keeps the program.
* Bad input never reaches curses. Arguments are parsed and validated first, so
  a typo exits with one line on stderr instead of a half-initialised terminal.
"""

import argparse
import curses
import locale
import random
import signal
import sys
import time
import unicodedata

__version__ = "1.0.0"

PROG = "ascii-rain"
USAGE_PROG = "python3 ascii_rain.py"

# Frame rate is fixed; --speed scales how fast drops fall, not how often the
# screen is redrawn. Decoupling them keeps the animation smooth at every speed.
FPS = 30.0

# Rows per second at --speed 1, before each column's own random factor. Slower
# than this and the glyph churn reads louder than the falling, which turns the
# rain into a static field that twinkles.
BASE_ROWS_PER_SECOND = 4.5

# Chance per frame that a live cell swaps its glyph, at --speed 1 and above.
# Scaled down with --speed so slow rain does not become a strobe.
CHURN = 0.22

SPEED_MIN, SPEED_MAX = 0.1, 10.0

# Half-width katakana and digits: every glyph here is one cell wide, which is
# what keeps the columns aligned. Full-width kana would render two cells wide
# and shear the field. `blocks` is the exception, and --help says so.
CHARSETS = {
    "matrix": "ｦｧｨｩｪｫｬｭｮｯｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789",
    "ascii": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()-=+[]{};:,.<>/?",
    "binary": "01",
    "blocks": "░▒▓█",
}

# name -> (head colour, body colour, tail colour). Curses gives eight reliable
# colours; brightness comes from A_BOLD and A_DIM on top of them.
COLORS = {
    "green": (curses.COLOR_WHITE, curses.COLOR_GREEN, curses.COLOR_GREEN),
    "amber": (curses.COLOR_WHITE, curses.COLOR_YELLOW, curses.COLOR_YELLOW),
    "ice": (curses.COLOR_WHITE, curses.COLOR_CYAN, curses.COLOR_CYAN),
    "mono": (curses.COLOR_WHITE, curses.COLOR_WHITE, curses.COLOR_WHITE),
}

PAIR_HEAD, PAIR_BODY, PAIR_TAIL = 1, 2, 3


class Interrupted(Exception):
    """A fatal signal, re-raised so the terminal restore path still runs."""


class Parser(argparse.ArgumentParser):
    """argparse, but a mistake is one line on stderr instead of a usage dump."""

    def error(self, message):
        sys.stderr.write("%s: %s (try --help)\n" % (PROG, message))
        raise SystemExit(2)


def build_parser():
    p = Parser(
        prog=USAGE_PROG,
        description="Matrix-style rain for your terminal. Any key exits.",
        epilog="Example: %s --charset binary --color ice --speed 2" % USAGE_PROG,
    )
    p.add_argument(
        "--speed",
        metavar="FLOAT",
        default="1.0",
        help="fall speed multiplier, %.1f-%.1f (default: 1.0)"
        % (SPEED_MIN, SPEED_MAX),
    )
    p.add_argument(
        "--charset",
        metavar="NAME",
        default="matrix",
        help="glyph pool: %s, or custom:<chars> (default: matrix); blocks can "
        "render double-width where ambiguous-width glyphs are treated as wide"
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
    # NaN fails this comparison, which is the right answer for NaN.
    if not (SPEED_MIN <= value <= SPEED_MAX):
        parser.error(
            "--speed must be between %.1f and %.1f, got %g"
            % (SPEED_MIN, SPEED_MAX, value)
        )
    return value


def usable_glyph(ch):
    """One printable, one-cell-wide, non-blank character.

    A double-width glyph in a custom pool overwrites the neighbouring column
    and shears the whole field; a pool of spaces silently draws nothing at
    all. Both used to be accepted, and both looked like a broken program.
    """
    if not ch.isprintable() or ch.isspace():
        return False
    if unicodedata.combining(ch):
        return False
    return unicodedata.east_asian_width(ch) not in ("W", "F")


def parse_charset(raw, parser):
    if raw.startswith("custom:"):
        wanted = raw[len("custom:") :]
        glyphs = "".join(dict.fromkeys(c for c in wanted if usable_glyph(c)))
        if not glyphs:
            parser.error(
                "--charset custom: needs at least one printable, single-width, "
                "non-blank character"
            )
        return glyphs
    if raw not in CHARSETS:
        parser.error(
            "--charset %r is not one of: %s, custom:<chars>"
            % (raw, ", ".join(sorted(CHARSETS)))
        )
    return CHARSETS[raw]


def parse_color(raw, parser):
    if raw not in COLORS:
        parser.error("--color %r is not one of: %s" % (raw, ", ".join(sorted(COLORS))))
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
        # The trail is stored as a fraction of the screen, not a row count, so
        # a resize rescales it in both directions. Storing rows meant one
        # shrink stunted every column that recycled while the screen was small
        # — permanently, because growing back never lengthened anything.
        self.fraction = random.uniform(1.0 / 6.0, 1.0)
        self.clip(height)
        self.speed = speed * random.uniform(0.5, 1.7) * BASE_ROWS_PER_SECOND
        self.churn = CHURN * min(1.0, speed)
        self.cells = {}
        # Only some columns start on screen. Warm-starting all of them opens
        # at nearly twice the steady-state density, which reads as a burst
        # that then thins out rather than as rain that was always falling.
        if warm_start and random.random() < 0.55:
            # Drop into the middle of the screen *with* a trail. A head and no
            # trail reads as white confetti, which is what the first seconds of
            # every run and every resize used to look like.
            self.head = random.uniform(0, height)
            top = int(self.head) - self.length + 1
            for row in range(top, int(self.head) + 1):
                if 0 <= row < height:
                    self.cells[row] = random.choice(self.glyphs)
        else:
            # Stagger new columns above the screen so they never fall as one
            # flat wave.
            self.head = -random.uniform(0, height * 1.5)

    def clip(self, height):
        """Re-derive the trail length from the screen height, both ways."""
        self.length = max(3, min(int(self.fraction * height), max(4, height)))

    def advance(self, height):
        previous = int(self.head)
        self.head += self.speed / FPS
        current = int(self.head)
        for row in range(previous + 1, current + 1):
            if 0 <= row < height:
                self.cells[row] = random.choice(self.glyphs)
        if self.cells and random.random() < self.churn:
            row = random.choice(list(self.cells))
            # Re-pick if the draw matched what is already there: with a
            # two-glyph pool like `binary`, half of every column's shimmer was
            # a glyph being replaced by itself.
            glyph = random.choice(self.glyphs)
            if len(self.glyphs) > 1 and glyph == self.cells[row]:
                glyph = random.choice(self.glyphs.replace(glyph, "") or self.glyphs)
            self.cells[row] = glyph
        # Cells that have fallen out of the trail are dead; drop them so the
        # dict cannot grow without bound on a long-running screensaver.
        cutoff = int(self.head) - self.length
        for row in [r for r in self.cells if r < cutoff or r >= height]:
            del self.cells[row]
        return self.head - self.length > height

    def draw(self, win, x, height, width, bold_body):
        head_row = int(self.head)
        for row, glyph in self.cells.items():
            distance = head_row - row
            if distance < 0 or distance >= self.length:
                continue
            if distance == 0:
                attr = curses.color_pair(PAIR_HEAD) | curses.A_BOLD
            elif bold_body and distance <= max(1, self.length // 5):
                attr = curses.color_pair(PAIR_BODY) | curses.A_BOLD
            elif distance <= self.length * 0.6:
                attr = curses.color_pair(PAIR_BODY)
            else:
                attr = curses.color_pair(PAIR_TAIL) | curses.A_DIM
            _put(win, row, x, glyph, attr, height, width)


def _put(win, y, x, glyph, attr, height, width):
    """Write one cell, including the bottom-right one curses refuses to fill.

    Writing the last cell of the last line scrolls or raises on most curses
    builds. `insstr` writes it without advancing the cursor, which is the
    portable way in — and on a 1x1 terminal it is the only cell there is.
    """
    if not (0 <= y < height and 0 <= x < width):
        return
    try:
        if y == height - 1 and x == width - 1:
            win.insstr(y, x, glyph, attr)
        else:
            win.addstr(y, x, glyph, attr)
    except curses.error:
        # A narrow terminal can still refuse a write mid-resize. A missing
        # glyph is not worth tearing the screen down for.
        pass


def init_colors(name):
    """Set up the three colour pairs. False if the terminal has no colour.

    False is not an error: uninitialised pairs render in the terminal's default
    foreground, and A_BOLD/A_DIM still separate head from body from tail. A
    monochrome terminal gets monochrome rain.
    """
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

    Rebuilding every column on a resize blanked the screen and restarted the
    rain — at the one moment the user is definitely looking. Surviving columns
    keep falling; only the new ones are born.
    """
    field = list(previous[: max(1, width)]) if previous else []
    while len(field) < max(1, width):
        field.append(Drop(height, glyphs, speed, warm_start))
    for drop in field:
        drop.clip(height)
    return field


def run(stdscr, glyphs, speed, color):
    try:
        curses.curs_set(0)
    except curses.error:
        # No `civis` capability (vt100, ansi, xterm-mono, dumb...). A visible
        # cursor parked in a corner is a blemish; refusing to start is not an
        # option. This one unguarded call used to kill the program outright.
        pass
    try:
        # Otherwise ncurses waits a full second on Escape to see whether an
        # escape sequence follows — and Escape is the key people press to leave.
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    try:
        # cbreak leaves tty flow control on, so Ctrl-S froze the program and
        # Ctrl-Q was swallowed — two keys that did not exit, from a program
        # that promises any key does. raw() hands every byte straight through.
        curses.raw()
    except curses.error:
        pass
    stdscr.nodelay(True)
    has_color = init_colors(color)
    head_fg, body_fg, _ = COLORS[color]
    # With one colour for both, a bold body tier is indistinguishable from the
    # head, so mono (and any colourless terminal) drops that tier.
    bold_body = has_color and head_fg != body_fg

    height, width = stdscr.getmaxyx()
    field = build_field(width, height, glyphs, speed, warm_start=True)
    frame = 1.0 / FPS
    next_frame = time.monotonic()

    while True:
        if SHUTDOWN:
            # A signal fired and something ate the exception on its way out
            # (curses.wrapper has a bare `except` of its own). Leave anyway.
            return
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
            # Whatever else was typed or pasted is still in the tty buffer;
            # without this it lands on the user's shell prompt — and runs.
            curses.flushinp()
            return

        stdscr.erase()
        for x, drop in enumerate(field):
            if x >= width:
                break
            if drop.advance(height):
                drop.reset(height, speed)
            drop.draw(stdscr, x, height, width, bold_body)
        stdscr.refresh()

        next_frame += frame
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            # Fell behind (a resize storm, a suspended process). Re-anchor
            # rather than sprinting to catch up on a backlog of frames.
            next_frame = time.monotonic()


CATCHABLE = ("SIGINT", "SIGQUIT", "SIGHUP", "SIGTERM")

# Set by the first fatal signal. Read by the frame loop, so a shutdown still
# happens even if the exception is swallowed on its way out.
SHUTDOWN = False


def install_signal_handlers():
    """Turn fatal signals into an exception so the terminal is restored.

    Ctrl-\\ (SIGQUIT) killed the process outright, leaving the user on the
    alternate screen with echo off, needing `reset` to get their shell back.
    SIGHUP did the same. SIGKILL cannot be caught and never will be.

    Only the first signal raises. Two arriving in the same interpreter tick —
    `^C^\\` pasted as one write, or a supervisor sending SIGINT then SIGTERM —
    used to raise the second exception *inside* the restore the first one had
    started, which left the terminal exactly as broken as no handler at all.
    Later signals return instead, so the restore always finishes.
    """

    def handler(signum, frame):
        global SHUTDOWN
        if SHUTDOWN:
            # Already unwinding. Raising again here would land inside the
            # restore the first signal started, which is the whole bug. Do
            # nothing and let the first exception finish its work.
            return
        SHUTDOWN = True
        raise Interrupted(signum)

    for name in CATCHABLE:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def terminal_encoding():
    """The encoding curses will actually put on the wire."""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        return locale.nl_langinfo(locale.CODESET) or "ascii"
    except (AttributeError, ValueError):
        return getattr(sys.stdout, "encoding", None) or "ascii"


def representable(glyphs, encoding, parser, charset_name):
    """Drop glyphs the terminal cannot encode, and say so out loud.

    Under a non-UTF-8 locale — `LC_ALL=C`, which is what cron, some CI runners
    and `sudo` hand you — curses silently emits a space for every glyph it
    cannot encode. `blocks` drew a wholly blank screen and `matrix` quietly
    became a field of digits, both with no message and exit 0.
    """
    keep = ""
    for ch in glyphs:
        try:
            ch.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            continue
        keep += ch
    if not keep:
        parser.error(
            "this terminal's encoding (%s) cannot render the %s glyphs - try "
            "--charset ascii or --charset binary, or a UTF-8 locale"
            % (encoding, charset_name)
        )
    if len(keep) < len(glyphs):
        sys.stderr.write(
            "%s: %d of %d %s glyphs are not representable in %s; drawing with "
            "the rest.\n" % (PROG, len(glyphs) - len(keep), len(glyphs),
                             charset_name, encoding)
        )
    return keep


def main(argv=None):
    # Before anything else: a signal during argument parsing should not be able
    # to outrun the handler that makes it exit cleanly.
    install_signal_handlers()
    try:
        return _main(argv)
    except (KeyboardInterrupt, Interrupted):
        # Everything from argument parsing to teardown is inside this, because
        # a signal at any of those moments used to print a traceback.
        return 0


def _main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    speed = parse_speed(args.speed, parser)
    glyphs = parse_charset(args.charset, parser)
    color = parse_color(args.color, parser)
    glyphs = representable(
        glyphs, terminal_encoding(), parser,
        args.charset if args.charset in CHARSETS else "custom pool",
    )

    if not sys.stdout.isatty():
        sys.stderr.write(
            "%s: stdout is not a terminal - there is nothing to draw on.\n" % PROG
        )
        return 2
    if not sys.stdin.isatty():
        # With no tty on stdin no keypress can ever arrive, so "any key exits"
        # would be a lie and the only way out would be a signal.
        sys.stderr.write(
            "%s: stdin is not a terminal - no keypress could reach it.\n" % PROG
        )
        return 2

    try:
        curses.wrapper(run, glyphs, speed, color)
    except curses.error as exc:
        sys.stderr.write("%s: terminal error: %s\n" % (PROG, exc))
        return 2
    finally:
        # curses.wrapper only restores if its own `stdscr` name got bound. A
        # signal landing in the ~1 ms inside initscr() beats that binding, and
        # the screen is already in raw mode by then: exit 0, no output, and a
        # shell that needs `reset`. This is the backstop for that window.
        try:
            if not curses.isendwin():
                curses.endwin()
        except curses.error:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
