#!/usr/bin/env python3
"""Regenerate screenshot.png from the real program.

This is a development tool, not part of ascii-rain. It is the only thing in
this repo with dependencies:

    pip install pyte pillow

What it does: runs `ascii_rain.py` in a pseudo-terminal of a fixed size, reads
the bytes the program writes, replays those bytes through a terminal emulator
(pyte) to recover the final cell grid, and paints that grid to a PNG. So every
glyph, every position and every brightness tier in the image is the program's
own output — but the hues and the background are this script's choice, because
they are the terminal's choice in real life and there is no terminal here.

    python3 tools/screenshot.py            # writes ../screenshot.png

    python3 tools/screenshot.py --check-fonts   # print the fonts it would use

Fonts come from the system: a monospace face in regular and bold for latin, and
a CJK face for the half-width katakana. Each role has a list of candidate paths
covering the common Linux and macOS locations, tried in order; set
ASCII_RAIN_FONT_MONO, ASCII_RAIN_FONT_MONO_BOLD or ASCII_RAIN_FONT_CJK to a
font file to override any of them.
"""

import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

import pyte
import pyte.graphics
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM = os.path.join(HERE, os.pardir, "ascii_rain.py")
OUTPUT = os.path.join(HERE, os.pardir, "screenshot.png")

COLS, ROWS = 100, 30
SETTLE_SECONDS = 20.0
CELL_W, CELL_H, FONT_SIZE, PAD = 11, 22, 17, 18

BG = (6, 9, 7)
# (curses foreground, bold, dim) -> RGB. These four keys are every attribute
# combination ascii_rain emits for the green palette.
PALETTE = {
    ("white", True, False): (238, 255, 240),   # head
    ("green", True, False): (61, 235, 106),    # near-head body
    ("green", False, False): (26, 148, 62),    # body
    ("green", False, True): (16, 74, 34),      # dim tail
}
FALLBACK = PALETTE[("green", False, False)]

# Candidate fonts per role, in preference order, as (path, face index). The
# index matters for .ttc collections: macOS ships Menlo as one file holding
# regular, bold, italic and bold-italic. Three hardcoded Linux paths used to
# stand here, and a machine missing any of them got an OSError traceback out of
# Pillow. The macOS entries are the standard system locations; this repo's
# sandbox is Linux, so those are the paths, not a claim they were exercised.
MONO_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),   # Debian/Ubuntu
    ("/usr/share/fonts/dejavu/DejaVuSansMono.ttf", 0),            # Fedora
    ("/usr/share/fonts/TTF/DejaVuSansMono.ttf", 0),               # Arch
    ("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", 0),
    ("/System/Library/Fonts/Menlo.ttc", 0),                       # macOS
    ("/System/Library/Fonts/Supplemental/Courier New.ttf", 0),    # macOS
]
MONO_BOLD_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0),
    ("/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf", 0),
    ("/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf", 0),
    ("/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf", 0),
    ("/System/Library/Fonts/Menlo.ttc", 1),                       # macOS, bold face
    ("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 0),
]
CJK_CANDIDATES = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf", 0),
    ("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf", 0),
    ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", 0),      # Fedora/Arch
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),             # macOS
    ("/System/Library/Fonts/Supplemental/Osaka.ttf", 0),           # macOS
]

# role -> (candidates, environment override). The role names are what the
# failure message says out loud, so they are short enough to type.
ROLES = [
    ("mono", MONO_CANDIDATES, "ASCII_RAIN_FONT_MONO"),
    ("mono-bold", MONO_BOLD_CANDIDATES, "ASCII_RAIN_FONT_MONO_BOLD"),
    ("cjk", CJK_CANDIDATES, "ASCII_RAIN_FONT_CJK"),
]

# pyte does not model SGR 2 (dim), which curses emits for the tail. Borrow the
# unused italics flag as a dim marker; ascii_rain never emits real italics.
pyte.graphics.TEXT = dict(pyte.graphics.TEXT)
pyte.graphics.TEXT[2] = "+italics"


def die(message):
    """One line on stderr, exit 2. A missing font is a setup problem, not a bug
    worth a Pillow traceback."""
    sys.stderr.write("screenshot: %s\n" % message)
    raise SystemExit(2)


def load_font(path, index, size):
    return ImageFont.truetype(path, size, index=index)


