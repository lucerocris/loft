"""Typing into a background agent's session (there is no `claude send`).

Shared by the server (replies, queued prompts, pickers) and the `loft reply` CLI.
"""
import fcntl, os, pty, select, signal, struct, termios, time

from . import board as B
from . import paths

HOME = paths.HOME


def _attached(card, steps=(), idle=0.8, read_after=0.8):
    """Attach to a background agent in a hidden pty, wait for the screen to
    settle, write each step (a string of keys; "\x00SLEEP:<s>" pauses), and
    detach. The session keeps running, exactly as after `claude attach`.
    Returns the raw bytes the attach printed (for reading a prompt off it)."""
    env = B.account_env(card)
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(str(HOME))
        os.execvpe(paths.claude_bin(), [paths.claude_bin(), "attach", card["id"]], env)
    out = bytearray()
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))

        def drain(idle, limit):
            got, start, last = 0, time.time(), time.time()
            while time.time() - start < limit:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        return got
                    if not chunk:
                        return got
                    got += len(chunk); last = time.time(); out.extend(chunk)
                elif got and time.time() - last > idle:
                    return got
            return got

        # the first paint lands within ~1.5 s; a spinner may keep it from going idle
        if not drain(idle, 3.5) and not drain(idle, 8):
            raise RuntimeError("Couldn't open the agent's session.")
        for st in steps:
            if st.startswith("\x00SLEEP:"):
                drain(9, float(st[7:])); continue
            os.write(fd, st.encode())
            drain(0.35, 1.2)
        if steps:
            drain(read_after, 3)
    finally:
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        for _ in range(20):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
            except ChildProcessError:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
            except OSError:
                pass
    return bytes(out)


def send_to_agent(card, text):
    """Type text into a background agent's prompt and press Enter.

    There is no `claude send`, so this attaches briefly in a hidden pty, pastes
    the text (bracketed paste, so multi-line stays one message), presses Enter
    and detaches. Not for pickers: Enter there picks the highlighted row (see
    answer_route, which drives them with keys)."""
    _attached(card, ["\x1b[200~" + text + "\x1b[201~", "\x00SLEEP:0.4", "\r"], idle=1.2)
    return "Sent"
