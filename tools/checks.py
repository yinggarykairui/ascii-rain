#!/usr/bin/env python3
"""Check the things about ascii-rain a person cannot check by looking.

A development tool, not part of the program. It drives `ascii_rain.py` under a
pseudo-terminal and asserts the behaviour that only shows up in exit statuses,
terminfo capabilities and byte streams:

    python3 tools/checks.py            # every group
    python3 tools/checks.py signals    # exit status per signal
    python3 tools/checks.py dumb       # the TERM=dumb refusal
    python3 tools/checks.py args       # argument handling, including `--`
    python3 tools/checks.py resize     # the field across a resize
    python3 tools/checks.py tiers      # the no-`dim` tail
    python3 tools/checks.py --help     # this text

Exit status is 0 if every check in the named groups passed, 1 otherwise, and 2
for a word that is not a group. Only the `tiers` group needs a dependency
(`pip install pyte`); the other three run on the standard library alone, and
`tiers` is the slow one — it averages lit-cell counts over several runs on a
480x120 grid because the thing it measures is a percentage.

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
    for name, value in (env_extra or {}).items():
        # A None value means "unset this", which env_extra could not otherwise
        # express — and an unset TERM is one of the states being checked.
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
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


def wait_within(pid, seconds, drain=None):
    """Reap `pid` if it ends inside `seconds`. Returns (status or None, waited).

    None means it outlasted the deadline and was SIGKILLed — which is itself
    the finding, so it is reported rather than raised. `drain` is the pty
    master to keep emptying meanwhile: a full pty buffer blocks the child's
    next write, which would look exactly like the hang being measured.
    """
    started = time.time()
    while time.time() - started < seconds:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return status, time.time() - started
        if drain is not None:
            ready, _, _ = select.select([drain], [], [], 0.02)
            if ready:
                try:
                    os.read(drain, 65536)
                except OSError:
                    pass
        else:
            time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    return None, time.time() - started


def describe_status(status):
    if status is None:
        return "outlasted the deadline, SIGKILLed"
    if os.WIFSIGNALED(status):
        return "killed by signal %d" % os.WTERMSIG(status)
    return "exited %d" % os.WEXITSTATUS(status)


# A signal landing inside importlib's machinery — most often the weakref
# callback that releases a module lock, which the garbage collector can run at
# any allocation — used to raise where the exception had nowhere to go: CPython
# printed `Exception ignored in: <function _get_module_lock.<locals>.cb ...>`
# and a traceback naming ascii_rain.py, about one startup-signalled run in a
# hundred. Holding the import lock and signalling makes that window
# deterministic instead of one-in-a-hundred.
IMPORT_LOCK_PROBE = """
import _imp, os, signal, sys
sys.path.insert(0, {root})
import ascii_rain
ascii_rain.install_signal_handlers()
_imp.acquire_lock()
try:
    os.kill(os.getpid(), signal.SIGINT)
finally:
    _imp.release_lock()
