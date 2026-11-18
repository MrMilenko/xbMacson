"""Typed XBDM commands on top of a Connection."""

import os
import re
import struct
import threading
import time

from . import screenshot as _shot
from .connection import Connection, split_host
from .profiles import get_profile
from .protocol import (
    ERR_CANNOT_CREATE, ERR_NOT_STOPPED, ERR_UNKNOWN_COMMAND,
    ConnectError, XbdmError, parse_kv, qword, quote, to_int,
)


class Module:
    __slots__ = ("name", "base", "size", "check", "timestamp", "flags")

    def __init__(self, name, base, size, check=0, timestamp=0, flags=()):
        self.name = name
        self.base = base
        self.size = size
        self.check = check
        self.timestamp = timestamp
        self.flags = tuple(flags)

    @property
    def end(self):
        return self.base + self.size

    @property
    def is_xbe(self):
        return "xbe" in self.flags

    def contains(self, addr):
        return self.base <= addr < self.end

    @classmethod
    def from_kv(cls, kv):
        flags = [k for k, v in kv.items() if v is True]
        return cls(kv.get("name", "?"), to_int(kv.get("base"), 0),
                   to_int(kv.get("size"), 0), to_int(kv.get("check"), 0),
                   to_int(kv.get("timestamp"), 0), flags)

    def as_dict(self):
        return {"name": self.name, "base": self.base, "size": self.size,
                "flags": list(self.flags)}


class DirEntry:
    __slots__ = ("name", "size", "is_dir")

    def __init__(self, name, size, is_dir):
        self.name = name
        self.size = size
        self.is_dir = is_dir

    def as_dict(self):
        return {"name": self.name, "size": self.size, "is_dir": self.is_dir}


class Context(dict):
    """Register values keyed by lower-case name (eip, esp, ebp ... or
    iar, lr, gpr1 ...)."""

    def pc(self, profile):
        return self.get(profile.pc_reg)

    def sp(self, profile):
        return self.get(profile.sp_reg)

    def fp(self, profile):
        return self.get(profile.fp_reg)


X86_REG_ORDER = ["eip", "esp", "ebp", "eflags", "eax", "ebx", "ecx", "edx",
                 "esi", "edi", "cr0npxstate"]


def parse_context(lines):
    ctx = Context()
    for line in lines:
        for k, v in parse_kv(line).items():
            n = to_int(v)
            if n is not None:
                ctx[k] = n
    return ctx


def parse_drivelist(reply):
    """Drive letters from a drivelist reply.

    Seen in two shapes: one status line of letters (``200- CDEFXYZ``) or a
    202 body of ``drivename="C"`` lines.
    """
    drives = []
    for line in reply.lines:
        name = parse_kv(line).get("drivename")
        if isinstance(name, str):
            drives.append(name)
    if drives:
        return sorted(drives)
    text = reply.text.strip()
    return sorted(set(ch.upper() for ch in text if ch.isalpha()))


def parse_dirlist(lines):
    entries = []
    for line in lines:
        kv = parse_kv(line)
        name = kv.get("name")
        if not isinstance(name, str):
            continue
        is_dir = kv.get("directory") is True
        size = 0 if is_dir else qword(kv, "sizelo", "sizehi")
        entries.append(DirEntry(name, size, is_dir))
    entries.sort(key=lambda e: (0 if e.is_dir else 1, e.name.lower()))
    return entries


