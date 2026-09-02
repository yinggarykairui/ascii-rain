#!/usr/bin/env python3
"""ascii-rain — a terminal rain screensaver in pure Python stdlib.

One file, no dependencies. Columns of glyphs fall at their own speeds, the
leading cell burns bright, the tail fades out behind it. Any key exits.

Design constraints worth knowing before editing:

* Only the stdlib. `curses` ships with CPython on Unix; that is the whole
  dependency list, and it is why this file should still run in five years.
* The terminal must be restored on every exit path. `curses.wrapper` handles
  the normal ones; SIGHUP, SIGQUIT and SIGTERM are turned into exceptions so
  they unwind through the same restore — and are then re-raised at self with
  the default handler, so the process is *killed by* the signal it caught and
  exit 0 keeps meaning "a key was pressed".
* No terminal capability is assumed. A terminal without a hideable cursor, or
  without colour, loses that feature and keeps the program.
* Bad input never reaches curses. Arguments are parsed and validated first, so
  a typo exits with one line on stderr instead of a half-initialised terminal.
"""

import argparse
import curses
import locale
import os
import random
import select
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

# The largest grid this program will animate. A terminal window is bounded by
# pixels; COLUMNS and LINES are not, and ncurses believes them over the
# terminal's own ioctl. `COLUMNS=9999 LINES=9999` — which scripts and CI export
# routinely — made initscr() try to allocate a hundred-megacell window and never
# come back: no first frame, no keypress, no SIGINT, no SIGTERM, SIGKILL only,
# and a terminal left in raw mode with the cursor hidden. These numbers are
# larger than an 8K display at a six-pixel font, so no real window reaches them.
MAX_COLS, MAX_ROWS = 1000, 400

# On a terminal with no `dim` capability the tail cannot step down in
# brightness, so it steps down in density instead: this is the chance a cell
# has of still being drawn once it falls past the body tier. Half is enough to
# read as a fainter section without the trail breaking into dashes.
TAIL_SURVIVAL = 0.5

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


def complain(text):
    """One line on stderr, or silence where there is no stderr to write on.

    CPython leaves `sys.stderr` as None when fd 2 was closed at exec — the
    `2>&-` case, which is not `2>/dev/null`: there is no stream object at all.
    Every write in this file assumed one, so the refusal path raised an
    AttributeError of its own and the program exited 1 without a word, from a
    README that promises one line and exit 2. A refusal nobody can hear is
    still a refusal, and the exit status is the part the caller reads.
    """
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(text)
    except (ValueError, OSError):
        # Closed under us, or a pipe with no reader left. Same answer.
        pass


def is_a_terminal(stream):
    """True only if this stream exists and is a terminal.

    `sys.stdout` is None when fd 1 was closed at exec, so `.isatty()` was an
    AttributeError six lines deep instead of the refusal above it. A closed
    stream is at least as absent as a redirected one, and gets the same
    answer.
    """
    try:
        return stream is not None and stream.isatty()
    except (ValueError, OSError):
        return False


class Parser(argparse.ArgumentParser):
    """argparse, but a mistake is one line on stderr instead of a usage dump."""

    def error(self, message):
        complain("%s: %s (try --help)\n" % (PROG, message))
        raise SystemExit(2)


def build_parser(scout=False):
    """The real parser, or a scout of it that cannot exit on its own.

    argparse fires `--help` and `--version` the instant it reaches them, so
    `--version --bogus` printed the version and exited 0 with the mistake
    unread — and so did `--help --bogus`. The scout is the same parser with
    those two turned into ordinary flags that store instead of exiting, which
    makes it safe to run first, purely to find the words this program has no
    use for. Every other spelling, default and abbreviation is identical, so
    what the scout accepts is what the real parser accepts.
    """
    p = Parser(
        prog=USAGE_PROG,
        description="Matrix-style rain for your terminal. Any key exits.",
        epilog="Example: %s --charset binary --color ice --speed 2" % USAGE_PROG,
        add_help=not scout,
    )
    if scout:
        p.add_argument("-h", "--help", action="store_true")
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
    if scout:
        p.add_argument("--version", action="store_true")
    else:
        p.add_argument(
            "--version", action="version", version="%s %s" % (PROG, __version__)
        )
    return p


# The three flags that take a value. A `--` sitting in one of these slots is
# that flag's argument to reject, not an end-of-options marker to consume.
VALUE_FLAGS = ("--speed", "--charset", "--color")


