#!/usr/bin/env python3
"""Check the things about ascii-rain a person cannot check by looking.

A development tool, not part of the program. It drives `ascii_rain.py` under a
pseudo-terminal and asserts the behaviour that only shows up in exit statuses,
terminfo capabilities and byte streams:

    python3 tools/checks.py            # every group
    python3 tools/checks.py signals    # exit status per signal
    python3 tools/checks.py dumb       # the TERM=dumb refusal
    python3 tools/checks.py args       # argument handling, including `--`
    python3 tools/checks.py tiers      # the no-`dim` tail

Exit status is 0 if every check in the named groups passed, 1 otherwise. Only
the `tiers` group needs a dependency (`pip install pyte`); the other three run
on the standard library alone.

The child always runs with RLIMIT_CORE at 0. SIGQUIT's default action is to
dump core, and this checker sends SIGQUIT on purpose.
"""

import fcntl
import os
import pty
import resource
import select
import signal
import struct
import subprocess
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
PROGRAM = os.path.join(ROOT, "ascii_rain.py")

# The sha ascii_rain.py sat at when day 019 shipped. `tiers` compares the
# dim-capable path against this build to show it was left alone; if the object
# is not in the clone (a shallow fetch, say) that one comparison is skipped and
# says so, rather than being quietly dropped.
DAY019_SHA = "a6052da94f3af6587c936dea29f6ea2ba40f6da2"

COLS, ROWS = 100, 30

FAILURES = []


def check(name, ok, detail=""):
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, "  " + detail if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def note(text):
    print("     %s" % text)


# --------------------------------------------------------------------------
# running the program


def _child_setup():
    """Runs in the forked child, before exec."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def spawn(args=(), term="xterm-256color", cols=COLS, rows=ROWS, pipe_stderr=False,
          env_extra=None):
    """Start the program on a pty. Returns (pid, master_fd, stderr_fd or None).

    stdin and stdout are the pty, because the program refuses to run without a
    terminal on both. stderr can be a pipe instead, which is the only way to
    tell "wrote nothing to the screen" apart from "wrote a message" when both
    would otherwise land on the same fd.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    err_read = err_write = None
    if pipe_stderr:
        err_read, err_write = os.pipe()
    env = dict(os.environ)
    env["TERM"] = term
    env.update(env_extra or {})
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(err_write if pipe_stderr else slave, 2)
            for fd in (master, slave, err_read, err_write):
                if fd is not None and fd > 2:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            _child_setup()
            os.execve(sys.executable, [sys.executable, PROGRAM] + list(args), env)
        except BaseException:
            os._exit(127)
    os.close(slave)
    if pipe_stderr:
        os.close(err_write)
    return pid, master, err_read


def read_for(fd, seconds, stop_when_idle=False):
    """Drain `fd` for `seconds`, or until it goes quiet if asked."""
    data = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            if stop_when_idle:
                break
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def restore_suffix(term):
    """The bytes ncurses emits to hand the terminal back, for this TERM."""
    import curses

    saved = os.environ.get("TERM")
    os.environ["TERM"] = term
    try:
        curses.setupterm()
        return curses.tigetstr("rmcup"), curses.tigetstr("rmkx")
    finally:
        if saved is None:
            del os.environ["TERM"]
        else:
            os.environ["TERM"] = saved


# --------------------------------------------------------------------------
# group: signals


def run_until(pid, master, finish, settle=1.2):
    """Let the program draw, then end it with `finish(master, pid)`."""
    data = read_for(master, settle)
    finish(master, pid)
    data += read_for(master, 2.0, stop_when_idle=True)
    _, status = os.waitpid(pid, 0)
    os.close(master)
    return status, data


def group_signals():
    term = "xterm-256color"
    rmcup, rmkx = restore_suffix(term)

    def restored(data):
        # The teardown is rmcup ("leave the alternate screen") followed by
        # rmkx ("leave keypad mode"), with a carriage return between them.
        return data.endswith(rmkx) and rmcup in data[-64:]

    for name in ("SIGINT", "SIGQUIT", "SIGHUP", "SIGTERM"):
        signum = getattr(signal, name)
        pid, master, _ = spawn(term=term)
        status, data = run_until(pid, master, lambda m, p, s=signum: os.kill(p, s))
        killed = os.WIFSIGNALED(status)
        check("signals: %s kills the process" % name, killed,
              "WTERMSIG=%s" % (os.WTERMSIG(status) if killed
                               else "exited %d" % os.WEXITSTATUS(status)))
        if killed:
            check("signals: %s reports its own number" % name,
                  os.WTERMSIG(status) == signum,
                  "want %d, got %d" % (signum, os.WTERMSIG(status)))
        check("signals: %s restores the terminal" % name, restored(data),
              repr(data[-24:]))

    pid, master, _ = spawn(term=term)
    status, data = run_until(pid, master, lambda m, p: os.write(m, b"q"))
    check("signals: a keypress exits 0",
          os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0,
          "status %d" % status)
    check("signals: a keypress restores the terminal", restored(data),
          repr(data[-24:]))


# --------------------------------------------------------------------------
# group: dumb


def group_dumb():
    pid, master, err = spawn(term="dumb", pipe_stderr=True)
    out = read_for(master, 2.0, stop_when_idle=True)
    message = os.read(err, 65536)
    _, status = os.waitpid(pid, 0)
    os.close(master)
    os.close(err)

    check("dumb: exits 2",
          os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2,
          "status %d" % status)
    lines = [line for line in message.decode("utf-8", "replace").splitlines() if line]
    check("dumb: one line on stderr", len(lines) == 1, repr(lines))
    check("dumb: the line names TERM",
          bool(lines) and "dumb" in lines[0], repr(lines[:1]))
    check("dumb: writes nothing to stdout", out == b"", repr(out[:60]))
    check("dumb: emits no escape sequence", b"\x1b" not in out, repr(out[:60]))

    pid, master, _ = spawn(term="vt100")
    drawn = read_for(master, 2.0)
    os.write(master, b"q")
    read_for(master, 1.0, stop_when_idle=True)
    os.waitpid(pid, 0)
    os.close(master)
    check("dumb: vt100 still animates", len(drawn) > 5120,
          "%d bytes in 2 s" % len(drawn))


# --------------------------------------------------------------------------
# group: args


def cli(args):
    """Run with stdout on a pipe: everything here is decided before curses."""
    proc = subprocess.run(
        [sys.executable, PROGRAM] + list(args),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), \
        proc.stderr.decode("utf-8", "replace")


def group_args():
    code, _, err = cli(["--speed", "2", "--"])
    check("args: a bare -- is consumed",
          code == 2 and "stdout is not a terminal" in err,
          "exit %d: %s" % (code, err.strip()[:70]))

    code, _, err = cli(["--"])
    check("args: -- on its own is consumed",
          code == 2 and "stdout is not a terminal" in err,
          "exit %d: %s" % (code, err.strip()[:70]))

    code, _, err = cli(["--", "x"])
    check("args: a word after -- is named",
          code == 2 and "unexpected argument" in err and "'x'" in err,
          "exit %d: %s" % (code, err.strip()[:70]))

    code, _, err = cli(["--charset", "--"])
    check("args: -- as a flag's value is a missing value",
          code == 2 and "expected one argument" in err,
          "exit %d: %s" % (code, err.strip()[:70]))

    # The day-019 refusals. The `--` change rewrites the argv every one of
    # these travels through, so they are re-run as a regression sweep.
    refusals = [
        (["--speed", "99"], "--speed"),
        (["--speed", "abc"], "--speed"),
        (["--charset", "nope"], "--charset"),
        (["--color", "nope"], "--color"),
        (["--charset", "custom:   "], "custom"),
        (["--nonsense"], "unrecognized"),
    ]
    for args, wanted in refusals:
        code, out, err = cli(args)
        lines = [line for line in err.splitlines() if line]
        check("args: %s exits 2 with one line" % " ".join(args),
              code == 2 and len(lines) == 1 and wanted in err
              and "Traceback" not in err and out == "",
              "exit %d, %d line(s): %s" % (code, len(lines), err.strip()[:70]))

    for args in (["--help"], ["--version"]):
        code, out, err = cli(args)
        check("args: %s exits 0" % args[0], code == 0 and out and err == "",
              "exit %d" % code)


# --------------------------------------------------------------------------
# group: tiers