def resolve_font(role, candidates, env_var, size=FONT_SIZE):
    """The first candidate that exists and actually loads, or the override."""
    override = os.environ.get(env_var)
    if override:
        try:
            load_font(override, 0, size)
        except (OSError, ValueError) as exc:
            die("no %s font: %s is set to %r, which will not load (%s)"
                % (role, env_var, override, exc))
        return override, 0
    for path, index in candidates:
        if not os.path.exists(path):
            continue
        try:
            load_font(path, index, size)
        except (OSError, ValueError):
            continue
        return path, index
    die("no %s font found in %d known locations - set %s to a font file"
        % (role, len(candidates), env_var))


def resolve_all():
    return dict(
        (role, resolve_font(role, candidates, env_var))
        for role, candidates, env_var in ROLES
    )


def check_fonts():
    for role, (path, index) in sorted(resolve_all().items()):
        print("%-9s %s%s" % (role, path, " [face %d]" % index if index else ""))
    return 0


def capture(seconds):
    """Run the program in a pty for `seconds` and return the bytes it wrote."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execv(sys.executable, [sys.executable, PROGRAM,
                                  "--charset", "matrix", "--color", "green"])
        os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    data = b""
    start = time.time()
    while time.time() - start < seconds:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
    os.write(fd, b"q")
    time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
    os.close(fd)
    return data


def paint(screen):
    img = Image.new("RGB", (COLS * CELL_W + 2 * PAD, ROWS * CELL_H + 2 * PAD), BG)
    draw = ImageDraw.Draw(img)
    resolved = resolve_all()
    mono, mono_bold, cjk = resolved["mono"], resolved["mono-bold"], resolved["cjk"]
    fonts = {
        (False, "latin"): load_font(mono[0], mono[1], FONT_SIZE),
        (True, "latin"): load_font(mono_bold[0], mono_bold[1], FONT_SIZE),
        # One CJK face for both weights: the katakana are drawn at head
        # brightness often enough that a missing bold face would be visible,
        # and no CJK family here ships a bold that matches the mono metrics.
        (False, "cjk"): load_font(cjk[0], cjk[1], FONT_SIZE),
        (True, "cjk"): load_font(cjk[0], cjk[1], FONT_SIZE),
    }
    lit = heads = 0
    for y in range(ROWS):
        line = screen.buffer[y]
        for x in range(COLS):
            cell = line[x]
            if not cell.data or cell.data == " ":
                continue
            lit += 1
            if cell.fg == "white" and cell.bold:
                heads += 1
            script = "cjk" if ord(cell.data[0]) > 0x2500 else "latin"
            colour = PALETTE.get((cell.fg, bool(cell.bold), bool(cell.italics)),
                                 FALLBACK)
            draw.text((PAD + x * CELL_W, PAD + y * CELL_H), cell.data,
                      font=fonts[(bool(cell.bold), script)], fill=colour)
    return img, lit, heads


KNOWN_FLAGS = ("-h", "--help", "--check-fonts")


def main():
    argv = sys.argv[1:]
    # Unknown words first. --check-fonts used to be answered before this guard
    # ran, so `screenshot.py bogus --check-fonts` printed the fonts and exited
    # 0 with bogus unread; and --help was itself an unexpected argument, which
    # is a poor answer from a script the README points at.
    for arg in argv:
        if arg not in KNOWN_FLAGS:
            die("unexpected argument: %r (the flags are --check-fonts and "
                "--help)" % arg)
    if any(flag in argv for flag in ("-h", "--help")):
        sys.stdout.write(__doc__.strip() + "\n")
        return 0
    if "--check-fonts" in argv:
        return check_fonts()
    # Resolved before the 20-second capture, so a missing font fails in a
    # second rather than after the run it would have painted.
    resolve_all()
    data = capture(SETTLE_SECONDS)
    screen = pyte.Screen(COLS, ROWS)
    pyte.Stream(screen).feed(data.decode("utf-8", "replace"))
    img, lit, heads = paint(screen)
    img.save(OUTPUT)
    print("wrote %s  %dx%d  cells lit: %d  heads: %d"
          % (OUTPUT, img.size[0], img.size[1], lit, heads))
    return 0


if __name__ == "__main__":
    sys.exit(main())