def takes_a_value(arg):
    """True if `arg` is a spelling of a flag that claims the next word.

    argparse accepts any unambiguous prefix of a long option, so `--sp` is
    `--speed` and `--cha` is `--charset`. Matching VALUE_FLAGS by equality saw
    neither, and a `--` sitting in an abbreviated flag's value slot was eaten
    as an end-of-options marker: `--sp -- 2` reported `unexpected argument: 2`
    where `--speed -- 2` correctly reported a missing value. An *ambiguous*
    prefix (`--c`) counts here too — argparse will refuse it by name, and that
    refusal is the truthful one, so nothing behind it should be consumed first.

    `--speed=2` carries its own value and claims nothing.
    """
    if not arg.startswith("--") or "=" in arg:
        return False
    return any(flag.startswith(arg) for flag in VALUE_FLAGS)


# Every flag this program answers to, long spellings first. `takes_a_value`
# only needs the three that claim a word; deciding whether a *value* is really
# a flag needs all of them.
KNOWN_FLAGS = VALUE_FLAGS + ("--help", "--version")


def spells_a_flag(arg):
    """True if argparse would read `arg` as one of this program's own options.

    Abbreviations count, the same way `takes_a_value` counts them, and an
    ambiguous one (`--c`) counts too: argparse refuses it by name and that
    refusal is the truthful one.
    """
    if arg == "-h":
        return True
    if not arg.startswith("--") or arg == "--":
        return False
    name = arg.split("=", 1)[0]
    return any(flag.startswith(name) for flag in KNOWN_FLAGS)


def claims_the_next_word(arg):
    """True if `arg` is a spelling of exactly one value-taking flag.

    `takes_a_value` counts an ambiguous prefix in as well, because nothing
    behind one should be eaten as an end-of-options marker. The question here
    is narrower — whether it is safe to write the next word onto this one —
    and `--c` is argparse's "ambiguous option" to report: attaching there put
    `--c=-x`, a spelling nobody typed, into that message.
    """
    return (takes_a_value(arg)
            and sum(1 for flag in VALUE_FLAGS if flag.startswith(arg)) == 1)


def attach_values(argv):
    """Write a flag's value onto the flag when the value looks like an option.

    argparse lets a value beginning with `-` through only if it matches its
    own negative-number pattern, which is `-5` and `-1.5` and nothing else. So
    `--speed -inf`, `--speed -1e3`, `--speed -2.` and `--charset -x` all came
    back as "expected one argument" — a complaint that the value was missing,
    about a value that was right there — while `--speed -5` and `--speed=-inf`
    named what they refused. Same mistake, three voices, and the two spellings
    of one mistake disagreeing.

    `--speed=-inf` is the spelling argparse always reads literally, so that is
    the spelling every value gets. Two words are left alone: a bare `--`,
    which is this program's end-of-options marker and, in a value slot, the
    missing value argparse reports; and one of this program's own flags, since
    `--speed --color ice` is a forgotten value rather than a speed of
    "--color".
    """
    kept = []
    expecting = False
    for arg in argv:
        if (expecting and arg.startswith("-") and arg != "--"
                and not spells_a_flag(arg)):
            kept[-1] = "%s=%s" % (kept[-1], arg)
            expecting = False
            continue
        kept.append(arg)
        expecting = claims_the_next_word(arg)
    return kept


def consume_end_of_options(argv, parser):
    """Strip a bare `--`, and reject anything behind it.

    `--` means "no more options"; this program takes no positional arguments,
    so the correct reading of `ascii_rain.py --speed 2 --` is "run", and of
    `ascii_rain.py -- x` is "there is no x here". argparse gets neither right
    on its own: it hands the bare marker to its (empty) positional list and
    reports `unrecognized arguments: --`, which names the marker rather than
    the mistake.

    `--charset --` is left alone deliberately. There the `--` is the value slot
    of a flag, so it is argparse's "expected one argument" to report, not a
    marker to eat.
    """
    kept = []
    expecting_value = False
    for index, arg in enumerate(argv):
        if arg == "--" and not expecting_value:
            rest = argv[index + 1:]
            if rest:
                parser.error("unexpected argument: %s" % show(rest[0]))
            return kept
        kept.append(arg)
        expecting_value = takes_a_value(arg)
    return kept


def show(value, limit=48):
    """A value quoted back for an error message, cut short if it is long.

    `repr` escapes control characters, which is what keeps a crafted argument
    from repainting the terminal it is being complained about on. It does not
    bound the length: `--color` with a five-kilobyte word printed five
    kilobytes of it. Past `limit` characters the rest is an ellipsis and a
    count.
    """
    if len(value) > limit:
        return "%r... (truncated, %d characters)" % (value[:limit], len(value))
    return repr(value)


