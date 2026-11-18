"""A fake XBDM server for offline tests. Binds 127.0.0.1 only.

It speaks enough of the protocol for the library, the CLI and the TUI:
greeting, text, multiline and binary replies, uploads, notifyat dial-back
(which survives a simulated reboot), the dedicated notify channel, threads
with contexts, memory, modules, files and screenshots.

Tests drive it directly: ``push()`` sends a notification line, ``reboot()``
drops every channel and dials back later, fields like ``files_in_use`` and
``require_suspend`` shape how it answers.
"""

import socket
import struct
import threading
import time

from xbmacson.protocol import parse_kv, to_int
from xbmacson.screenshot import xenos_tiled_offset


class FakeThread:
    def __init__(self, tid, start=0x10000, base=0xD0000000, limit=0xCFFF0000,
                 priority=8, context=None, pcs=None):
        self.tid = tid
        self.start = start
        self.base = base
        self.limit = limit
        self.priority = priority
        self.suspend = 0
        self.stopped = False
        self.context = dict(context or {})
        self.pcs = list(pcs or [])   # cycled through on each getcontext
        self._pc_i = 0


class FakeXbdm:
    def __init__(self, profile="og", name="fakebox", version="1.00.5838.1"):
        self.profile = profile
        self.name = name
        self.version = version
        self.pc_reg = "Iar" if profile == "x360" else "Eip"
        self.endian = ">" if profile == "x360" else "<"
        self.threads = {}
        self.memory = []             # [(base, bytearray)]
        self.modules = []            # [dict(name, base, size, flags)]
        self.files = {}              # "E:\\path" -> bytes
        self.dirs = {"E:\\": None, "E:\\games\\": None}
        self.drives = "CDEXYZ"
        self.drivelist_multiline = False
        self.files_in_use = set()    # sendfile answers 413 until a reboot
        self.require_suspend = False # getcontext on a running thread -> 408
        self.running_xbe = "E:\\games\\halo\\default.xbe"
        self.screen = None           # (width, height, rgb bytes)
        self.reboot_downtime = 0.3
        self.commands = []           # every command line received
        self.reboots = 0
        self.launched = []
        self.greeting = "201- connected"

        self._lock = threading.RLock()
        self._listen = None
        self._accept_thread = None
        self._running = False
        self._down_until = 0.0
        self._registrations = []     # [(host, port)] notifyat, persistent
        self._channels = []          # live notification sockets
        self._cmd_socks = set()      # live command connections
        self._channel_event = threading.Condition(self._lock)
        self.port = 0

    # -- lifetime --------------------------------------------------------

    def start(self):
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(16)
        self.port = self._listen.getsockname()[1]
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self

    @property
    def target(self):
        return f"127.0.0.1:{self.port}"

    def stop(self):
        self._running = False
        try:
            self._listen.close()
        except OSError:
            pass
        with self._lock:
            for s in self._channels:
                _close(s)
            self._channels = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- test controls ---------------------------------------------------

    def add_thread(self, tid, **kw):
        t = FakeThread(tid, **kw)
        self.threads[tid] = t
        return t

    def add_memory(self, base, data):
        self.memory.append((base, bytearray(data)))

    def write_u32(self, addr, value):
        self._write(addr, struct.pack(self.endian + "I", value))

    def add_module(self, name, base, size, *flags):
        self.modules.append({"name": name, "base": base, "size": size, "flags": flags})

    def wait_for_channel(self, count=1, timeout=5.0):
        end = time.monotonic() + timeout
        with self._lock:
            while len(self._channels) < count:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._channel_event.wait(left)
            return True

    def push(self, line):
        """Send a notification line on every live channel."""
        data = (line + "\r\n").encode("latin-1")
        with self._lock:
            dead = []
            for s in self._channels:
                try:
                    s.sendall(data)
                except OSError:
                    dead.append(s)
            for s in dead:
                self._channels.remove(s)

    def reboot(self):
        """Drop every channel, refuse connections for a moment, then dial
        every notifyat registration again (XBDM keeps them across boots)."""
        with self._lock:
            self.reboots += 1
            self.files_in_use.clear()
            for t in self.threads.values():
                t.stopped = False
                t.suspend = 0
            for s in self._channels:
                _close(s)
            self._channels = []
            # A rebooting console drops its command connections too.
            for s in list(self._cmd_socks):
                _close(s)
            self._cmd_socks.clear()
            self._down_until = time.monotonic() + self.reboot_downtime
            regs = list(self._registrations)

        def back():
            time.sleep(self.reboot_downtime)
            for host, port in regs:
                self._dial(host, port)
        threading.Thread(target=back, daemon=True).start()

    def channel_count(self):
        with self._lock:
            return len(self._channels)

    def registrations(self):
        with self._lock:
            return list(self._registrations)

    # -- memory ----------------------------------------------------------

    def _find(self, addr):
        for base, buf in self.memory:
            if base <= addr < base + len(buf):
                return base, buf
        return None, None

    def _read(self, addr, length):
        out = bytearray()
        for a in range(addr, addr + length):
            base, buf = self._find(a)
            if buf is None:
                return None
            out.append(buf[a - base])
        return bytes(out)

    def _read_partial(self, addr, length):
        out = []
        for a in range(addr, addr + length):
            base, buf = self._find(a)
            out.append(None if buf is None else buf[a - base])
        return out

    def _write(self, addr, data):
        for i, b in enumerate(data):
            base, buf = self._find(addr + i)
            if buf is None:
                raise ValueError(f"fake: unmapped write at {addr + i:#x}")
            buf[addr + i - base] = b

    # -- server ----------------------------------------------------------

    def _accept_loop(self):
        while self._running:
            try:
                sock, addr = self._listen.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock, addr), daemon=True).start()

    def _dial(self, host, port):
        try:
            s = socket.create_connection((host, port), timeout=2)
        except OSError:
            return False
        s.settimeout(None)
        with self._lock:
            self._channels.append(s)
            self._channel_event.notify_all()
        return True

    def _serve(self, sock, addr):
        if time.monotonic() < self._down_until:
            _close(sock)
            return
        rf = sock.makefile("rb")
        with self._lock:
            self._cmd_socks.add(sock)
        try:
            sock.sendall((self.greeting + "\r\n").encode())
            while self._running:
                raw = rf.readline()
                if not raw:
                    break
                line = raw.decode("latin-1").rstrip("\r\n")
                with self._lock:
                    self.commands.append(line)
                result = self._handle(line, sock, rf, addr)
                if result == "close":
                    break
                if result == "dedicated":
                    with self._lock:
                        self._cmd_socks.discard(sock)
                        self._channels.append(sock)
                        self._channel_event.notify_all()
                    return  # socket now belongs to the channel list
        except OSError:
            pass
        finally:
            with self._lock:
                owned = sock in self._channels
                self._cmd_socks.discard(sock)
            if not owned:
                _close(sock)

    # -- commands --------------------------------------------------------

    def _handle(self, line, sock, rf, addr):
        parts = line.split(None, 1)
        if not parts:
            return None
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        kv = parse_kv(rest)

        def send(text):
            sock.sendall((text + "\r\n").encode("latin-1"))

        def multiline(lines):
            body = "202- multiline response follows\r\n"
            body += "".join(l + "\r\n" for l in lines) + ".\r\n"
            sock.sendall(body.encode("latin-1"))

        def thread():
            tid = to_int(kv.get("thread"))
            return self.threads.get(tid)

        if cmd == "bye":
            send("200- bye")
            return "close"
        if cmd == "dbgname":
            send(f"200- {self.name}")
        elif cmd == "dmversion":
            send(f"200- {self.version}")
        elif cmd == "consoletype":
            send("200- devkit")
        elif cmd == "systeminfo":
            multiline(["HDD=Enabled", "Type=DevKit", "Platform=Xbox"])
        elif cmd == "xbeinfo":
            multiline([f'timestamp=0x3e8b1a2c checksum=0x00000000',
                       f'name="{self.running_xbe}"'])
        elif cmd == "modules":
            lines = []
            for m in self.modules:
                flags = "".join(" " + f for f in m["flags"])
                lines.append(f'name="{m["name"]}" base=0x{m["base"]:08x} '
                             f'size=0x{m["size"]:08x} check=0x00000000 '
                             f'timestamp=0x00000000{flags}')
            multiline(lines)
        elif cmd == "threads":
            multiline([str(t) for t in sorted(self.threads)])
        elif cmd == "threadinfo":
            t = thread()
            if t is None:
                send("405- no such thread")
            else:
                multiline([f"suspend={t.suspend} priority={t.priority} "
                           f"tlsbase=0x{t.base - 0x100:08x} start=0x{t.start:08x} "
                           f"base=0x{t.base:08x} limit=0x{t.limit:08x} "
                           f"createlo=0x00000000 createhi=0x00000000"])
        elif cmd == "getcontext":
            t = thread()
            if t is None:
                send("405- no such thread")
            elif self.require_suspend and not (t.stopped or t.suspend):
                send("408- not stopped")
            else:
                ctx = dict(t.context)
                if t.pcs:
                    ctx[self.pc_reg] = t.pcs[t._pc_i % len(t.pcs)]
                    t._pc_i += 1
                multiline([" ".join(f"{k}=0x{v:08x}" for k, v in ctx.items())])
        elif cmd in ("suspend", "resume"):
            t = thread()
            if t is None:
                send("405- no such thread")
            else:
                t.suspend += 1 if cmd == "suspend" else -1
                t.suspend = max(0, t.suspend)
                send("200- OK")
        elif cmd == "continue":
            t = thread()
            if t is None:
                send("405- no such thread")
            elif not t.stopped:
                send("408- not stopped")
            else:
                t.stopped = False
                send("200- OK")
        elif cmd == "isstopped":
            t = thread()
            if t is None:
                send("405- no such thread")
            elif t.stopped:
                send("200- exception code=0xc0000005")
            else:
                send("408- not stopped")
        elif cmd in ("go", "stop", "halt", "stopon", "nostopon"):
            send("200- OK")
        elif cmd == "debugger":
            send("200- OK")
        elif cmd == "getmem2":
            a, n = to_int(kv.get("addr")), to_int(kv.get("length"))
            data = self._read(a, n)
            if data is None:
                send("404- memory not mapped")
            else:
                sock.sendall(b"203- binary response follows\r\n" + data)
        elif cmd == "getmem":
            a, n = to_int(kv.get("addr")), to_int(kv.get("length"))
            vals = self._read_partial(a, n)
            text = "".join("??" if v is None else f"{v:02x}" for v in vals)
            multiline([text[i:i + 64] for i in range(0, len(text), 64)] or [""])
        elif cmd == "drivelist":
            if self.drivelist_multiline:
                multiline([f'drivename="{d}"' for d in self.drives])
            else:
                send(f"200- {self.drives}")
        elif cmd == "dirlist":
            path = kv.get("name", "")
            if not path.endswith("\\"):
                path += "\\"
            if path not in self.dirs:
                send("402- file not found")
            else:
                lines = []
                for d in self.dirs:
                    if d != path and d.startswith(path) and "\\" not in d[len(path):-1]:
                        lines.append(f'name="{d[len(path):-1]}" sizehi=0x0 sizelo=0x0 '
                                     f'createhi=0x0 createlo=0x0 changehi=0x0 '
                                     f'changelo=0x0 directory')
                for f, data in self.files.items():
                    if f.startswith(path) and "\\" not in f[len(path):]:
                        lines.append(f'name="{f[len(path):]}" sizehi=0x0 '
                                     f'sizelo=0x{len(data):x} createhi=0x0 '
                                     f'createlo=0x0 changehi=0x0 changelo=0x0')
                multiline(lines)
        elif cmd == "getfile":
            name = kv.get("name", "")
            if name not in self.files:
                send("402- file not found")
            else:
                data = self.files[name]
                sock.sendall(b"203- binary response follows\r\n"
                             + struct.pack("<I", len(data)) + data)
        elif cmd == "sendfile":
            name = kv.get("name", "")
            length = to_int(kv.get("length"), 0)
            if name in self.files_in_use:
                send("413- file cannot be created")
            else:
                send("204- send binary data")
                buf = bytearray()
                while len(buf) < length:
                    chunk = rf.read(length - len(buf))
                    if not chunk:
                        return "close"
                    buf += chunk
                self.files[name] = bytes(buf)
                send("200- OK")
        elif cmd == "mkdir":
            name = kv.get("name", "")
            self.dirs[name.rstrip("\\") + "\\"] = None
            send("200- OK")
        elif cmd == "delete":
            self.files.pop(kv.get("name", ""), None)
            send("200- OK")
        elif cmd == "magicboot":
            if kv.get("cold") is True:
                send("200- OK")
                self.reboot()
            else:
                self.launched.append(kv.get("title"))
                self.running_xbe = kv.get("title")
                send("200- OK")
        elif cmd == "reboot":
            send("200- OK")
            self.reboot()
            return "close"
        elif cmd == "screenshot":
            self._screenshot(sock)
        elif cmd == "notifyat":
            port = to_int(kv.get("port"))
            host = addr[0]
            if kv.get("drop") is True:
                with self._lock:
                    self._registrations = [r for r in self._registrations if r[1] != port]
                send("200- OK")
            else:
                with self._lock:
                    if (host, port) not in self._registrations:
                        self._registrations.append((host, port))
                send("200- OK")
                threading.Thread(target=self._dial, args=(host, port), daemon=True).start()
        elif cmd == "notify":
            send("205- now a notification channel")
            return "dedicated"
        else:
            send("407- unknown command")
        return None

    def _screenshot(self, sock):
        if self.screen is None:
            send = "400- no frame"
            sock.sendall((send + "\r\n").encode())
            return
        w, h, rgb = self.screen
        if self.profile == "x360":
            # Tiled, byte order B G R A per texel.
            aligned_w = (w + 31) & ~31
            aligned_h = (h + 31) & ~31
            fb = bytearray(aligned_w * aligned_h * 4)
            for y in range(h):
                for x in range(w):
                    t = xenos_tiled_offset(x, y, w, 4)
                    r, g, b = rgb[(y * w + x) * 3:(y * w + x) * 3 + 3]
                    fb[t * 4:t * 4 + 4] = bytes((b, g, r, 0xFF))
            fmt = 0x18280186
            pitch = aligned_w * 4
        else:
            pitch = w * 4 + 64  # padded rows, like a real pitch
            fb = bytearray(pitch * h)
            for y in range(h):
                for x in range(w):
                    r, g, b = rgb[(y * w + x) * 3:(y * w + x) * 3 + 3]
                    o = y * pitch + x * 4
                    fb[o:o + 4] = bytes((b, g, r, 0))
            fmt = 0x1E
        header = (f"pitch=0x{pitch:08x} width=0x{w:08x} height=0x{h:08x} "
                  f"format=0x{fmt:08x} offsetx=0x00000000 offsety=0x00000000 "
                  f"framebuffersize=0x{len(fb):08x}")
        sock.sendall(b"203- binary response follows\r\n"
                     + header.encode() + b"\r\n" + bytes(fb))


def _close(s):
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass
