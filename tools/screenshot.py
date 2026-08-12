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

Fonts come from the system: DejaVu Sans Mono for latin, Noto Sans CJK for the
half-width katakana. Adjust the paths below if yours live elsewhere.
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

MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# pyte does not model SGR 2 (dim), which curses emits for the tail. Borrow the
# unused italics flag as a dim marker; ascii_rain never emits real italics.
pyte.graphics.TEXT = dict(pyte.graphics.TEXT)
pyte.graphics.TEXT[2] = "+italics"


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
    fonts = {
        (False, "latin"): ImageFont.truetype(MONO, FONT_SIZE),
        (True, "latin"): ImageFont.truetype(MONO_BOLD, FONT_SIZE),
        (False, "cjk"): ImageFont.truetype(CJK, FONT_SIZE),
        (True, "cjk"): ImageFont.truetype(CJK, FONT_SIZE),
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


def main():
    data = capture(SETTLE_SECONDS)
    screen = pyte.Screen(COLS, ROWS)
    pyte.Stream(screen).feed(data.decode("utf-8", "replace"))
    img, lit, heads = paint(screen)
    img.save(OUTPUT)
    print("wrote %s  %dx%d  cells lit: %d  heads: %d"
          % (OUTPUT, img.size[0], img.size[1], lit, heads))


if __name__ == "__main__":
    main()