def parse_speed(raw, parser):
    try:
        value = float(raw)
    except ValueError:
        parser.error("--speed wants a number, got %s" % show(raw))
    # NaN fails this comparison, which is the right answer for NaN.
    if not (SPEED_MIN <= value <= SPEED_MAX):
        # Both the word that was typed and the number float() made of it.
        # Python reads `2_0` as twenty, so quoting the word alone produced
        # "must be between 0.1 and 10.0, got '2_0'" — a line arguing against
        # itself, since 2.0 is inside that range.
        parser.error(
            "--speed must be between %.1f and %.1f, got %s (read as %r)"
            % (SPEED_MIN, SPEED_MAX, show(raw), value)
        )
    return value


# The blocks whose characters terminals draw two cells wide because they are
# emoji, whatever Unicode's East_Asian_Width table says. U+1F327 CLOUD WITH
# RAIN is `N` there, and so is every regional indicator, so the width test
# alone let `--charset custom:🌧` through and it sheared the grid with no
# warning. The emoji below these blocks (⌚, ⚡, ⭐) are already `W` in the table
# and were already rejected.
#
# Only the emoji blocks, not the whole of plane 1: the first cut of this fence
# ran 0x1F000-0x1FAFF, which also swallowed U+1F130 (Ambiguous) and U+1F0A1
# (Neutral) — narrow characters that no terminal draws wide, refused by a rule
# whose own comment promised not to touch them. Ambiguous width stays
# untouched, here and everywhere: Cyrillic, Greek, ① and the box-drawing set
# are accepted, which is the trade the README describes.
EMOJI_RANGES = (
    (0x1F1E6, 0x1F1FF),  # regional indicators (flags)
    (0x1F300, 0x1F5FF),  # miscellaneous symbols and pictographs
    (0x1F600, 0x1F64F),  # emoticons
    (0x1F680, 0x1F6FF),  # transport and map symbols
    (0x1F7E0, 0x1F7EB),  # coloured circles and squares
    (0x1F900, 0x1F9FF),  # supplemental symbols and pictographs
    (0x1FA70, 0x1FAFF),  # symbols and pictographs extended-A
)


def usable_glyph(ch):
    """One printable, one-cell-wide, non-blank character.

    A double-width glyph in a custom pool overwrites the neighbouring column
    and shears the whole field; a pool of spaces silently draws nothing at
    all. Both used to be accepted, and both looked like a broken program.
    """
    if not ch.isprintable() or ch.isspace():
        return False
    # Every mark, not just the ones with a non-zero combining class. U+FE0F
    # (variation selector 16) is a mark whose combining class is 0, so the old
    # test passed it and it was drawn as a glyph: hundreds of invisible cells
    # scattered through the field.
    if unicodedata.category(ch).startswith("M"):
        return False
    if any(first <= ord(ch) <= last for first, last in EMOJI_RANGES):
        return False
    return unicodedata.east_asian_width(ch) not in ("W", "F")


def parse_charset(raw, parser, notices):
    if raw.startswith("custom:"):
        wanted = raw[len("custom:") :]
        glyphs = "".join(dict.fromkeys(c for c in wanted if usable_glyph(c)))
        if not glyphs:
            parser.error(
                "--charset custom: needs at least one printable, single-width, "
                "non-blank character"
            )
        # Say what was thrown away. `custom:A🌧B` drew A and B and said
        # nothing at all, which reads as the program ignoring what was asked
        # for. Same shape and same voice as the encoding message below.
        #
        # Held rather than written, because it is an advisory about a pool
        # that may yet turn out never to be used: `--charset custom:<emoji>ab
        # --color nope` printed it *and* the --color refusal - two lines for
        # one bad input, the first about a glyph pool the second throws away.
        # The caller says it once the arguments are known to be good.
        lost = sum(1 for c in wanted if not usable_glyph(c))
        if lost:
            notices.append(
                "%s: %d of %d custom glyphs cannot be drawn in one cell "
                "(double-width, blank, or an invisible mark); drawing with "
                "the rest.\n" % (PROG, lost, len(wanted))
            )
        return glyphs
    if raw not in CHARSETS:
        parser.error(
            "--charset %s is not one of: %s, custom:<chars>"
            % (show(raw), ", ".join(sorted(CHARSETS)))
        )
    return CHARSETS[raw]


