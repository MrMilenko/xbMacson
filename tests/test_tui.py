"""Drive the real curses TUI in a pseudo terminal against the fake server."""

import os
import re
import select
import subprocess
import sys
import time
import unittest

import helpers

try:
    import fcntl
    import pty
    import struct
    import termios
except ImportError:  # Windows
    pty = None

ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")


@unittest.skipIf(pty is None, "needs a pty (macOS or Linux)")
class TuiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.box = helpers.og_box()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        env = dict(os.environ, TERM="xterm-256color")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(helpers.ROOT, "xbmacson.py"), self.box.target],
            stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
        os.close(slave)
        self.master = master
        self.screen = b""

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)
        os.close(self.master)
        self.box.stop()

    def read_until(self, text, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                self.screen += chunk
            if text.encode() in ANSI_RE.sub(b"", self.screen):
                return True
        self.fail(f"{text!r} never appeared; screen tail: "
                  f"{ANSI_RE.sub(b'', self.screen)[-600:]!r}")

    def wait_exit(self, timeout=8):
        """Keep draining the pty while waiting: a full pty buffer would block
        the child's last writes and it would never exit."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                return self.proc.returncode
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    os.read(self.master, 65536)
                except OSError:
                    pass
        self.fail("TUI did not exit")

    def key(self, ch):
        os.write(self.master, ch.encode() if isinstance(ch, str) else ch)

    def test_monitor_keys(self):
        self.read_until("Connected to 127.0.0.1 (fakebox).")
        self.assertTrue(self.box.wait_for_channel())
        self.read_until("LIVE")
        self.box.push("debugstr thread=4 lf string=hello from the fake box")
        self.read_until("hello from the fake box")
        # Stop bar on a second-chance exception, then C continues.
        self.box.threads[28].stopped = True
        self.box.push("exception stop code=0xc0000005 thread=28 address=0x00011234 read=0x0")
        self.read_until("STOPPED: Exception (Access Violation) (thread 28)")
        self.read_until("C:Continue")
        self.key("c")
        self.read_until("Execution resumed.")
        self.assertIn("continue thread=0x1c", self.box.commands)
        # Raw mode shows the wire line.
        self.screen = b""
        self.key("r")
        self.read_until("RAW")
        self.read_until("debugstr thread=4 lf string=hello from the fake box")
        self.key("r")
        # Info screen.
        self.screen = b""
        self.key("i")
        self.read_until("DBGNAME")
        self.read_until("fakebox")
        self.key("\x1b")
        # File browser: drives, then a directory.
        self.screen = b""
        self.key("b")
        self.read_until("E:\\")
        self.key("\x1b")
        self.read_until("B:Browse")
        self.key("q")
        self.assertEqual(self.wait_exit(), 0)
        self.assertTrue(any(c.startswith("notifyat") and c.endswith(" drop")
                            for c in self.box.commands))


if __name__ == "__main__":
    unittest.main()