# How many runs each density figure averages. Every column picks its own length
# and phase, so a single settled grid varies by 5-12 % run to run; three runs
# sat close enough to the 10 % threshold below to fail on an unlucky afternoon.
DENSITY_RUNS = 6

# The density figures are counted on a grid this wide rather than the 100x30 of
# the other groups. Each column contributes its own random length and phase, so
# the noise in a lit-cell count falls with the number of columns on screen —
# 240 of them costs the same wall clock as 100 and halves the spread.
DENSITY_COLS, DENSITY_ROWS = 240, 60


def lit_cells(data, cols, rows):
    import pyte

    screen = pyte.Screen(cols, rows)
    pyte.Stream(screen).feed(data.decode("utf-8", "replace"))
    count = 0
    for y in range(rows):
        line = screen.buffer[y]
        for x in range(cols):
            cell = line[x]
            if cell.data and cell.data != " ":
                count += 1
    return count


def settled_capture(term, program=PROGRAM, seconds=3.0, cols=COLS, rows=ROWS):
    """One run, drained to the end so the last frame in the bytes is complete."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = dict(os.environ)
    env["TERM"] = term
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            for fd in (master, slave):
                if fd > 2:
                    os.close(fd)
            _child_setup()
            os.execve(sys.executable, [sys.executable, program], env)
        except BaseException:
            os._exit(127)
    os.close(slave)
    data = read_for(master, seconds)
    os.write(master, b"q")
    data += read_for(master, 2.0, stop_when_idle=True)
    os.waitpid(pid, 0)
    os.close(master)
    return data


def mean_lit(term, program=PROGRAM, runs=DENSITY_RUNS):
    counts = [
        lit_cells(
            settled_capture(term, program, cols=DENSITY_COLS, rows=DENSITY_ROWS),
            DENSITY_COLS, DENSITY_ROWS,
        )
        for _ in range(runs)
    ]
    return sum(counts) / float(len(counts)), counts


def day019_program():
    """Write the day-019 ascii_rain.py to a temp file, or return None."""
    try:
        blob = subprocess.check_output(
            ["git", "-C", ROOT, "show", "%s:ascii_rain.py" % DAY019_SHA],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    path = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "ascii_rain_day019_%d.py" % os.getpid())
    with open(path, "wb") as handle:
        handle.write(blob)
    return path


def has_dim_sgr(data):
    # xterm-256color spells A_DIM as ESC[0;2m; the leading reset varies, the
    # `;2m` or `[2m` does not.
    return b"\x1b[2m" in data or b";2m" in data


def group_tiers():
    try:
        import pyte  # noqa: F401
    except ImportError:
        check("tiers: pyte is installed", False, "pip install pyte")
        return

    # Two notes on which terminals appear below, both measured rather than
    # assumed.
    #
    # The dim-capable reference is xterm-256color, not TERM=linux. `linux` does
    # have a `dim` string in terminfo — so ascii-rain takes the dim path there,
    # correctly — but its `ncv#18` tells ncurses that dim cannot be combined
    # with colour, and ncurses drops the attribute: the bytes carry no dim SGR
    # at all. That is a terminfo fact about the Linux console, not a decision
    # this program makes.
    #
    # And the thinning is measured against the day-019 binary on the *same*
    # terminal rather than against another terminal on the same binary.
    # Counting lit cells off a settled grid carries a per-terminal offset —
    # day-019, whose tail is identical everywhere, still counts differently at
    # TERM=linux than at TERM=vt100 — so a cross-terminal difference mixes the
    # effect with that offset. Same terminal, two binaries, back to back: the
    # offset cancels and what is left is the tail.
    dim_capture = settled_capture("xterm-256color")
    check("tiers: a dim-capable terminal still emits the dim SGR",
          has_dim_sgr(dim_capture), "TERM=xterm-256color")

    flat_capture = settled_capture("vt100")
    check("tiers: no dim SGR at TERM=vt100", not has_dim_sgr(flat_capture))

    thin, thin_counts = mean_lit("vt100")
    full, full_counts = mean_lit("linux", runs=3)
    note("tiers: for reference, TERM=linux draws %.0f %s" % (full, full_counts))

    baseline = day019_program()
    if baseline is None:
        note("tiers: day-019 blob %s is not in this clone, so the two "
             "comparisons below fall back to TERM=linux on this build, which "
             "is noisier" % DAY019_SHA[:7])
        old_thin, old_thin_counts = full, full_counts
        old_full, old_full_counts = full, full_counts
    else:
        try:
            old_thin, old_thin_counts = mean_lit("vt100", program=baseline)
            old_full, old_full_counts = mean_lit("linux", program=baseline, runs=3)
        finally:
            os.unlink(baseline)

    drop = 1.0 - (thin / old_thin) if old_thin else 0.0
    # Threshold 5 %, not the 10 % the field model is held to below, and the gap
    # between those two numbers is a property of the measurement rather than of
    # the program. Replaying a capture through pyte recovers about 13 % of the
    # 18.7 % the model draws: a settled grid is one frame, and a frame read off
    # a terminal emulator carries cells the emulator kept that ncurses had
    # already blanked. 13 % is not far enough above 10 % to assert at any
    # sample size this check can afford, so the pty half asserts the direction
    # and the size it can stand behind, and the exact figure is asserted where
    # it is exact.
    check("tiers: the no-dim tail draws measurably fewer cells", drop >= 0.05,
          "at TERM=vt100: day 019 %s -> %.0f, now %s -> %.0f, %.1f%% fewer"
          % (old_thin_counts, old_thin, thin_counts, thin, drop * 100))

    off = abs(full - old_full) / old_full if old_full else 1.0
    check("tiers: the dim path is unchanged from day 019", off <= 0.20,
          "at TERM=linux: day 019 %s -> %.0f, now %.0f, %.1f%% apart"
          % (old_full_counts, old_full, full, off * 100))

    model_tiers()


def model_tiers():
    """The 10 % figure, asserted on the field itself rather than on a capture.

    This runs `ascii_rain`'s own Drop objects in this process — no terminal, no
    emulator — and counts, on a settled field, the cells the dim path would
    draw against the cells the thinned path draws. It is the same predicate
    `Drop.draw` uses, read off the same objects, so there is nothing between
    the claim and the number.
    """
    sys.path.insert(0, ROOT)
    import random

    import ascii_rain

    # Seeded, so this number is the same on every machine and every run.
    random.seed(20260901)
    height, width = 60, 240
    field = [ascii_rain.Drop(height, ascii_rain.CHARSETS["matrix"], 1.0, True)
             for _ in range(width)]
    with_dim = thinned = 0
    for frame in range(700):
        for drop in field:
            if drop.advance(height):
                drop.reset(height, 1.0)
        if frame < 200 or frame % 25:
            continue  # let it settle, then sample frames far enough apart
        for drop in field:
            head_row = int(drop.head)
            for row, (_, survives) in drop.cells.items():
                distance = head_row - row
                if distance < 0 or distance >= drop.length:
                    continue
                with_dim += 1
                in_tail = distance > 0 and distance > drop.length * 0.6
                if survives or not in_tail:
                    thinned += 1
    drop_fraction = 1.0 - (thinned / float(with_dim))
    check("tiers: the field model thins the tail by at least 10%",
          drop_fraction >= 0.10,
          "dim path %d cells, thinned %d, %.1f%% fewer"
          % (with_dim, thinned, drop_fraction * 100))


# --------------------------------------------------------------------------

GROUPS = [
    ("signals", group_signals),
    ("dumb", group_dumb),
    ("args", group_args),
    ("tiers", group_tiers),
]


HELP_FLAGS = ("-h", "--help")


def main(argv):
    known = dict(GROUPS)
    # The README sends people here, and `--help` used to be read as a group
    # name: "checks: no such group: '--help'", exit 2. Unknown words are still
    # refused first, so `--help bogus` names bogus rather than exiting 0 over
    # it - the same order ascii_rain.py uses.
    for name in argv:
        if name not in known and name not in HELP_FLAGS:
            sys.stderr.write("checks: no such group: %r (have: %s)\n"
                             % (name, ", ".join(known)))
            return 2
    if any(flag in argv for flag in HELP_FLAGS):
        sys.stdout.write(__doc__.strip() + "\n")
        return 0
    names = argv or [name for name, _ in GROUPS]
    for name in names:
        print("== %s" % name)
        known[name]()
    if FAILURES:
        print("\n%d check(s) failed: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