def parse_color(raw, parser):
    if raw not in COLORS:
        parser.error(
            "--color %s is not one of: %s"
            % (show(raw), ", ".join(sorted(COLORS)))
        )
    return raw


class Drop:
    """One falling column of glyphs.

    `head` is a float row so speed is smooth between frames; only its integer
    part decides which cells are lit. Glyphs are generated once per cell and
    then occasionally churned, which is what makes the trail shimmer.

    Each cell in `cells` is `(glyph, survives)`. `survives` is the coin flip
    for the no-`dim` tail, taken once when the cell is born and never again:
    re-rolling it per frame turns the tail into a strobe, while deciding once
    lets the trail dissolve as it falls.
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
                    self.spawn(row)
        else:
            # Stagger new columns above the screen so they never fall as one
            # flat wave.
            self.head = -random.uniform(0, height * 1.5)

    def spawn(self, row):
        """Light one cell: a glyph, and its one-time tail coin flip."""
        self.cells[row] = (random.choice(self.glyphs),
                           random.random() < TAIL_SURVIVAL)

    def clip(self, height):
        """Re-derive the trail length from the screen height, both ways."""
        self.length = max(3, min(int(self.fraction * height), max(4, height)))
        # The height this column was last fitted to. `refit` needs the old one
        # to know a shrink from a redraw, and by how much.
        self.height = height

    def refit(self, height):
        """Fit a surviving column to a new screen: the trail, and the floor.

        `clip` alone rescales the trail and leaves `head` on the absolute row
        it was already on, so a shrink strands every column whose head is now
        past the bottom: the whole trail is below the last row, it draws
        nothing at all, and `advance` then recycles it from up to 1.5 screens
        *above* the top. On a 100x30 -> 40x12 shrink that was some 16 of the
        40 surviving columns and the field went from 0.27 lit to 0.08 for two
        to three seconds — at the one moment the user is certainly looking,
        and against a README that promises the columns already falling keep
        falling.

        So the head is rescaled by the ratio the trail length already is, and
        its trail is re-lit around where it lands: `cells` is keyed by
        absolute row, so a head that moved without them would light nothing
        until it had fallen its own length again. Every head moves, not only
        the stranded ones — a column parked 20 rows above a 30-row screen is
        4 seconds away from a 12-row one and 1.6 seconds away once it is
        rescaled too, and leaving those behind traded the empty field for a
        hole a second later. A column keeps its speed, its trail fraction and
        its place in the fall; only the rows it lands on change.
        """
        was = self.height
        self.clip(height)
        if was == height:
            return
        self.head = min(self.head * (float(height) / was), height - 1.0)
        self.cells = {}
        for row in range(int(self.head) - self.length + 1, int(self.head) + 1):
            if 0 <= row < height:
                self.spawn(row)

    def advance(self, height):
        previous = int(self.head)
        self.head += self.speed / FPS
        current = int(self.head)
        for row in range(previous + 1, current + 1):
            if 0 <= row < height:
                self.spawn(row)
        if self.cells and random.random() < self.churn:
            row = random.choice(list(self.cells))
            current_glyph, survives = self.cells[row]
            # Re-pick if the draw matched what is already there: with a
            # two-glyph pool like `binary`, half of every column's shimmer was
            # a glyph being replaced by itself.
            glyph = random.choice(self.glyphs)
            if len(self.glyphs) > 1 and glyph == current_glyph:
                glyph = random.choice(self.glyphs.replace(glyph, "") or self.glyphs)
            # The coin flip rides along unchanged; churn swaps the glyph, not
            # the cell's fate.
            self.cells[row] = (glyph, survives)
        # Cells that have fallen out of the trail are dead; drop them so the
        # dict cannot grow without bound on a long-running screensaver.
        cutoff = int(self.head) - self.length
        for row in [r for r in self.cells if r < cutoff or r >= height]:
            del self.cells[row]
        return self.head - self.length > height

    def draw(self, win, x, height, width, bold_body, dim):
        head_row = int(self.head)
        for row, (glyph, survives) in self.cells.items():
            distance = head_row - row
            if distance < 0 or distance >= self.length:
                continue
            if distance == 0:
                attr = curses.color_pair(PAIR_HEAD) | curses.A_BOLD
            elif bold_body and distance <= max(1, self.length // 5):
                attr = curses.color_pair(PAIR_BODY) | curses.A_BOLD
            elif distance <= self.length * 0.6:
                attr = curses.color_pair(PAIR_BODY)
            elif dim:
                attr = curses.color_pair(PAIR_TAIL) | curses.A_DIM
            elif survives:
                # No `dim` to step down with. The tail is thinned instead: the
                # cells that lost their coin flip are just not drawn once they
                # reach this tier, so the trail still ends fainter than it
                # started. vt100, ansi, xterm-mono and linux all land here.
                attr = curses.color_pair(PAIR_TAIL)
            else:
                continue
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


# Bit 4 of terminfo's `ncv` (no_color_video) is A_DIM: the terminal cannot
# combine dim with colour. The other bits name the other attributes; only this
# one matters here.
NCV_DIM = 16


def has_dim(has_color):
    """True if this terminal can render a dimmer tier *as it will be used*.

    Two ways to lose it, and the second one is why this takes an argument.
    `vt100`, `ansi` and `xterm-mono` have no `dim` string at all. `TERM=linux`
    does have one — and also `ncv#18`, which tells ncurses that dim cannot be
    combined with colour, so ncurses drops A_DIM the moment a colour pair is in
    use and the tail comes out byte-identical to the body: two visible tiers on
    a trail documented to step down twice. Asking terminfo alone got that wrong.

    With no colour there is no pair to combine with, so `ncv` does not apply and
    a dim string is enough.
    """
    try:
        if curses.tigetstr("dim") is None:
            return False
        ncv = curses.tigetnum("ncv")
    except curses.error:
        return False
    # tigetnum answers -1 for an absent capability and -2 for one of the wrong
    # type; neither is a mask.
    if has_color and ncv > 0 and ncv & NCV_DIM:
        return False
    return True


# The window-size variables this program wrote itself, and only those, so that
# taking them back out again cannot touch one the user set on purpose.
PINNED = []


def sane_window_env():
    """Stop COLUMNS and LINES from describing a window that cannot exist.

    ncurses reads both and believes them over the terminal's real size, so a
    stale or invented value is not a hint, it is the screen. Anything wider or
    taller than the terminal actually is has to be a lie, and a big enough lie
    hangs initscr() before the program owns a single exit path.

    Rule: a value that is not a positive whole number, or that is bigger than
    the window the kernel reports, is dropped and ncurses goes back to asking
    the terminal. A *smaller* value is kept — drawing into part of a window is
    a thing people ask for on purpose. With no ioctl answer to check against,
    MAX_COLS/MAX_ROWS stand in as the ceiling.

    Then the same ceiling is put on the window itself. A size arriving by
    ioctl used to bypass this entirely, and `window_size` clips only after
    initscr() has already allocated: at 6000x6000 that was two seconds before
    the first byte and three to eight before a keypress was answered, and at
    32768 cells or more in either dimension ncurses gave up inside initscr()
    with `Error opening terminal: xterm-256color.` and exit 1 — its own
    message, blaming TERM for a window size, and a status this program
    documents nowhere. ncurses believes COLUMNS and LINES over the ioctl, so
    writing the cap into them is how a window is clipped *before* the
    allocation rather than after it.

    A variable written here is written for initscr() alone: it is taken back
    out by `unpin_window_env` the moment the screen exists, because ncurses
    re-reads both on every resize and a dimension left pinned stops following
    the window. Measured: a 1200x450 terminal shrunk to 100x30 went on
    drawing 1000 columns and 400 rows into it.

    Returns the names it dropped, for the caller to say out loud if it wants.
    """
    try:
        real = os.get_terminal_size(sys.__stdout__.fileno())
        ceilings = {"COLUMNS": real.columns, "LINES": real.lines}
    except (OSError, ValueError, AttributeError):
        ceilings = {}
    dropped = []
    del PINNED[:]
    for name, cap in (("COLUMNS", MAX_COLS), ("LINES", MAX_ROWS)):
        raw = os.environ.get(name)
        ceiling = ceilings.get(name) or cap
        value = None
        if raw is not None:
            try:
                value = int(raw)
            except ValueError:
                value = 0
            if not 1 <= value <= ceiling:
                del os.environ[name]
                dropped.append(name)
                value = None
        if value is None:
            # Nothing in the environment now, so what ncurses will read is
            # the window the kernel reports.
            value = ceilings.get(name)
        if value is not None and value > cap:
            os.environ[name] = str(cap)
            PINNED.append(name)
    return dropped


def unpin_window_env():
    """Give the window back to the terminal, now that the screen is allocated.

    The cap has to be in the environment across initscr(), which is where the
    memory is claimed and where a window past 32767 cells in a dimension gave
    up outright. It must not still be there afterwards: ncurses reads COLUMNS
    and LINES again on every resize, so a value left behind pins the screen
    to the cap and a window shrunk below it is drawn far outside itself.
    """
    for name in PINNED:
        os.environ.pop(name, None)
    del PINNED[:]


def window_size(stdscr):
    """The grid to animate: what curses reports, clipped to something real.

    `sane_window_env` covers the case that hangs initscr(); this covers the
    rest of it. Per-frame work is one pass over the columns and their live
    cells, so it scales with the grid, and a grid that is not a window makes
    a frame take seconds — long enough that a keypress and a signal both look
    ignored. Beyond the cap the extra columns are simply not animated.
    """
    height, width = stdscr.getmaxyx()
    return min(height, MAX_ROWS), min(width, MAX_COLS)


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
        drop.refit(height)
    return field


def run(stdscr, glyphs, speed, color):
    # The screen exists now, so the size caps go back out of the environment
    # before ncurses can read them again on a resize.
    unpin_window_env()
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
    dim = has_dim(has_color)

    height, width = window_size(stdscr)
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
            height, width = window_size(stdscr)
            stdscr.erase()
            field = build_field(
                width, height, glyphs, speed, warm_start=True, previous=field
            )
            continue
        if key != -1:
            # The exit is decided here, so the signals this program catches
            # stop being honoured here: the rest of a paste is still streaming
            # into a tty whose ISIG comes back on during the restore below,
            # and a ^C in it used to reach the handler and kill a process that
            # had already been told to leave by a keypress.
            stop_catching_signals()
            # Whatever else was typed or pasted is still in the tty buffer;
            # without this it lands on the user's shell prompt — and runs.
            curses.flushinp()
            drain_input()
            return

        stdscr.erase()
        for x, drop in enumerate(field):
            if x >= width:
                break
            if drop.advance(height):
                drop.reset(height, speed)
            drop.draw(stdscr, x, height, width, bold_body, dim)
        stdscr.refresh()

        next_frame += frame
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            # Fell behind (a resize storm, a suspended process). Re-anchor
            # rather than sprinting to catch up on a backlog of frames.
            next_frame = time.monotonic()


def drain_input():
    """Read what is still arriving, not only what has already arrived.

    `curses.flushinp()` discards what the tty holds at that instant, and a
    paste bigger than the tty's buffer is still being written by the terminal
    emulator while this program tears down: 18,851 bytes of a 64 KB paste
    landed on the shell prompt after the alternate screen was left, which is
    the one thing the flush exists to prevent. Reading until the descriptor
    has been quiet for a moment catches the rest of it; the ceiling means a
    terminal that never stops talking cannot hold the exit open. The wait is
    a tenth of a second and it is not skipped on the first pass: polling with
    no timeout instead read an empty buffer and left, because the terminal
    that is mid-paste has not been scheduled to refill it yet, and the tail
    landed on the shell after all.

    Called while the tty is still in raw mode, which is half the point: with
    ISIG off a ^C in the middle of the paste is a byte to discard rather than
    a signal at a process that has already decided how it is leaving. The
    other half is echo, which is also still off, so the bytes read here are
    never painted onto the shell's screen.
    """
    idle, ceiling = 0.1, 1.0
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return
    deadline = time.monotonic() + ceiling
    while time.monotonic() < deadline:
        try:
            ready, _, _ = select.select([fd], [], [], idle)
        except (OSError, ValueError):
            return
        if not ready:
            return
        try:
            if not os.read(fd, 65536):
                return
        except OSError:
            return


CATCHABLE = ("SIGINT", "SIGQUIT", "SIGHUP", "SIGTERM")

# The signal number that started the shutdown, or 0. Read by the frame loop, so
# a shutdown still happens even if the exception is swallowed on its way out —
# and read again on the way out of main(), which is why it stores the signum
# rather than a bare flag: that backstop returns normally, and the process must
# still die *of the signal* rather than exit 0.
SHUTDOWN = 0


try:
    # The import machinery holds this lock while it runs, including inside the
    # weakref callbacks that clean up its per-module locks — code that can run
    # at any allocation, long after the last `import` statement. It is a
    # builtin, not a package; every CPython has it, and the guard below is a
    # no-op on a runtime that does not.
    from _imp import lock_held as _import_lock_held
except ImportError:
    _import_lock_held = None


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

    Nor does it raise while an import is in flight. A signal landing inside
    importlib's own machinery — most often the weakref callback that releases a
    module lock, which runs whenever the garbage collector gets to it — raises
    where the exception has nowhere to go: CPython prints
    `Exception ignored in: <function _get_module_lock.<locals>.cb ...>` with a
    traceback naming this file, then carries on. About one run in a hundred
    signalled during startup did exactly that. Recording the signum and
    returning is enough, because the frame loop and `main` both act on it.
    """

    def handler(signum, frame):
        global SHUTDOWN
        if SHUTDOWN:
            # Already unwinding. Raising again here would land inside the
            # restore the first signal started, which is the whole bug. Do
            # nothing and let the first exception finish its work.
            return
        SHUTDOWN = signum
        if _import_lock_held is not None and _import_lock_held():
            return
        raise Interrupted(signum)

    for name in CATCHABLE:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def stop_catching_signals():
    """Stop honouring the caught signals, once a keypress has decided the exit.

    The handler turns a fatal signal into an exception so the terminal is
    restored, which is right for a signal that means to end the program. It is
    wrong for the tail of a paste: the program returns on the first pasted
    byte, the restore turns ISIG back on while the rest is still streaming
    into the tty, and the line discipline raises SIGINT at a process that is
    already leaving — a keypress exit that came back as `killed by SIGINT`
    12 times out of 12, and as SIGQUIT for a paste holding ^\\.

    Ignoring rather than defaulting, because the default for these is death,
    and the exit status of a keypress is 0 whatever else is in the buffer.
    """
    for name in CATCHABLE:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, signal.SIG_IGN)
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