sys.stdout.write("shutdown=" + str(ascii_rain.SHUTDOWN))
"""


def check_import_lock_handler():
    probe = subprocess.run(
        [sys.executable, "-c", IMPORT_LOCK_PROBE.format(root=repr(ROOT))],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    err = probe.stderr.decode("utf-8", "replace")
    check("signals: a signal during an import prints nothing",
          probe.returncode == 0 and err == ""
          and probe.stdout.decode() == "shutdown=%d" % int(signal.SIGINT),
          "exit %d, stdout %r, stderr %r"
          % (probe.returncode, probe.stdout.decode()[:40], err.strip()[:60]))


def group_signals():
    check_import_lock_handler()
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

    # COLUMNS and LINES that no window could have. ncurses believes them over
    # the terminal's ioctl, and 9999x9999 used to hang initscr() before the
    # program owned a single exit path: 30 seconds, one byte of output, a
    # keypress ignored, SIGINT and SIGTERM ignored, SIGKILL the only way out
    # and the terminal left in raw mode. Both variables are exported routinely
    # by scripts and CI, so this is the ordinary case, not an exotic one.
    absurd = {"COLUMNS": "9999", "LINES": "9999"}
    pid, master, _ = spawn(term=term, env_extra=absurd)
    drawn = read_for(master, 1.5)
    check("signals: COLUMNS=9999 LINES=9999 still draws", len(drawn) > 5120,
          "%d bytes in 1.5 s" % len(drawn))
    os.write(master, b"q")
    status, waited = wait_within(pid, 1.0, drain=master)
    os.close(master)
    check("signals: COLUMNS=9999 LINES=9999 exits on a keypress within a second",
          status is not None and os.WIFEXITED(status)
          and os.WEXITSTATUS(status) == 0,
          "%.2f s, %s" % (waited, describe_status(status)))

    pid, master, _ = spawn(term=term, env_extra=absurd)
    read_for(master, 1.0)
    os.kill(pid, signal.SIGTERM)
    status, waited = wait_within(pid, 2.0, drain=master)
    os.close(master)
    check("signals: COLUMNS=9999 LINES=9999 dies on SIGTERM",
          status is not None and os.WIFSIGNALED(status)
          and os.WTERMSIG(status) == signal.SIGTERM,
          "%.2f s, %s" % (waited, describe_status(status)))


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

    # An unset TERM and an empty one were both reported as `TERM=(unset)`, and
    # both blamed the terminfo database for a variable nobody set.
    for label, env_extra, wanted in (
            ("unset", {"TERM": None}, "TERM is not set"),
            ("empty", {"TERM": ""}, "TERM is set but empty")):
        pid, master, err = spawn(term="xterm-256color", pipe_stderr=True,
                                 env_extra=env_extra)
        out = read_for(master, 2.0, stop_when_idle=True)
        message = os.read(err, 65536).decode("utf-8", "replace")
        _, status = os.waitpid(pid, 0)
        os.close(master)
        os.close(err)
        lines = [line for line in message.splitlines() if line]
        check("dumb: TERM %s exits 2 and says so" % label,
              os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
              and len(lines) == 1 and wanted in lines[0]
              and "terminfo database" not in lines[0] and out == b"",
              "exit %s: %s" % (status, message.strip()[:80]))

    # TERM arrives from the client over ssh, so a refusal quotes it the way
    # every other user-supplied value is quoted. A bare %s let `TERM=$'\e[2J'`
    # repaint the screen it was being complained about on, an embedded newline
    # made two lines of one refusal, and a 4096-character TERM printed 4272
    # bytes.
    for label, value in (("an escape sequence", "\x1b[2J\x1b[31mPWNED"),
                         ("a newline", "xterm\nSECOND"),
                         ("4096 characters", "x" * 4096)):
        pid, master, err = spawn(term=value, pipe_stderr=True)
        out = read_for(master, 2.0, stop_when_idle=True)
        message = os.read(err, 65536).decode("utf-8", "replace")
        _, status = os.waitpid(pid, 0)
        os.close(master)
        os.close(err)
        lines = [line for line in message.splitlines() if line]
        check("dumb: a TERM holding %s is quoted, not printed" % label,
              os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2
              and len(lines) == 1 and len(message) < 300
              and "\x1b" not in message and out == b"",
              "exit %s, %d line(s), %d bytes: %s"
              % (status, len(lines), len(message), message.strip()[:60]))

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

    # argparse accepts unambiguous prefixes, so every accepted spelling of a
    # flag has to behave the same. `--sp -- 2` used to eat the -- and report
    # `unexpected argument: '2'`.
    for args in (["--sp", "--", "2"], ["--cha", "--", "ascii"]):
        code, _, err = cli(args)
        check("args: %s is a missing value, like the long spelling"
              % " ".join(args),
              code == 2 and "expected one argument" in err,
              "exit %d: %s" % (code, err.strip()[:70]))

    # One wording for one mistake: a word the program has no use for, however
    # it arrived. argparse's own plural ("unrecognized arguments: a") was the
    # second voice.
    for args in (["a"], ["--", "a"], ["--nonsense"]):
        code, _, err = cli(args)
        check("args: %s is one 'unexpected argument' line" % " ".join(args),
              code == 2 and "unexpected argument" in err
              and len([line for line in err.splitlines() if line]) == 1,
              "exit %d: %s" % (code, err.strip()[:70]))

    # --help and --version used to fire the instant argparse reached them and
    # exit 0 over the top of the mistake standing next to them.
    for args in (["--version", "--bogus"], ["--help", "--bogus"]):
        code, out, err = cli(args)
        check("args: %s exits 2 naming the mistake" % " ".join(args),
              code == 2 and "--bogus" in err and out == "",
              "exit %d: %s" % (code, err.strip()[:70]))

    # A number quoted back has to be the one that was typed. `--speed 2_0`
    # reported "got 20", which is what float() made of it.
    code, _, err = cli(["--speed", "2_0"])
    check("args: --speed 2_0 is quoted as typed",
          code == 2 and "2_0" in err and "got 20" not in err,
          "exit %d: %s" % (code, err.strip()[:70]))

    # The day-019 refusals. The `--` change rewrites the argv every one of
    # these travels through, so they are re-run as a regression sweep.
    refusals = [
        (["--speed", "99"], "--speed"),
        (["--speed", "abc"], "--speed"),
        (["--charset", "nope"], "--charset"),
        (["--color", "nope"], "--color"),
        (["--charset", "custom:   "], "custom"),
        (["--nonsense"], "unexpected argument"),
    ]
    for args, wanted in refusals:
        code, out, err = cli(args)
        lines = [line for line in err.splitlines() if line]
        check("args: %s exits 2 with one line" % " ".join(args),
              code == 2 and len(lines) == 1 and wanted in err
              and "Traceback" not in err and out == "",
              "exit %d, %d line(s): %s" % (code, len(lines), err.strip()[:70]))

    # A bad *value* next to --help used to be read past: `--speed 99 --help`
    # printed the help and exited 0, while `--bogus --help` exited 2. One
    # sentence in the README, two behaviours in the program.
    for args in (["--speed", "99", "--help"], ["--charset", "zzz", "--help"],
                 ["--color", "zzz", "-h"], ["--speed", "99", "--version"]):
        code, out, err = cli(args)
        check("args: %s exits 2, not 0" % " ".join(args),
              code == 2 and out == "" and args[1] in err,
              "exit %d: %s" % (code, err.strip()[:70]))

    # A custom pool that loses glyphs says so, in the voice the non-UTF-8
    # locale message already uses. Silence read as the program ignoring what
    # was asked for.
    code, _, err = cli(["--charset", "custom:A\U0001f327B"])
    check("args: a custom: pool names the glyphs it dropped",
          "1 of 3 custom glyphs" in err,
          err.strip().splitlines()[0][:80] if err.strip() else "(silent)")

    # The emoji fence is the emoji blocks, not the whole of plane 1. U+1F130
    # (Ambiguous) and U+1F0A1 (Neutral) are narrow and stay; the rain cloud is
    # drawn wide by every terminal and goes, whatever the width table says.
    for glyphs, wanted in (("\u2460", True), ("\u0416", True), ("\u250c", True),
                           ("\U0001f130", True), ("\U0001f0a1", True),
                           ("\U0001f327", False), ("\U0001f600", False)):
        code, _, err = cli(["--charset", "custom:" + glyphs])
        accepted = "stdout is not a terminal" in err
        check("args: custom:%s is %s" % (ascii(glyphs).strip("'"),
                                         "accepted" if wanted else "refused"),
              accepted == wanted, err.strip()[:70])

    # repr() escapes control characters; it does not bound length, and a
    # 5 KB argument printed 5 KB of stderr.
    code, _, err = cli(["--color", "Z" * 5000])
    check("args: a very long value is quoted short",
          code == 2 and len(err) < 300 and "truncated" in err,
          "%d characters of stderr" % len(err))

    for args in (["--help"], ["--version"]):
        code, out, err = cli(args)
        check("args: %s exits 0" % args[0], code == 0 and out and err == "",
              "exit %d" % code)


# --------------------------------------------------------------------------
# group: tiers


# How many runs each density figure averages. Every column picks its own length
# and phase, so a single settled grid varies by 3-5 % run to run on the grid
# below; eight runs put the standard error of a mean near 1 %, which is what
# makes a 10 % threshold an assertion rather than a coin toss. The effect being
# asserted measures ~17 % on this instrument, so the margin is three standard
# errors or so — the reason the grid is this wide and the count this high.
DENSITY_RUNS = 12

# The density figures are counted on a grid this wide rather than the 100x30 of
# the other groups. Each column contributes its own random length and phase, so
# the noise in a lit-cell count falls with the number of columns on screen —
# 960 of them costs about two seconds a run and cuts the spread from 5-12 % to
# 2-4 %.
DENSITY_COLS, DENSITY_ROWS = 960, 120

# Long enough for the settled grid to be the one being counted; the field warm
# starts, so it is settled from the first frame and this is drain time.
DENSITY_SECONDS = 1.0


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


def interleaved_means(cases, runs=DENSITY_RUNS):
    """Mean lit cells for several (label, term, program) cases, sampled round
    robin.

    Order matters here. Measuring every run of one case and then every run of
    the next lets whatever the machine is doing during the first block bias it
    against the second, and the figures below are differences between blocks a
    couple of minutes apart. Interleaving pairs them in time instead, so a
    drift lands on every case at once and cancels out of the ratio.
    """
    counts = dict((label, []) for label, _, _ in cases)
    for _ in range(runs):
        for label, term, program in cases:
            counts[label].append(lit_cells(
                settled_capture(term, program, seconds=DENSITY_SECONDS,
                                cols=DENSITY_COLS, rows=DENSITY_ROWS),
                DENSITY_COLS, DENSITY_ROWS))
    return dict((label, (sum(c) / float(len(c)), c))
                for label, c in counts.items())


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


# ncurses keeps the first terminfo entry it is handed for the life of the
# process, so asking about a second TERM in-process answers about the first.
# One child per terminal is the only honest way to read this.
TERMINFO_PROBE = (
    "import curses;curses.setupterm();"
    "print(curses.tigetstr('dim') is not None, curses.tigetnum('ncv'))"
)


def branch_of(term):
    """Which tail branch `term` takes, and the terminfo reason, as a sentence.

    ascii_rain.has_dim() asks terminfo two questions: is there a `dim` string,
    and does `ncv` (no_color_video) mask dim while a colour pair is in use.
    This mirrors that so the printed reasoning names the reason rather than the
    conclusion.
    """
    env = dict(os.environ)
    env["TERM"] = term
    out = subprocess.check_output([sys.executable, "-c", TERMINFO_PROBE], env=env)
    has_string, ncv = out.decode().split()
    ncv = int(ncv)
    if has_string != "True":
        return "density", "no dim string"
    if ncv > 0 and ncv & 16:
        return "density", "dim string, but ncv#%d masks dim under colour" % ncv
    return "dim", "dim string, ncv#%d does not mask it" % ncv


def group_tiers():
    try:
        import pyte  # noqa: F401
    except ImportError:
        check("tiers: pyte is installed", False, "pip install pyte")
        return

    # Which terminal stands for which branch, and why. Every number below is
    # read off one of these three, so the mapping is asserted rather than
    # assumed — a terminfo change on the machine running this would otherwise
    # silently move a terminal between branches and leave the checks passing.
    #
    # NOTE: done-checklist item 4 (day 038) names TERM=linux as the *dim*
    # reference — "the TERM=linux capture still contains the dim SGR". That
    # wording is superseded. `linux` has a dim string and also ncv#18, which
    # tells ncurses dim cannot be combined with colour; ascii-rain always has a
    # colour pair in use there, so ncurses dropped A_DIM and the tail rendered
    # byte-identical to the body. has_dim() is ncv-aware now and `linux` takes
    # the density branch. The item's 10 % is asserted below against
    # xterm-256color, which really does emit dim, and against the day-019
    # binary on each density terminal, which is the same comparison without a
    # per-terminal offset in it.
    branches = {}
    for term, wanted in (("xterm-256color", "dim"), ("linux", "density"),
                         ("vt100", "density")):
        branch, why = branch_of(term)
        branches[term] = branch
        check("tiers: TERM=%s is the %s branch" % (term, wanted),
              branch == wanted, "terminfo says: %s" % why)

    dim_capture = settled_capture("xterm-256color")
    check("tiers: the dim terminal emits the dim SGR",
          has_dim_sgr(dim_capture), "TERM=xterm-256color")
    for term in ("vt100", "linux"):
        check("tiers: no dim SGR at TERM=%s" % term,
              not has_dim_sgr(settled_capture(term)))

    baseline = day019_program()
    if baseline is None:
        check("tiers: the day-019 baseline is available", False,
              "git show %s:ascii_rain.py failed - item 4 needs it as the "
              "calibration standard, so the density checks below are not run "
              "rather than being quietly turned into something weaker"
              % DAY019_SHA[:7])
        model_tiers()
        model_dim_path_ignores_survival()
        model_tail_never_returns()
        return
    terms = ("xterm-256color", "vt100", "linux")
    try:
        means = interleaved_means(
            [("now/" + term, term, PROGRAM) for term in terms]
            + [("019/" + term, term, baseline) for term in terms])
    finally:
        os.unlink(baseline)
    now = dict((term, means["now/" + term]) for term in terms)
    old = dict((term, means["019/" + term]) for term in terms)
    for term in terms:
        note("tiers: TERM=%-14s draws %.0f lit cells, drew %.0f at day 019"
             % (term, now[term][0], old[term][0]))
        note("       now %s" % (now[term][1],))
        note("       019 %s" % (old[term][1],))

    # Item 4's 10 %, against xterm-256color, the terminal that really does emit
    # dim. Counting lit cells off a settled grid carries a per-terminal offset:
    # day 019's tail was identical everywhere, and its captures still differ by
    # several percent from one terminal to the next. So day 019 is the
    # calibration standard. Dividing each terminal's own day-019 count out of
    # its count today removes the offset and leaves the tail, and the dim
    # terminal's own drift (checked below, and ~0) normalises what is left.
    # Both the corrected figure and the raw cross-terminal one are printed;
    # the corrected one is the one asserted, because the raw one is the effect
    # minus an offset of roughly the same size as the margin. For scale: four
    # consecutive runs of this check measured 12.4, 13.8, 14.4 and 14.7 %
    # against the checklist's 10 %, so a reading near 11 % is an unlucky
    # afternoon and a reading near 5 % is a regression.
    dim_now, dim_old = now["xterm-256color"][0], old["xterm-256color"][0]
    dim_drift = dim_now / dim_old if dim_old else 0.0
    for term in ("vt100", "linux"):
        offset = old[term][0] / dim_old if dim_old else 1.0
        raw = 1.0 - (now[term][0] / dim_now) if dim_now else 0.0
        corrected = 1.0 - (now[term][0] / old[term][0]) / dim_drift \
            if old[term][0] and dim_drift else 0.0
        note("tiers: TERM=%s captures %.1f%% more cells than TERM=xterm-256color "
             "for the same cell set (day 019, both binaries drawing every "
             "in-trail cell)" % (term, (offset - 1) * 100))
        check("tiers: TERM=%s draws at least 10%% fewer cells than the dim "
              "terminal" % term, corrected >= 0.10,
              "%.1f%% fewer with that offset divided out (%.1f%% raw)"
              % (corrected * 100, raw * 100))

    # The dim path is unchanged. Tolerance 10 %, which is strictly below the
    # ~17 % the thinning measures on this same instrument — the old +/-20 %
    # could not fail the way it existed to fail — and about ten standard errors
    # above the noise in an eight-run mean.
    off = abs(dim_now - dim_old) / dim_old if dim_old else 1.0
    check("tiers: the dim path is unchanged from day 019", off <= 0.10,
          "at TERM=xterm-256color: day 019 %.0f, now %.0f, %.1f%% apart"
          % (dim_old, dim_now, off * 100))

    model_tiers()
    model_dim_path_ignores_survival()
    model_tail_never_returns()


class Recorder:
    """A stand-in for the curses window that records writes instead of drawing.

    `Drop.draw` only ever calls addstr/insstr through `_put`, so this is enough
    to read the draw path's decisions straight off the real code — no terminal,
    no emulator, nothing between the claim and the answer.
    """

    def __init__(self):
        self.cells = []

    def addstr(self, y, x, glyph, attr):
        self.cells.append((y, x, glyph, attr))

    insstr = addstr


def seeded_field(rain, height, width, seed):
    import random

    random.seed(seed)
    return [rain.Drop(height, rain.CHARSETS["matrix"], 1.0, True)
            for _ in range(width)]


def load_rain():
    sys.path.insert(0, ROOT)
    import ascii_rain

    return ascii_rain


def stub_color_pairs():
    """Let Drop.draw run with no terminal under it. Returns the undo.

    `curses.color_pair` refuses to answer before initscr(), and the model
    checks below have no screen at all. Which cells get written is all they
    read, and that does not depend on the attribute value, so color_pair
    becomes arithmetic for the duration.
    """
    import curses

    real = curses.color_pair
    curses.color_pair = lambda pair: pair << 8

    def undo():
        curses.color_pair = real

    return undo


def model_tiers():
    """The 10 % figure, asserted on the field itself rather than on a capture.

    This runs `ascii_rain`'s own Drop objects in this process — no terminal, no
    emulator — and counts, on a settled field, the cells the dim path would
    draw against the cells the thinned path draws. It is the same predicate
    `Drop.draw` uses, read off the same objects, so there is nothing between
    the claim and the number.
    """
    rain = load_rain()

    # Seeded, so this number is the same on every machine and every run.
    height, width = 60, 240
    field = seeded_field(rain, height, width, 20260901)
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


def model_dim_path_ignores_survival():
    """The dim path is unchanged from day 019, asserted structurally.

    Day 019 had no survival flag, so "unchanged" means the draw path must not
    consult one while dim is in use. Rather than measure that, force every
    cell's flag to True and then to False and compare what `Drop.draw` writes:
    identical under dim, different without it. A number can drift; this cannot.
    """
    rain = load_rain()
    undo = stub_color_pairs()
    height, width = 40, 60
    for dim, want_same in ((True, True), (False, False)):
        drawn = []
        for flag in (True, False):
            field = seeded_field(rain, height, width, 20260902)
            for _ in range(120):
                for drop in field:
                    if drop.advance(height):
                        drop.reset(height, 1.0)
            for drop in field:
                drop.cells = dict((row, (glyph, flag))
                                  for row, (glyph, _) in drop.cells.items())
            window = Recorder()
            for x, drop in enumerate(field):
                drop.draw(window, x, height, width, True, dim)
            drawn.append(window.cells)
        same = drawn[0] == drawn[1]
        check("tiers: the draw path %s the survival flag when dim is %s"
              % ("ignores" if want_same else "uses", "in use" if dim else "off"),
              same == want_same,
              "%d cells either way" % len(drawn[0]) if same
              else "%d vs %d cells" % (len(drawn[0]), len(drawn[1])))
    undo()


def model_tail_never_returns():
    """Decided once, so the trail dissolves rather than strobes.

    The per-cell coin flip is taken when the cell is born and never re-taken;
    a per-frame re-roll would score identically on every density figure above
    and look completely different on screen. The observable difference is this:
    a cell that has once been held back must stay held back for the rest of
    that drop's life. Churn swaps a cell's glyph, so the cell is followed by
    row and by life, not by what it is showing.
    """
    rain = load_rain()
    undo = stub_color_pairs()
    height, width = 40, 80
    field = seeded_field(rain, height, width, 20260903)
    lives = [0] * width
    held_back = set()
    offences = []
    for _ in range(400):
        for index, drop in enumerate(field):
            if drop.advance(height):
                drop.reset(height, 1.0)
                lives[index] += 1
        for index, drop in enumerate(field):
            window = Recorder()
            drop.draw(window, index, height, width, True, False)
            shown = set(y for y, _, _, _ in window.cells)
            head_row = int(drop.head)
            for row in drop.cells:
                distance = head_row - row
                if distance < 0 or distance >= drop.length:
                    continue
                key = (index, lives[index], row)
                if row in shown:
                    if key in held_back:
                        offences.append(key)
                else:
                    held_back.add(key)
    undo()
    check("tiers: a tail cell, once held back, is never drawn again",
          not offences,
          "%d cell(s) held back and then drawn, e.g. %s"
          % (len(offences), offences[:3]) if offences
          else "%d cells held back across the run" % len(held_back))

# --------------------------------------------------------------------------
# group: resize


def drawn_cells(field, height, width):
    """The cells `Drop.draw` would light on this field, as a set.

    Read off the real draw path with the dim branch chosen, so the per-cell
    coin flip of the thinned tail is not in the number.
    """
    lit = set()
    for x, drop in enumerate(field[:width]):
        window = Recorder()
        drop.draw(window, x, height, width, True, True)
        for y, column, _, _ in window.cells:
            lit.add((y, column))
    return lit


def settled_field(rain, width, height, seed, frames=240):
    import random

    random.seed(seed)
    field = rain.build_field(width, height, rain.CHARSETS["matrix"], 1.0, True)
    for _ in range(frames):
        for drop in field:
            if drop.advance(height):
                drop.reset(height, 1.0)
    return field


def group_resize():
    """A shrink keeps the columns falling, instead of emptying the field.

    README: "the columns already falling keep falling, only the new ones are
    born, and trail lengths rescale with the new height". Rescaling the trail
    while leaving `head` on its old absolute row made that false in the one
    direction it is visible: on a 100x30 -> 40x12 shrink a median of 16 of the
    40 surviving columns had their whole trail below the new last row, drew
    nothing, and were then recycled from up to 1.5 screens above the top. Lit
    density measured 0.27 before the shrink and 0.08 after it, for two to
    three seconds.

    Both numbers here are read off the program's own Drop objects in this
    process — no terminal, no emulator — and seeded, so they are the same on
    every machine.
    """
    rain = load_rain()
    undo = stub_color_pairs()
    (wide, tall), (narrow, short) = (100, 30), (40, 12)
    ratios = []
    stranded_total = 0
    for seed in (20260904, 20260905, 20260906, 20260907, 20260908):
        field = settled_field(rain, wide, tall, seed)
        before = len(drawn_cells(field, tall, wide)) / float(wide * tall)
        field = rain.build_field(narrow, short, rain.CHARSETS["matrix"], 1.0,
                                 True, previous=field)
        after = len(drawn_cells(field, short, narrow)) / float(narrow * short)
        # A column that draws nothing is only honest if it has not arrived
        # yet: a head still above the top row. One below the bottom is the
        # bug — the column is falling where no one can see it.
        stranded = sum(1 for drop in field[:narrow]
                       if int(drop.head) >= short
                       and not drawn_cells([drop], short, 1))
        stranded_total += stranded
        ratios.append(after / before)
        note("resize: seed %d: %.3f lit at 100x30, %.3f at 40x12 the instant "
             "after (%.0f%%), %d column(s) stranded below the new floor"
             % (seed, before, after, 100 * after / before, stranded))
    ratios.sort()
    median = ratios[len(ratios) // 2]
    check("resize: no column is left falling below the new bottom row",
          stranded_total == 0, "%d stranded across %d shrinks"
          % (stranded_total, len(ratios)))
    check("resize: a shrink keeps at least 70% of the lit density",
          median >= 0.70, "median %.0f%% of the pre-shrink density (%s)"
          % (median * 100, ", ".join("%.0f%%" % (r * 100) for r in ratios)))

    # The other direction, and the case that is not a resize at all: a grow
    # must not strand anything either, and re-fitting to the same height must
    # leave the field alone rather than re-lighting it.
    field = settled_field(rain, narrow, short, 20260909)
    before = drawn_cells(field, short, narrow)
    field = rain.build_field(narrow, short, rain.CHARSETS["matrix"], 1.0, True,
                             previous=field)
    check("resize: a redraw at the same size changes nothing",
          drawn_cells(field, short, narrow) == before,
          "%d cells before, %d after"
          % (len(before), len(drawn_cells(field, short, narrow))))

    grown = rain.build_field(wide, tall, rain.CHARSETS["matrix"], 1.0, True,
                             previous=field)
    stranded = sum(1 for drop in grown[:wide]
                   if int(drop.head) >= tall and not drawn_cells([drop], tall, 1))
    check("resize: a grow strands nothing either", stranded == 0,
          "%d stranded" % stranded)
    undo()


# --------------------------------------------------------------------------

GROUPS = [
    ("signals", group_signals),
    ("dumb", group_dumb),
    ("args", group_args),
    ("resize", group_resize),
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