class Console:
    """A console reachable over XBDM.

    Commands run on one lazily opened connection guarded by a lock, so the
    object can be shared between threads. If the console resets, the next
    command reconnects once before giving up.
    """

    def __init__(self, target, profile="og", timeout=5.0, port=None):
        self.profile = get_profile(profile)
        host, p = split_host(target, self.profile.port)
        self.host = host
        self.port = port or p
        self.timeout = timeout
        self._conn = None
        self._lock = threading.RLock()
        # Decided on first use by get_context(suspend="auto").
        self.context_needs_suspend = None

    # -- connection ------------------------------------------------------

    def connect(self, timeout=None):
        return Connection(self.host, self.port, timeout or self.timeout).open()

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _run(self, fn):
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._conn is None:
                        self._conn = self.connect()
                    return fn(self._conn)
                except XbdmError:
                    raise
                except (OSError, ValueError) as e:
                    if self._conn is not None:
                        self._conn.close(send_bye=False)
                        self._conn = None
                    if isinstance(e, ConnectError):
                        raise
                    if attempt:
                        raise ConnectError(f"lost connection to {self.host}: {e}")

    def command(self, cmd, check=True):
        return self._run(lambda c: c.command(cmd, check=check))

    def text(self, cmd):
        return self.command(cmd).body()

    # -- info ------------------------------------------------------------

    def dbgname(self):
        return self.text("dbgname")

    def dmversion(self):
        return self.text("dmversion")

    def consoletype(self):
        return self.text("consoletype")

    def systeminfo(self):
        return parse_kv(" ".join(self.command("systeminfo").lines))

    def xbeinfo(self, name="running"):
        arg = "running" if name == "running" else f"name={quote(name)}"
        return parse_kv(" ".join(self.command(f"xbeinfo {arg}").lines))

    def modules(self):
        return [Module.from_kv(parse_kv(line))
                for line in self.command("modules").lines]

    def module_for(self, addr, modules=None):
        for m in modules if modules is not None else self.modules():
            if m.contains(addr):
                return m
        return None

    # -- threads ---------------------------------------------------------

    def threads(self):
        out = []
        for line in self.command("threads").lines:
            n = to_int(line.strip())
            if n is not None:
                out.append(n)
        return out

    def thread_info(self, tid):
        kv = parse_kv(" ".join(self.command(f"threadinfo thread={tid:#x}").lines))
        info = {k: to_int(v, v) for k, v in kv.items()}
        return info

    def get_context(self, tid, suspend="auto", parts="control int"):
        """Read a thread's registers.

        ``suspend`` is "never", "always" or "auto". In auto mode the first call
        tries without suspending; if XBDM refuses (408 not stopped) every later
        call suspends, reads and resumes. ``context_needs_suspend`` records the
        outcome so callers can report the cost.
        """
        cmd = f"getcontext thread={tid:#x} {parts}"

        def plain(c):
            return parse_context(c.command(cmd).lines)

        def suspended(c):
            c.command(f"suspend thread={tid:#x}")
            try:
                return parse_context(c.command(cmd).lines)
            finally:
                c.command(f"resume thread={tid:#x}", check=False)

        if suspend == "always" or (suspend == "auto" and self.context_needs_suspend):
            return self._run(suspended)
        try:
            ctx = self._run(plain)
            if suspend == "auto" and self.context_needs_suspend is None:
                self.context_needs_suspend = False
            return ctx
        except XbdmError as e:
            if suspend != "auto" or e.code != ERR_NOT_STOPPED:
                raise
        self.context_needs_suspend = True
        return self._run(suspended)

    def isstopped(self, tid):
        r = self.command(f"isstopped thread={tid:#x}", check=False)
        if r.code == ERR_NOT_STOPPED:
            return False
        r.raise_for_error("isstopped")
        return True

    def suspend(self, tid):
        self.command(f"suspend thread={tid:#x}")

    def resume(self, tid):
        self.command(f"resume thread={tid:#x}")

    def continue_thread(self, tid, exception=False):
        extra = " exception" if exception else ""
        r = self.command(f"continue thread={tid:#x}{extra}", check=False)
        if r.code not in (200, ERR_NOT_STOPPED):
            r.raise_for_error("continue")

    def go(self):
        self.command("go", check=False)

    def stop(self):
        self.command("stop", check=False)

    # -- memory ----------------------------------------------------------

    def read_memory(self, addr, length):
        """Read target memory. Uses getmem2 (binary) and falls back to getmem
        (hex text) on consoles that do not know getmem2. Unreadable bytes in
        the hex form raise XbdmError 404."""
        if length <= 0:
            return b""
        cmd = f"getmem2 addr={addr:#x} length={length:#x}"
        try:
            return self._run(lambda c: c.command_binary(cmd, length=length)).data
        except XbdmError as e:
            if e.code != ERR_UNKNOWN_COMMAND:
                raise
        return self.read_memory_hex(addr, length)

    def read_memory_hex(self, addr, length):
        lines = self.command(f"getmem addr={addr:#x} length={length:#x}").lines
        text = "".join(line.strip() for line in lines)
        if "??" in text[:length * 2]:
            raise XbdmError(404, "memory not mapped", "getmem")
        return bytes.fromhex(text[:length * 2])

    def read_u32(self, addr):
        return struct.unpack(self.profile.endian + "I", self.read_memory(addr, 4))[0]

    # -- files -----------------------------------------------------------

    def drives(self):
        return parse_drivelist(self.command("drivelist"))

    def dirlist(self, path):
        return parse_dirlist(self.command(f"dirlist name={quote(path)}").lines)

    def get_file(self, remote):
        cmd = f"getfile name={quote(remote)}"
        return self._run(lambda c: c.command_binary(cmd, length_prefix=True)).data

    def send_file(self, remote, data):
        cmd = f"sendfile name={quote(remote)} length={len(data):#x}"
        return self._run(lambda c: c.command_send(cmd, data))

    def mkdir(self, path):
        self.command(f"mkdir name={quote(path)}")

    def delete(self, path, is_dir=False):
        self.command(f"delete name={quote(path)}" + (" dir" if is_dir else ""))

    def deploy(self, local, remote, reboot_if_in_use=True, status=None,
               reboot_wait=90.0, poll=3.0, retry_waits=(2, 3, 5, 8),
               sleep=None):
        """Upload ``local`` to ``remote``.

        If the console holds the file open (the running title is the one being
        replaced), XBDM answers 413. Then reboot to the dashboard, wait for
        XBDM, and retry a few times, because the dashboard can take a moment
        to let go of the file. Same approach as OXDK's 360 deploy.
        Returns the number of bytes sent.
        """
        say = status or (lambda msg: None)
        sleep = sleep or time.sleep
        with open(local, "rb") as f:
            data = f.read()
        try:
            self.send_file(remote, data)
            return len(data)
        except XbdmError as e:
            if e.code != ERR_CANNOT_CREATE or not reboot_if_in_use:
                raise
        say("file is in use on the console; rebooting")
        self.reboot_and_wait(self.profile.reboot_for_deploy, reboot_wait, poll,
                             sleep=sleep, status=say)
        last = None
        for wait in retry_waits:
            sleep(wait)
            try:
                self.send_file(remote, data)
                return len(data)
            except XbdmError as e:
                if e.code != ERR_CANNOT_CREATE:
                    raise
                last = e
                say(f"still in use; waiting {wait}s more")
        raise last

    # -- titles and power ------------------------------------------------

    def launch(self, path):
        return self.command(self.profile.launch_command(path), check=False) \
            .raise_for_error("magicboot")

    def reboot(self, warm=False, wait=False, stop=False, nodebug=False):
        cmd = "reboot"
        for flag, on in (("warm", warm), ("wait", wait), ("stop", stop),
                         ("nodebug", nodebug)):
            if on:
                cmd += " " + flag
        self.raw_oneshot(cmd)

    def raw_oneshot(self, cmd):
        """Send a command on its own connection and do not wait long for the
        answer: reboot commands often drop the link before replying."""
        try:
            c = Connection(self.host, self.port, self.timeout).open()
        except ConnectError:
            raise
        try:
            c.send_line(cmd)
            try:
                c.sock.settimeout(2.0)
                return c.read_reply()
            except OSError:
                return None
        finally:
            c.close(send_bye=False)
            self.close()

    def wait_until_up(self, timeout=90.0, poll=3.0, sleep=None):
        sleep = sleep or time.sleep
        deadline = time.monotonic() + timeout
        while True:
            try:
                c = Connection(self.host, self.port, min(self.timeout, 5.0)).open()
                try:
                    c.command("dbgname")
                finally:
                    c.close()
                return True
            except (OSError, XbdmError):
                pass
            if time.monotonic() >= deadline:
                return False
            sleep(poll)

    def reboot_and_wait(self, cmd="reboot", timeout=90.0, poll=3.0,
                        sleep=None, status=None):
        sleep = sleep or time.sleep
        self.raw_oneshot(cmd)
        sleep(poll)
        if not self.wait_until_up(timeout, poll, sleep=sleep):
            raise ConnectError("console did not come back after a reboot")
        if status:
            status("console is back")

    # -- notifications ---------------------------------------------------

    def notifyat(self, port, drop=False, debug=False):
        cmd = f"notifyat port={port}"
        if drop:
            cmd += " drop"
        if debug:
            cmd += " debug"
        return self.command(cmd, check=False)

    def debugger(self, connect=True):
        return self.command("debugger " + ("connect" if connect else "disconnect"),
                            check=False)

    # -- screenshot ------------------------------------------------------

    def screenshot(self):
        def grab(c):
            return c.command_binary("screenshot", header_line=True,
                                    size_key="framebuffersize")
        # A large frame can take a while on a busy console.
        with self._lock:
            old = self.timeout
            self.timeout = max(old, 15.0)
            try:
                self.close()
                reply = self._run(grab)
            finally:
                self.timeout = old
                self.close()
        meta = parse_kv(reply.text)
        return _shot.decode(meta, reply.data, self.profile.screenshot)

    def save_screenshot(self, directory_or_path):
        shot = self.screenshot()
        path = directory_or_path
        if os.path.isdir(path) or path.endswith(os.sep) or not path.lower().endswith(".png"):
            os.makedirs(path, exist_ok=True)
            path = os.path.join(path, time.strftime("xbox_%Y%m%d_%H%M%S.png"))
        else:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        return shot.save(path)

    # -- probe -----------------------------------------------------------

    def probe(self):
        """What the TUI's Info screen shows: (label, text) pairs."""
        out = []
        with self._lock:
            try:
                c = self.connect()
            except ConnectError as e:
                return [("Probe failed", str(e))]
            try:
                out.append(("Greeting", c.greeting))
                for cmd in ("dmversion", "dbgname", "xbeinfo running", "modules"):
                    try:
                        r = c.command(cmd, check=False)
                        body = r.body() if r.ok else f"{r.code}- {r.text}"
                        out.append((cmd.upper(), body))
                    except OSError as e:
                        out.append((cmd.upper(), f"error: {e}"))
                        break
            finally:
                c.close()
        return out


_ADDR_RE = re.compile(r"^(0x)?[0-9a-fA-F]+$")


def parse_address(text):
    """Accept ``0x1234``, ``1234`` (hex) or a decimal with a ``#`` prefix."""
    text = text.strip()
    if text.startswith("#"):
        return int(text[1:], 10)
    if not _ADDR_RE.match(text):
        raise ValueError(f"not an address: {text!r}")
    return int(text, 16)