def representable(glyphs, encoding, parser, charset_name, notices):
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
        notices.append(
            "%s: %d of %d %s glyphs are not representable in %s; drawing with "
            "the rest.\n" % (PROG, len(glyphs) - len(keep), len(glyphs),
                             charset_name, encoding)
        )
    return keep


def die_by_signal(signum):
    """Finish the job the signal started: die of it, now that the screen is back.

    Catching a fatal signal to restore the terminal is right; exiting 0
    afterwards is not. A shell reports `^C` as 130 and a supervisor decides
    whether to restart from `WIFSIGNALED`, and both were being told the
    screensaver had finished normally. So the handler's disposition goes back to
    the default and the signal is raised again at this process, which never
    returns. Called only after the terminal restore has completed.
    """
    for stream in (sys.stdout, sys.stderr):
        # Nothing buffered may be lost: the re-raise below does not unwind, so
        # interpreter shutdown never runs and never flushes these. A stream
        # whose fd was closed at exec is None rather than a stream, and this
        # runs on the way out of a signal, where an AttributeError has nowhere
        # left to go.
        if stream is None:
            continue
        try:
            stream.flush()
        except (ValueError, OSError):
            pass
    try:
        signal.signal(signum, signal.SIG_DFL)
    except (ValueError, OSError):
        return 128 + signum
    try:
        signal.raise_signal(signum)
    except AttributeError:
        # signal.raise_signal is 3.8+; this is the older spelling, kept because
        # the file claims 3.8 and costs two lines to mean it.
        os.kill(os.getpid(), signum)
    # Only reachable if the signal is blocked or ignored process-wide, which is
    # the caller's arrangement, not this program's. Report it as a shell would.
    return 128 + signum


def unanimatable_terminal():
    """Why this TERM cannot be animated, or None if it can.

    Run before curses is entered, so a refusal leaves the screen untouched.
    `TERM=dumb` is the case that matters: it has no `cup`, no way to address
    the cursor, so every frame lands on the same line. The program used to draw
    one screenful and then rewrite its last line forever, which looks exactly
    like a hang. Refusing is the honest answer, and it is the same shape as the
    `stdout is not a terminal` refusal above it.
    """
    term = os.environ.get("TERM")
    # TERM goes through show() like every other value this program did not
    # choose. It is client-supplied over ssh, and a bare %s put it on the
    # screen as-is: `TERM=$'\e[2J\e[31mPWNED'` repainted the terminal it was
    # being complained about on, an embedded newline made two lines of one
    # refusal, and a 4096-character TERM printed 4272 bytes.
    # Three states, not two. An unset TERM and an empty one both used to be
    # reported as `TERM=(unset)`, and both then blamed the terminfo database
    # ("could not find terminfo database") for what is only a variable nobody
    # set. Say the true thing in each case before asking terminfo anything.
    if term is None:
        return (
            "TERM is not set, so there is no terminal type to look up - set it "
            "to your terminal's type, such as xterm or vt100."
        )
    if not term:
        return (
            "TERM is set but empty, which names no terminal type - set it to "
            "your terminal's type, such as xterm or vt100."
        )
    try:
        curses.setupterm()
    except (curses.error, ValueError, OSError, TypeError) as exc:
        # Unknown TERM, or no terminfo database at all. Reaching curses.wrapper
        # with this would come back as an opaque `terminal error:` after the
        # screen had already been entered.
        return (
            "TERM=%s is not a terminal type this system knows (%s) - set TERM "
            "to one your terminfo database has, such as xterm or vt100."
            % (show(term), exc)
        )
    try:
        cup = curses.tigetstr("cup")
    except curses.error:
        cup = None
    if cup is None:
        return (
            "TERM=%s has no cursor addressing, so there is nothing to animate "
            "with - set TERM to a full-screen terminal type such as xterm or "
            "vt100." % (show(term),)
        )
    return None


def main(argv=None):
    # Before anything else: a signal during argument parsing should not be able
    # to outrun the handler that makes it exit cleanly.
    install_signal_handlers()
    try:
        status = _main(argv)
    except (KeyboardInterrupt, Interrupted):
        # Everything from argument parsing to teardown is inside this, because
        # a signal at any of those moments used to print a traceback.
        return die_by_signal(SHUTDOWN or signal.SIGINT)
    if SHUTDOWN:
        # The frame loop's backstop: a signal fired, the exception was eaten on
        # its way out (curses.wrapper has a bare `except` of its own) and the
        # loop returned normally instead. The terminal is restored either way,
        # so the only thing left to get right is the exit status.
        return die_by_signal(SHUTDOWN)
    return status


def _main(argv=None):
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    argv = attach_values(consume_end_of_options(argv, parser))
    # One wording for one mistake. `ascii_rain.py a` used to reach argparse's
    # plural ("unrecognized arguments: a") while `ascii_rain.py -- a` reached
    # the line above ("unexpected argument: 'a'") — two voices for a word the
    # program has no use for. The scout finds it either way, and finds it
    # *before* --help or --version can exit 0 over the top of it.
    scout, extra = build_parser(scout=True).parse_known_args(argv)
    if extra:
        parser.error("unexpected argument: %s" % show(extra[0]))
    # Values are checked off the scout's reading too, for the same reason a
    # stray word is. `--speed 99 --help` printed the help and exited 0 with the
    # bad speed unread, while `--bogus --help` exited 2: one sentence in the
    # README, two behaviours in the program. A mistake standing next to --help
    # is a mistake either way it is spelled.
    # What was dropped from the glyph pool, held until there is nothing left
    # to refuse: an advisory standing beside a fatal error is a second line
    # for one bad input, and about a pool that is then never used.
    notices = []
    speed = parse_speed(scout.speed, parser)
    glyphs = parse_charset(scout.charset, parser, notices)
    color = parse_color(scout.color, parser)
    # Nothing left to refuse, so the real parser can run — which is where
    # --help and --version print and exit 0.
    args = parser.parse_args(argv)
    glyphs = representable(
        glyphs, terminal_encoding(), parser,
        args.charset if args.charset in CHARSETS else "custom pool", notices,
    )
    # Every argument is good now, and --help and --version have had their
    # chance to exit 0 over the top of a mistake. Now the advisories.
    for notice in notices:
        complain(notice)

    if not is_a_terminal(sys.stdout):
        complain(
            "%s: stdout is not a terminal - there is nothing to draw on. Run "
            "it in a terminal rather than a pipe or a file.\n" % PROG
        )
        return 2
    if not is_a_terminal(sys.stdin):
        # With no tty on stdin no keypress can ever arrive, so "any key exits"
        # would be a lie and the only way out would be a signal.
        complain(
            "%s: stdin is not a terminal - no keypress could reach it. Run it "
            "in a terminal rather than under a redirect.\n" % PROG
        )
        return 2

    sane_window_env()

    refusal = unanimatable_terminal()
    if refusal is not None:
        complain("%s: %s\n" % (PROG, refusal))
        return 2

    try:
        curses.wrapper(run, glyphs, speed, color)
    except curses.error:
        # A window closing or a connection dropping takes the terminal out
        # from under curses, and curses reports it as an ERR from whichever
        # call was in flight — `nocbreak() returned ERR`, which named a C
        # function at a user who had just closed a window. If a signal came
        # with it (a closing terminal sends SIGHUP), that signal is the whole
        # story and main() is about to re-raise it: 129 at the shell, and no
        # line, because there is no terminal left to read one on.
        if not SHUTDOWN:
            complain(
                "%s: the terminal went away - the window closed, or the "
                "connection dropped.\n" % PROG
            )
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
