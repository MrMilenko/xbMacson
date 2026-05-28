#!/usr/bin/env python3
"""
xbMacson - Cross-platform Xbox Debug Monitor (TUI)
Connects to XBDM (Xbox Debug Monitor) on port 731
and streams debug output from OutputDebugString/DbgPrint.

No flags needed: everything is interactive.
Config stored at ~/.xbmacson.json
"""

import curses
import socket
import struct
import sys
import time
import re
import os
import json
import threading
import queue
import zlib
import concurrent.futures

XBDM_PORT = 731
RECV_BUFSIZE = 4096
RECONNECT_INTERVAL = 0.4
RECONNECT_TIMEOUT = 60   # give up reconnecting after this many seconds
NOTIFY_RECV_TIMEOUT = 30 # treat this much silence on the notify channel as a dead link
ACCEPT_TIMEOUT = 30      # re-register if Xbox does not dial us back within this
CONFIG_PATH = os.path.expanduser("~/.xbmacson.json")
SCREENSHOT_DIR = os.path.expanduser("~/Desktop/xbox_screenshots")
MAX_LOG_LINES = 10000

DEBUGSTR_RE = re.compile(
    r'^debugstr\s+(?:stop\s+)?thread=(\d+)\s+(?:(cr|lf|crlf)\s+)?string=(.*)',
    re.DOTALL,
)

# ── XBDM Notification Patterns ─────────────────────────────────────────
# Exact wire formats taken from FGetNotifySz() in the XBDM kernel source
# (private/ntos/dm/xbdm/dmnotify.c).

BREAK_RE = re.compile(
    r'^break\s+addr=0x([0-9a-fA-F]+)\s+thread=(\d+)'
)
SINGLESTEP_RE = re.compile(
    r'^singlestep\s+addr=0x([0-9a-fA-F]+)\s+thread=(\d+)'
)
# "data [read|write|execute]=0x%08x addr=0x%08x thread=%lu"
DATABREAK_RE = re.compile(
    r'^data\s+(read|write|execute)=0x([0-9a-fA-F]+)'
    r'\s+addr=0x([0-9a-fA-F]+)\s+thread=(\d+)'
)
# "assert [stop] thread=%d string=..."
ASSERT_RE = re.compile(
    r'^assert\s+(?:stop\s+)?thread=(\d+)(?:\s+string=(.*))?', re.DOTALL
)
# "exception code=0x%08x thread=%d address=0x%08x [read|write]=0x%08x [first] [noncont]"
EXCEPTION_RE = re.compile(
    r'^exception\s+code=0x([0-9a-fA-F]+)\s+thread=(\d+)'
    r'\s+address=0x([0-9a-fA-F]+)'
    r'(?:\s+(read|write)=0x([0-9a-fA-F]+))?'
    r'(?:\s+(first))?(?:\s+(noncont))?'
)
EXEC_RE = re.compile(
    r'^execution\s+(\S+)'
)
# "modload name="..." base=0x%08x size=0x%08x check=0x%08x timestamp=0x%08x [tls] [xbe]"
MODLOAD_RE = re.compile(
    r'^modload\s+name="([^"]+)"'
    r'(?:\s+base=0x([0-9a-fA-F]+))?'
    r'(?:\s+size=0x([0-9a-fA-F]+))?'
    r'(?:\s+check=0x([0-9a-fA-F]+))?'
    r'(?:\s+timestamp=0x([0-9a-fA-F]+))?'
)
# "sectload name="..." base=0x%08x size=0x%08x index=%d flags=%d"
SECTLOAD_RE = re.compile(
    r'^sect(load|unload)\s+name="([^"]+)"'
    r'(?:\s+base=0x([0-9a-fA-F]+))?'
    r'(?:\s+size=0x([0-9a-fA-F]+))?'
)
MODUNLOAD_RE = re.compile(
    r'^modunload\s+name="([^"]+)"'
)
CREATE_THREAD_RE = re.compile(
    r'^create\s+thread=(\d+)(?:\s+start=0x([0-9a-fA-F]+))?'
)
DESTROY_THREAD_RE = re.compile(
    r'^(?:destroy|terminate)\s+thread=(\d+)'
)
# "rip [stop] thread=%lu [string=...]"
RIP_RE = re.compile(
    r'^rip\s+(?:stop\s+)?thread=(\d+)(?:\s+string=(.*))?', re.DOTALL
)
STACKTRACE_RE = re.compile(
    r'^stacktrace\s+thread=(\d+)'
)

# Well-known exception codes (Win32/Xbox)
EXCEPTION_NAMES = {
    0x80000003: "Breakpoint",
    0x80000004: "Single Step",
    0xC0000005: "Access Violation",
    0xC0000006: "In Page Error",
    0xC000001D: "Illegal Instruction",
    0xC0000025: "Non-continuable Exception",
    0xC00000FD: "Stack Overflow",
    0xC0000094: "Integer Divide by Zero",
    0xC0000095: "Integer Overflow",
    0xC000008C: "Array Bounds Exceeded",
    0xC0000096: "Privileged Instruction",
    0xC000013A: "Control-C",
    0xC0000194: "Possible Deadlock",
}


def _exc_name(code):
    """Return human-readable exception name or hex code."""
    return EXCEPTION_NAMES.get(code, f"0x{code:08X}")


# Color pair IDs
C_NORMAL = 0
C_RED = 1
C_GREEN = 2
C_YELLOW = 3
C_CYAN = 4
C_MAGENTA = 5
C_DIM = 6
C_BOLD_CYAN = 7
C_STATUS = 8
C_HEADER = 9
C_INPUT = 10
C_SELECTED = 11
C_BLUE = 14

# Thread color pairs: 20-27
THREAD_COLORS_BASE = 20
NUM_THREAD_COLORS = 7


# ── Config ───────────────────────────────────────────────────────────────

def load_config():
    defaults = {
        "default_ip": "",
        "auto_reconnect": True,
        "default_logging": False,
        "log_dir": "",
        "screenshot_dir": SCREENSHOT_DIR,
        "last_connected": [],
    }
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
            defaults.update(cfg)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# ── XBDM Protocol ───────────────────────────────────────────────────────

def parse_debug_line(line):
    m = DEBUGSTR_RE.match(line)
    if m:
        # cr/lf group is None when the field is absent: treat as "lf"
        return (m.group(1), m.group(2) or "lf", m.group(3))
    return None


def classify_line(text):
    lower = text.lower()
    if any(w in lower for w in [
        "error", "fail", "assert", "crash", "fatal", "exception",
        "abort", "panic", "corrupt", "violation", "denied",
    ]):
        return C_RED
    if any(w in lower for w in [
        "warn", "invalid", "not found", "deprecated", "timeout",
        "retry", "slow", "missing", "skip", "fallback",
    ]):
        return C_YELLOW
    if any(w in lower for w in [
        "init", "start", "loaded", "success", "connected", "ready",
        "complete", "done", "ok", "mounted", "registered",
    ]):
        return C_GREEN
    if any(w in lower for w in ["debug", "trace", "verbose"]):
        return C_BLUE
    if text.startswith("[") and "]" in text:
        return C_CYAN
    return C_NORMAL


def get_local_subnet():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split(".")
        return local_ip, f"{parts[0]}.{parts[1]}.{parts[2]}"
    except Exception:
        return None, None


def check_xbdm_host(ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        if s.connect_ex((ip, XBDM_PORT)) == 0:
            try:
                greeting = s.recv(256).decode("utf-8", errors="replace").strip()
                if greeting.startswith("201"):
                    s.sendall(b"DBGNAME\r\n")
                    resp = s.recv(256).decode("utf-8", errors="replace").strip()
                    name = resp.replace("200- ", "") if resp.startswith("200") else "unknown"
                    s.close()
                    return (ip, name)
            except Exception:
                s.close()
                return (ip, "unknown")
        s.close()
    except Exception:
        pass
    return None


def xbdm_query(ip, command):
    """One-shot XBDM command. Returns raw response string."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((ip, XBDM_PORT))
        # Read greeting
        sock.recv(RECV_BUFSIZE)
        # Send command
        sock.sendall(f"{command}\r\n".encode("utf-8"))
        # Read response, adapting to response type
        resp = b""
        while True:
            try:
                chunk = sock.recv(RECV_BUFSIZE)
                if not chunk:
                    break
                resp += chunk
                text = resp.decode("utf-8", errors="replace")
                first_line = text.split("\r\n", 1)[0] if "\r\n" in text else ""
                code = first_line[:3] if len(first_line) >= 3 else ""
                if code.isdigit():
                    c = int(code)
                    if c == 202:
                        # Multiline: ends with \r\n.\r\n
                        if "\r\n.\r\n" in text:
                            break
                    else:
                        # Single-line (200, 4xx, etc.)
                        if "\r\n" in text:
                            break
                if len(resp) > 65536:
                    break
            except socket.timeout:
                break
        sock.close()
        return resp.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def parse_drivelist(resp):
    """Extract drive letters from DRIVELIST response.

    XBDM returns either a simple letter string (200- CDEFXYZ)
    or multiline drivename=\"X\" lines (202-).
    """
    drives = []
    # Try multiline drivename="X" format first
    for m in re.finditer(r'drivename="([^"]+)"', resp):
        drives.append(m.group(1))
    if drives:
        drives.sort()
        return drives
    # Fall back: single-line letter string (200- CDEFXYZ)
    text = resp
    if text.startswith("200- "):
        text = text[5:]
    elif text.startswith("200-"):
        text = text[4:]
    text = text.strip()
    if text:
        drives = sorted(set(ch.upper() for ch in text if ch.isalpha()))
    return drives


def parse_dirlist(resp):
    """Parse DIRLIST response into list of dicts with name, size, is_dir."""
    entries = []
    for line in resp.split("\n"):
        line = line.strip()
        if not line.startswith("name="):
            continue
        m = re.search(r'name="([^"]+)"', line)
        if not m:
            continue
        name = m.group(1)
        is_dir = "directory" in line
        size = 0
        if not is_dir:
            slo = re.search(r'sizelo=0x([0-9a-fA-F]+)', line)
            shi = re.search(r'sizehi=0x([0-9a-fA-F]+)', line)
            if slo:
                size = int(slo.group(1), 16)
            if shi:
                size |= int(shi.group(1), 16) << 32
        entries.append({"name": name, "size": size, "is_dir": is_dir})
    # Sort: directories first (alpha), then files (alpha)
    entries.sort(key=lambda e: (0 if e["is_dir"] else 1, e["name"].lower()))
    return entries


def _fmt_size(nbytes):
    """Return human-readable file size string."""
    if nbytes < 1024:
        return f"{nbytes} B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    elif nbytes < 1024 * 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MB"
    else:
        return f"{nbytes / (1024 * 1024 * 1024):.1f} GB"


# ── PNG Writer ───────────────────────────────────────────────────────────

def _write_png(path, width, height, pitch, framebuffer):
    """Write XRGB framebuffer data as a PNG file (pure stdlib)."""
    def _crc32(data):
        return zlib.crc32(data) & 0xFFFFFFFF

    def _chunk(tag, data):
        chunk_data = tag + data
        return (
            struct.pack(">I", len(data))
            + chunk_data
            + struct.pack(">I", _crc32(chunk_data))
        )

    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter: none
        offset = row * pitch
        for px in range(width):
            o = offset + px * 4
            raw.append(framebuffer[o + 2])  # R
            raw.append(framebuffer[o + 1])  # G
            raw.append(framebuffer[o])      # B

    compressed = zlib.compress(bytes(raw), 9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", ihdr))
        f.write(_chunk(b"IDAT", compressed))
        f.write(_chunk(b"IEND", b""))


# ── XBDM Screenshot ─────────────────────────────────────────────────────

def take_screenshot(ip, save_dir):
    """Capture framebuffer via a one-shot XBDM connection. Returns path or error."""
    os.makedirs(save_dir, exist_ok=True)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((ip, XBDM_PORT))
        greeting = sock.recv(RECV_BUFSIZE).decode("utf-8", errors="replace").strip()
        if not greeting.startswith("201"):
            sock.close()
            return f"Bad greeting: {greeting}"

        sock.sendall(b"SCREENSHOT\r\n")

        # Read header line: "203- binary response follows\r\n"
        header = b""
        while b"\r\n" not in header:
            header += sock.recv(1)

        # Read info line with dimensions
        info = b""
        while b"\r\n" not in info:
            info += sock.recv(1)
        info_str = info.decode("utf-8", errors="replace").strip()

        vals = {}
        for m in re.finditer(r"(\w+)=0x([0-9a-fA-F]+)", info_str):
            vals[m.group(1)] = int(m.group(2), 16)

        pitch = vals["pitch"]
        width = vals["width"]
        height = vals["height"]
        fb_size = vals["framebuffersize"]

        # Read raw framebuffer
        data = b""
        while len(data) < fb_size:
            chunk = sock.recv(min(65536, fb_size - len(data)))
            if not chunk:
                break
            data += chunk
        sock.close()

        if len(data) < fb_size:
            return f"Incomplete: got {len(data)}/{fb_size} bytes"

        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(save_dir, f"xbox_{ts}.png")
        _write_png(path, width, height, pitch, data)
        return path

    except Exception as e:
        return f"Error: {e}"


# ── Data Types ───────────────────────────────────────────────────────────

class LogLine:
    __slots__ = ("ts", "thread_id", "text", "color", "is_system")

    def __init__(self, ts, thread_id, text, color, is_system=False):
        self.ts = ts
        self.thread_id = thread_id
        self.text = text
        self.color = color
        self.is_system = is_system


# ── XBDM Connection (background thread) ─────────────────────────────────

class XBDMConnection:
    def __init__(self, msg_queue):
        self.msg_queue = msg_queue
        self.sock = None
        self.thread = None
        self.running = False
        self.connected = False
        self.xbox_name = ""
        self.xbox_ip = ""
        self.dm_version = ""
        self.listen_sock = None  # bound on _run start; xbox dials us here
        self.notify_port = 0     # the port the xbox knows to call back on
        self._thread_bufs = {}  # per-thread cr/lf line assembly
        # Set when XBDM halts the box; TUI watches it for the Continue/Reboot prompt.
        self.stopped_thread = None
        self.stopped_reason = ""

    def connect(self, ip):
        self.xbox_ip = ip
        self.running = True
        self.connected = False
        self.xbox_name = ""
        self.dm_version = ""
        self._thread_bufs = {}
        self.stopped_thread = None
        self.stopped_reason = ""
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def disconnect(self):
        self.running = False
        self.connected = False
        self.stopped_thread = None
        # Remove our persistent notifyat entry so the Xbox stops dialing
        # this port forever, then drop the debug session.
        if self.xbox_ip and self.notify_port:
            try:
                xbdm_query(self.xbox_ip, f"notifyat port={self.notify_port} drop")
            except Exception:
                pass
        if self.xbox_ip:
            try:
                xbdm_query(self.xbox_ip, "DEBUGGER disconnect")
            except Exception:
                pass
        for attr in ("sock", "listen_sock"):
            s = getattr(self, attr)
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self.notify_port = 0

    def continue_execution(self):
        """Resume after a stop: send CONTINUE + GO on a one-shot connection,
        matching xbWatson's DmContinueThread() + DmGo() sequence."""
        tid = self.stopped_thread
        if tid is None:
            return
        self.stopped_thread = None
        self.stopped_reason = ""
        try:
            xbdm_query(self.xbox_ip, f"CONTINUE thread={tid}")
            xbdm_query(self.xbox_ip, "GO")
            self._emit("Execution resumed.", C_GREEN, is_system=True)
        except Exception as e:
            self._emit(f"Failed to resume: {e}", C_RED, is_system=True)

    def force_go(self):
        """Hardwired GO: send CONTINUE (if we have a thread) + GO
        regardless of local state, in case the Xbox is stuck waiting."""
        tid = self.stopped_thread
        self.stopped_thread = None
        self.stopped_reason = ""
        try:
            if tid:
                xbdm_query(self.xbox_ip, f"CONTINUE thread={tid}")
            xbdm_query(self.xbox_ip, "GO")
            self._emit("GO sent.", C_GREEN, is_system=True)
        except Exception as e:
            self._emit(f"GO failed: {e}", C_RED, is_system=True)

    @staticmethod
    def _enable_keepalive(sock):
        # A rebooting Xbox usually drops the link without a clean close, so
        # turn on TCP keepalive and tune it to notice a dead peer in ~10s
        # rather than the OS default. Options vary by platform; set what exists.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            return
        idle, intvl, cnt = 4, 2, 3
        for opt, val in (("TCP_KEEPIDLE", idle), ("TCP_KEEPALIVE", idle),
                         ("TCP_KEEPINTVL", intvl), ("TCP_KEEPCNT", cnt)):
            if hasattr(socket, opt):
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
                except OSError:
                    pass
        if hasattr(sock, "ioctl") and hasattr(socket, "SIO_KEEPALIVE_VALS"):
            try:
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, intvl * 1000))
            except OSError:
                pass

    def _emit(self, text, color=C_NORMAL, is_system=False, thread_id=None):
        self.msg_queue.put(
            LogLine(time.strftime("%H:%M:%S"), thread_id, text, color, is_system)
        )

    def _handle_stop(self, tid, reason):
        # XBDM has halted the box. Record state so the TUI status bar
        # (and, later, a toast) can prompt for Continue / Go / Reboot.
        self.stopped_thread = tid
        self.stopped_reason = reason
        self._emit(
            f"STOPPED: {reason} (thread {tid})",
            C_RED, is_system=True, thread_id=tid,
        )

    # ── Notification dispatcher ─────────────────────────────────────────

    def _dispatch_notification(self, line):
        """Route an incoming XBDM notification line to the right handler.
        Wire formats match FGetNotifySz() in private/ntos/dm/xbdm/dmnotify.c."""

        # Keepalive
        if line == "null":
            return

        # XBDM adds a "stop" token after the keyword when the box has halted
        # ("break stop addr=..."). Per-type regexes handle it for debugstr/
        # assert/rip; we check it here for break/exception.
        has_stop = False
        # Check for stop right after the first keyword
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[1] == "stop":
            has_stop = True

        # 1. Debug strings: with proper cr/lf/crlf line assembly
        parsed = parse_debug_line(line)
        if parsed:
            tid, term, msg = parsed
            if term == "cr":
                self._thread_bufs.setdefault(tid, "")
                self._thread_bufs[tid] += msg
            else:
                # lf or crlf: flush assembled line
                full = self._thread_bufs.pop(tid, "") + msg
                if full.strip():
                    self._emit(full, classify_line(full), thread_id=tid)
            return

        # 2. Assert: "assert [stop] thread=%d string=..."
        m = ASSERT_RE.match(line)
        if m:
            tid = m.group(1)
            msg = m.group(2) or ""
            self._emit(
                f"Xbox Assertion Failed - thread {tid} [{self.xbox_name}]",
                C_RED, is_system=True, thread_id=tid,
            )
            if msg.strip():
                self._emit(msg.strip(), C_RED, is_system=True, thread_id=tid)
            if has_stop:
                self._handle_stop(tid, "Assertion Failed")
            return

        # 3. Exception: "exception code=0x%08x thread=%d address=0x%08x
        #                  [read|write]=0x%08x [first] [noncont]"
        m = EXCEPTION_RE.match(line)
        if m:
            code = int(m.group(1), 16)
            tid = m.group(2)
            addr = m.group(3)
            rw_type = m.group(4)    # "read" or "write" or None
            rw_addr = m.group(5)    # address or None
            first = m.group(6)      # "first" or None
            noncont = m.group(7)    # "noncont" or None
            name = _exc_name(code)
            chance = "first" if first else "second"
            if code == 0xC0000005 and rw_type and rw_addr:
                if rw_type == "write":
                    self._emit(
                        f"The instruction at address 0x{addr.upper()} referenced "
                        f"memory at address 0x{rw_addr.upper()}.  "
                        f"The memory could not be written.",
                        C_RED, is_system=True, thread_id=tid,
                    )
                else:
                    self._emit(
                        f"The instruction at address 0x{addr.upper()} referenced "
                        f"memory at address 0x{rw_addr.upper()}.  "
                        f"The memory could not be read.",
                        C_RED, is_system=True, thread_id=tid,
                    )
            elif code == 0x80000003:
                self._emit(
                    f"A breakpoint exception (0x{addr.upper()}) has been reached "
                    f"in the application at location 0x{addr.upper()}.",
                    C_RED, is_system=True, thread_id=tid,
                )
            else:
                self._emit(
                    f"An exception ({name}) occurred in the application "
                    f"at location 0x{addr.upper()} ({chance} chance).",
                    C_RED, is_system=True, thread_id=tid,
                )
            if noncont:
                self._emit(
                    "Exception is non-continuable.",
                    C_RED, is_system=True, thread_id=tid,
                )
            if has_stop and not first:
                # xbWatson only handles second-chance exceptions
                self._handle_stop(tid, f"Exception ({name})")
            return

        # 4. Breakpoint: "break addr=0x%08x thread=%d"
        m = BREAK_RE.match(line)
        if m:
            addr, tid = m.group(1), m.group(2)
            self._emit(
                f"Break: 0x{addr.upper()} thread={tid}",
                C_RED, is_system=True, thread_id=tid,
            )
            if has_stop:
                self._handle_stop(tid, "Breakpoint")
            return

        # 5. Single step: xbWatson never shows these; drop the noise.
        if SINGLESTEP_RE.match(line):
            return

        # 6. Data breakpoint: "data [read|write|execute]=0x%08x addr=0x%08x thread=%lu"
        m = DATABREAK_RE.match(line)
        if m:
            access = m.group(1)
            data_addr = m.group(2)
            addr = m.group(3)
            tid = m.group(4)
            self._emit(
                f"Databreak: {access}=0x{data_addr.upper()} "
                f"addr=0x{addr.upper()} thread={tid}",
                C_RED, is_system=True, thread_id=tid,
            )
            if has_stop:
                self._handle_stop(tid, "Data Breakpoint")
            return

        # 7. Execution state: "execution [stopped|started|rebooting|pending]"
        m = EXEC_RE.match(line)
        if m:
            state = m.group(1).lower()
            msgs = {
                "rebooting": "xbMacson: Xbox is restarting.",
                "pending": "Execution pending.",
                "started": "Execution started.",
                "stopped": "Execution stopped.",
            }
            self._emit(
                msgs.get(state, f"Execution: {state}"),
                C_YELLOW, is_system=True,
            )
            return

        # 8. RIP: "rip [stop] thread=%lu [string=...]"
        m = RIP_RE.match(line)
        if m:
            tid = m.group(1)
            msg = m.group(2) or ""
            self._emit(
                f"A RIP error has occurred on the Xbox (thread {tid}).",
                C_RED, is_system=True, thread_id=tid,
            )
            if msg.strip():
                self._emit(
                    f"The error description was: {msg.strip()}",
                    C_RED, is_system=True, thread_id=tid,
                )
            if has_stop:
                self._handle_stop(tid, "RIP Error")
            return

        # 9. Stack trace request: xbWatson never shows these.
        if STACKTRACE_RE.match(line):
            return

        # 10. Module load: "modload name="..." base=0x... size=0x... [xbe]"
        m = MODLOAD_RE.match(line)
        if m:
            name = m.group(1)
            base = m.group(2) or "?"
            size = m.group(3) or "?"
            xbe = " [XBE]" if "xbe" in line else ""
            self._emit(
                f"Loaded: {name} base=0x{base.upper()} "
                f"size=0x{size.upper()}{xbe}",
                C_GREEN, is_system=True,
            )
            return

        # 11. Section load/unload: "sect[load|unload] name="..." base=0x... size=0x..."
        m = SECTLOAD_RE.match(line)
        if m:
            action = m.group(1)  # "load" or "unload"
            name = m.group(2)
            base = m.group(3) or "?"
            size = m.group(4) or "?"
            if action == "load":
                self._emit(
                    f"Section loaded: {name} base=0x{base.upper()} "
                    f"size=0x{size.upper()}",
                    C_GREEN, is_system=True,
                )
            else:
                self._emit(
                    f"Section unloaded: {name}",
                    C_YELLOW, is_system=True,
                )
            return

        # 12. Module unload
        m = MODUNLOAD_RE.match(line)
        if m:
            self._emit(f"Unloaded: {m.group(1)}", C_YELLOW, is_system=True)
            return

        # 13/14. Thread create and destroy: xbWatson never shows these and
        # they bury the real debug output. Consume and drop them.
        if CREATE_THREAD_RE.match(line) or DESTROY_THREAD_RE.match(line):
            return

        # Fallback: show unrecognised lines as-is
        self._emit(line, classify_line(line), is_system=True)

    def _run(self):
        first_connect = True
        shown_error = False
        deadline = None     # armed after a failure; cleared on progress
        registered = False  # have we sent NOTIFYAT this cycle?

        # Bind the listen socket once for the whole session. XBDM dials this
        # port for every notification session, including the ones that follow
        # a reset, because the Xbox persists our registration to its .ini.
        try:
            self.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listen_sock.bind(("0.0.0.0", 0))
            self.listen_sock.listen(2)
            self.notify_port = self.listen_sock.getsockname()[1]
        except OSError as e:
            self._emit(f"Could not bind a local port: {e}", C_RED, is_system=True)
            return

        while self.running:
            # Register (or re-register) with XBDM. DBGNAME/DMVERSION are
            # only for the connect banner; DEBUGGER connect enables debug
            # events; NOTIFYAT tells XBDM to dial us back at our port.
            if not registered:
                try:
                    if first_connect:
                        self._emit(
                            f"Connecting to {self.xbox_ip}:{XBDM_PORT} ...",
                            C_CYAN, is_system=True,
                        )
                        try:
                            resp = xbdm_query(self.xbox_ip, "DBGNAME")
                            if resp.startswith("200"):
                                self.xbox_name = resp.replace("200- ", "").strip()
                        except Exception:
                            pass
                        try:
                            resp = xbdm_query(self.xbox_ip, "DMVERSION")
                            if resp.startswith("200"):
                                self.dm_version = resp.replace("200- ", "").strip()
                        except Exception:
                            pass
                    try:
                        xbdm_query(self.xbox_ip, "DEBUGGER connect")
                    except Exception:
                        pass
                    xbdm_query(self.xbox_ip, f"notifyat port={self.notify_port}")
                    registered = True
                    deadline = None
                except (socket.timeout, ConnectionRefusedError,
                        ConnectionResetError, OSError):
                    if self.running and not shown_error:
                        self._emit("Waiting for Xbox...", C_YELLOW, is_system=True)
                        shown_error = True
                    if not self.running:
                        break
                    if deadline is None:
                        deadline = time.monotonic() + RECONNECT_TIMEOUT
                    if time.monotonic() > deadline:
                        self._emit(
                            f"Could not reach Xbox within {int(RECONNECT_TIMEOUT)}s; giving up.",
                            C_RED, is_system=True,
                        )
                        self.running = False
                        break
                    time.sleep(RECONNECT_INTERVAL)
                    continue

            # Wait for XBDM to dial us back. If accept times out, the Xbox
            # may have dropped our registration after a failed call, so
            # re-register on the next iteration.
            self.listen_sock.settimeout(ACCEPT_TIMEOUT)
            try:
                self.sock, _ = self.listen_sock.accept()
            except socket.timeout:
                # Xbox did not dial back in time. Could be a transient drop,
                # or a firewall blocking incoming connections. Arm the same
                # 60s deadline as a registration failure before giving up.
                if self.running and not shown_error:
                    self._emit("Waiting for Xbox...", C_YELLOW, is_system=True)
                    shown_error = True
                if deadline is None:
                    deadline = time.monotonic() + RECONNECT_TIMEOUT
                if time.monotonic() > deadline:
                    self._emit(
                        f"Xbox did not dial back within {int(RECONNECT_TIMEOUT)}s; giving up.",
                        C_RED, is_system=True,
                    )
                    self.running = False
                    break
                registered = False
                continue
            except OSError as e:
                if self.running and not shown_error:
                    self._emit(f"Accept failed: {e}", C_RED, is_system=True)
                    shown_error = True
                registered = False
                time.sleep(RECONNECT_INTERVAL)
                continue

            self._enable_keepalive(self.sock)
            self.sock.settimeout(NOTIFY_RECV_TIMEOUT)
            self.connected = True
            deadline = None
            self._thread_bufs = {}
            self.stopped_thread = None

            if first_connect:
                name_part = f" ({self.xbox_name})" if self.xbox_name else ""
                self._emit(
                    f"Connected to {self.xbox_ip}{name_part}.",
                    C_GREEN, is_system=True,
                )
                first_connect = False
            shown_error = False

            buffer = ""
            try:
                while self.running:
                    data = self.sock.recv(RECV_BUFSIZE)
                    if not data:
                        break  # session ended; XBDM will dial back when it can
                    buffer += data.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")
                        if not line:
                            continue
                        self._dispatch_notification(line)
            except (socket.timeout, ConnectionRefusedError,
                    ConnectionResetError, OSError):
                if self.running and not shown_error:
                    self._emit("Connection lost, reconnecting...", C_YELLOW, is_system=True)
                    shown_error = True
            finally:
                self.connected = False
                if self.sock is not None:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None

            if not self.running:
                break

            # XBDM persists our registration, so it will dial us back as
            # soon as it is up again. Just loop to accept the next session.
            time.sleep(RECONNECT_INTERVAL)


# ── TUI Application ─────────────────────────────────────────────────────

class XbMacsonTUI:
    # Modes
    MENU = "menu"
    SCAN = "scan"
    MONITOR = "monitor"
    TEXT_INPUT = "text_input"
    FILTER_INPUT = "filter_input"
    SETTINGS = "settings"
    PROBE = "probe"
    CONFIRM = "confirm"
    DRIVES = "drives"
    BROWSER = "browser"

    LOGO = [
        r"          _     __  __                             ",
        r"   __  __| |__ |  \/  | __ _  ___ ___  ___  _ __  ",
        r"   \ \/ /| '_ \| |\/| |/ _` |/ __/ __|/ _ \| '_ \ ",
        r"    >  < | |_) | |  | | (_| | (__\__ \ (_) | | | |",
        r"   /_/\_\|_.__/|_|  |_|\__,_|\___|___/\___/|_| |_|",
    ]

    def __init__(self, stdscr):
        self.scr = stdscr
        self.config = load_config()
        self.mode = self.MENU
        self.msg_queue = queue.Queue()
        self.conn = XBDMConnection(self.msg_queue)

        # Log
        self.log_lines = []
        self.scroll_offset = 0
        self.auto_scroll = True
        self.line_count = 0

        # Toggles
        self.filter_str = ""
        self.raw_mode = False
        self.paused = False
        self.log_file = None
        self.log_filename = ""

        # Scan
        self.scan_results = []
        self.scan_running = False
        self.scan_cursor = 0

        # Probe
        self.probe_lines = []

        # Screenshot
        self.screenshot_status = ""

        # Confirm dialog
        self.confirm_msg = ""
        self.confirm_cb = None
        self.confirm_return = self.MONITOR

        # Generic input
        self.input_buf = ""
        self.input_prompt = ""
        self.input_cb = None
        self.input_return_mode = self.MENU

        # Drives
        self.drives = []
        self.drives_loading = False
        self.drives_cursor = 0
        self.drives_return = self.MONITOR

        # Browser
        self.browser_path = ""
        self.browser_entries = []
        self.browser_cursor = 0
        self.browser_scroll = 0
        self.browser_loading = False

        # Menu / settings cursor
        self.cursor = 0
        self._menu_opts = []

        self._init_curses()

    def _init_curses(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(C_RED, curses.COLOR_RED, -1)
        curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
        curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
        curses.init_pair(C_MAGENTA, curses.COLOR_MAGENTA, -1)
        curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
        curses.init_pair(C_BOLD_CYAN, curses.COLOR_CYAN, -1)
        curses.init_pair(C_STATUS, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_HEADER, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(C_INPUT, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(C_SELECTED, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(C_BLUE, curses.COLOR_BLUE, -1)
        # Thread color palette
        thread_fg = [
            curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW,
            curses.COLOR_MAGENTA, curses.COLOR_BLUE, curses.COLOR_RED,
            curses.COLOR_WHITE,
        ]
        for i, fg in enumerate(thread_fg):
            curses.init_pair(THREAD_COLORS_BASE + i, fg, -1)
        self._thread_color_map = {}
        curses.curs_set(0)
        self.scr.nodelay(True)
        self.scr.keypad(True)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _size(self):
        return self.scr.getmaxyx()

    def _header(self, title):
        h, w = self._size()
        self.scr.attron(curses.color_pair(C_HEADER) | curses.A_BOLD)
        self.scr.addnstr(0, 0, " " * w, w)
        x = max(0, (w - len(title)) // 2)
        self.scr.addnstr(0, x, title, w - x)
        self.scr.attroff(curses.color_pair(C_HEADER) | curses.A_BOLD)

    def _status(self, text):
        h, w = self._size()
        self.scr.attron(curses.color_pair(C_STATUS))
        self.scr.addnstr(h - 1, 0, text.ljust(w), w - 1)
        self.scr.attroff(curses.color_pair(C_STATUS))

    def _center(self, y, text, attr=0):
        _, w = self._size()
        x = max(0, (w - len(text)) // 2)
        self.scr.addnstr(y, x, text, w - x, attr)

    def _addline(self, y, x, text, attr=0, maxw=None):
        _, w = self._size()
        if maxw is None:
            maxw = w - x - 1
        if maxw > 0 and y < self._size()[0] - 1:
            self.scr.addnstr(y, x, text, maxw, attr)

    def _visible_lines(self):
        if not self.filter_str:
            return self.log_lines
        filt = self.filter_str.lower()
        return [ln for ln in self.log_lines if filt in ln.text.lower() or ln.is_system]

    def _drain_queue(self):
        for _ in range(500):  # batch process
            try:
                item = self.msg_queue.get_nowait()
            except queue.Empty:
                break
            if not isinstance(item, LogLine):
                continue
            self.line_count += 1
            self.log_lines.append(item)
            if self.log_file and not self.paused:
                pfx = f"[{item.ts}]"
                if item.thread_id:
                    pfx += f" [T{item.thread_id}]"
                self.log_file.write(f"{pfx} {item.text}\n")
                self.log_file.flush()
            if len(self.log_lines) > MAX_LOG_LINES:
                self.log_lines = self.log_lines[-MAX_LOG_LINES:]

    def _remember_ip(self, ip, name=""):
        last = self.config.get("last_connected", [])
        last = [e for e in last if (e[0] if isinstance(e, list) else e) != ip]
        last.insert(0, [ip, name])
        self.config["last_connected"] = last[:5]
        save_config(self.config)

    def _start_monitor(self, ip):
        self.conn.disconnect()
        self.log_lines.clear()
        self.line_count = 0
        self.scroll_offset = 0
        self.auto_scroll = True
        self._thread_color_map = {}
        self.mode = self.MONITOR
        self.conn.connect(ip)
        self._remember_ip(ip)
        if self.config.get("default_logging", False) and not self.log_file:
            self._toggle_log()

    # ── Main Loop ────────────────────────────────────────────────────────

    def run(self):
        handlers = {
            self.MENU: (self._draw_menu, self._key_menu),
            self.SCAN: (self._draw_scan, self._key_scan),
            self.MONITOR: (self._draw_monitor, self._key_monitor),
            self.TEXT_INPUT: (self._draw_text_input, self._key_text_input),
            self.FILTER_INPUT: (self._draw_filter, self._key_filter),
            self.SETTINGS: (self._draw_settings, self._key_settings),
            self.PROBE: (self._draw_probe, self._key_probe),
            self.CONFIRM: (self._draw_confirm, self._key_confirm),
            self.DRIVES: (self._draw_drives, self._key_drives),
            self.BROWSER: (self._draw_browser, self._key_browser),
        }
        while True:
            self._drain_queue()
            draw, handle = handlers[self.mode]
            draw()
            if not handle():
                break
            time.sleep(0.02)

        self.conn.disconnect()
        if self.log_file:
            self.log_file.close()

    # ── Menu ─────────────────────────────────────────────────────────────

    def _build_menu_opts(self):
        opts = []

        # Resume active session
        if self.conn.running or self.conn.connected:
            name = self.conn.xbox_name or self.conn.xbox_ip
            label = f"Resume session - {name}"
            if self.line_count:
                label += f" ({self.line_count} lines)"
            opts.append(("resume", label))

        dip = self.config.get("default_ip", "")
        if dip:
            opts.append(("connect", f"Connect to {dip}"))

        last = self.config.get("last_connected", [])
        for entry in last[:3]:
            ip = entry[0] if isinstance(entry, list) else entry
            name = entry[1] if isinstance(entry, list) and len(entry) > 1 else ""
            if ip == dip:
                continue
            label = ip
            if name:
                label += f" ({name})"
            opts.append(("recent:" + ip, f"Recent: {label}"))

        if self.conn.xbox_ip:
            opts.append(("browse", "Browse filesystem"))

        opts.append(("scan", "Scan network for Xbox consoles"))
        opts.append(("enter", "Enter IP address"))
        opts.append(("settings", "Settings"))
        if self.conn.running or self.conn.connected:
            opts.append(("disconnect", "Disconnect"))
        opts.append(("quit", "Quit"))
        return opts

    def _draw_menu(self):
        self.scr.erase()
        h, w = self._size()
        self._header("xbMacson")

        y = 2
        for line in self.LOGO:
            if y < h - 2:
                self._center(y, line, curses.color_pair(C_GREEN) | curses.A_BOLD)
            y += 1

        y += 1
        if y < h - 2:
            self._center(
                y, "Cross-platform Xbox Debug Monitor",
                curses.color_pair(C_DIM) | curses.A_DIM,
            )
        y += 2

        self._menu_opts = self._build_menu_opts()
        if self.cursor >= len(self._menu_opts):
            self.cursor = 0

        col = max(2, (w - 52) // 2)
        for i, (_, desc) in enumerate(self._menu_opts):
            if y >= h - 2:
                break
            if i == self.cursor:
                attr = curses.color_pair(C_SELECTED) | curses.A_BOLD
                self._addline(y, col, f" > {desc}".ljust(50), attr, 50)
            else:
                self._addline(y, col, f"   {desc}", curses.color_pair(C_CYAN))
            y += 1

        self._status(" \u2191\u2193 Navigate  Enter Select  Q Quit")
        self.scr.refresh()

    def _key_menu(self):
        key = self.scr.getch()
        if key == -1:
            return True
        n = len(self._menu_opts)
        if key in (curses.KEY_UP, ord("k")):
            self.cursor = (self.cursor - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = (self.cursor + 1) % n
        elif key in (curses.KEY_ENTER, 10, 13):
            tag = self._menu_opts[self.cursor][0]
            if tag == "resume":
                self.mode = self.MONITOR
            elif tag == "connect":
                self._start_monitor(self.config["default_ip"])
            elif tag.startswith("recent:"):
                self._start_monitor(tag.split(":", 1)[1])
            elif tag == "browse":
                self._begin_drives(self.MENU)
            elif tag == "scan":
                self._begin_scan()
            elif tag == "enter":
                self._begin_input("Xbox IP Address: ", self._on_ip_entered, self.MENU)
            elif tag == "settings":
                self.mode = self.SETTINGS
                self.cursor = 0
            elif tag == "disconnect":
                self.conn.disconnect()
            elif tag == "quit":
                return False
        elif key in (ord("q"), ord("Q")):
            return False
        elif key in (ord("s"), ord("S")):
            self._begin_scan()
        return True

    # ── Text Input ───────────────────────────────────────────────────────

    def _begin_input(self, prompt, callback, return_mode):
        self.input_buf = ""
        self.input_prompt = prompt
        self.input_cb = callback
        self.input_return_mode = return_mode
        self.mode = self.TEXT_INPUT
        curses.curs_set(1)

    def _draw_text_input(self):
        self.scr.erase()
        h, w = self._size()
        self._header("xbMacson")

        y = 2
        for line in self.LOGO:
            if y < h - 2:
                self._center(y, line, curses.color_pair(C_GREEN) | curses.A_BOLD)
            y += 1
        y += 2

        col = max(2, (w - 52) // 2)
        self._addline(y, col + 3, self.input_prompt, curses.color_pair(C_CYAN))
        ix = col + 3 + len(self.input_prompt)
        field_w = min(30, w - ix - 2)
        self._addline(y, ix, self.input_buf.ljust(field_w), curses.color_pair(C_INPUT), field_w)

        cx = ix + len(self.input_buf)
        if cx < w:
            self.scr.move(y, cx)

        self._status(" Enter Confirm  Esc Cancel")
        self.scr.refresh()

    def _key_text_input(self):
        key = self.scr.getch()
        if key == -1:
            return True
        if key == 27:
            curses.curs_set(0)
            self.mode = self.input_return_mode
        elif key in (curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            if self.input_cb:
                self.input_cb(self.input_buf)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.input_buf = self.input_buf[:-1]
        elif 32 <= key <= 126:
            self.input_buf += chr(key)
        return True

    def _on_ip_entered(self, ip):
        ip = ip.strip()
        if ip:
            self._start_monitor(ip)
        else:
            self.mode = self.MENU

    # ── Network Scan ─────────────────────────────────────────────────────

    def _begin_scan(self):
        self.scan_results = []
        self.scan_running = True
        self.scan_error = ""
        self.scan_subnet = ""
        self.scan_cursor = 0
        self.mode = self.SCAN
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        local_ip, subnet = get_local_subnet()
        if not subnet:
            self.scan_error = "Could not determine local subnet"
            self.scan_running = False
            return
        self.scan_subnet = f"{subnet}.x (from {local_ip})"
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            futs = {
                pool.submit(check_xbdm_host, f"{subnet}.{i}"): i
                for i in range(1, 255)
            }
            for fut in concurrent.futures.as_completed(futs):
                result = fut.result()
                if result:
                    self.scan_results.append(result)
        self.scan_running = False

    def _draw_scan(self):
        self.scr.erase()
        h, w = self._size()
        self._header("Network Scan")

        y = 2
        for line in self.LOGO:
            if y < h - 2:
                self._center(y, line, curses.color_pair(C_GREEN) | curses.A_BOLD)
            y += 1

        y += 1
        if y < h - 2:
            self._center(
                y, "Network Scanner",
                curses.color_pair(C_DIM) | curses.A_DIM,
            )
        y += 2

        col = max(2, (w - 52) // 2)
        if self.scan_running:
            spin = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
            ch = spin[int(time.time() * 10) % len(spin)]
            subnet_info = f" ({self.scan_subnet})" if self.scan_subnet else ""
            self._addline(
                y, col, f" {ch} Scanning port {XBDM_PORT}{subnet_info}...",
                curses.color_pair(C_CYAN),
            )
            y += 1
            if self.scan_results:
                self._addline(
                    y, col, f"   Found {len(self.scan_results)} so far...",
                    curses.color_pair(C_GREEN),
                )
                y += 1
        else:
            if self.scan_error:
                self._addline(
                    y, col, f"   {self.scan_error}",
                    curses.color_pair(C_RED) | curses.A_BOLD,
                )
            elif self.scan_results:
                self._addline(
                    y, col,
                    f"   Found {len(self.scan_results)} Xbox console(s):",
                    curses.color_pair(C_GREEN) | curses.A_BOLD,
                )
            else:
                self._addline(
                    y, col, "   No Xbox consoles found.",
                    curses.color_pair(C_YELLOW),
                )
            y += 1

        y += 1
        if self.scan_cursor >= len(self.scan_results):
            self.scan_cursor = 0
        for i, (ip, name) in enumerate(self.scan_results):
            if y >= h - 2:
                break
            text = f"{ip:<18} {name}"
            if i == self.scan_cursor:
                self._addline(y, col, f" > {text}".ljust(50), curses.color_pair(C_SELECTED) | curses.A_BOLD, 50)
            else:
                self._addline(y, col, f"   {text}", curses.color_pair(C_CYAN))
            y += 1

        status = " Scanning...  Esc Back" if self.scan_running else " \u2191\u2193 Select  Enter Connect  D Set Default  Esc Back"
        self._status(status)
        self.scr.refresh()

    def _key_scan(self):
        key = self.scr.getch()
        if key == -1:
            return True
        if key == 27:
            self.mode = self.MENU
            self.cursor = 0
            return True
        if not self.scan_results:
            return True
        n = len(self.scan_results)
        if key in (curses.KEY_UP, ord("k")):
            self.scan_cursor = (self.scan_cursor - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            self.scan_cursor = (self.scan_cursor + 1) % n
        elif key in (curses.KEY_ENTER, 10, 13):
            ip, name = self.scan_results[self.scan_cursor]
            self._remember_ip(ip, name)
            self._start_monitor(ip)
        elif key in (ord("d"), ord("D")):
            ip, _ = self.scan_results[self.scan_cursor]
            self.config["default_ip"] = ip
            save_config(self.config)
        return True

    # ── Monitor ──────────────────────────────────────────────────────────

    def _draw_monitor(self):
        self.scr.erase()
        h, w = self._size()

        # Header
        if self.conn.connected:
            title = f"xbMacson: {self.conn.xbox_name or self.conn.xbox_ip}"
            if self.conn.xbox_name:
                title += f" ({self.conn.xbox_ip})"
            if self.conn.dm_version:
                title += f": XBDM {self.conn.dm_version}"
        else:
            title = f"xbMacson: connecting to {self.conn.xbox_ip}..."
        self._header(title)

        # Indicator row: show active modes as pill-style badges
        inds = []
        if self.raw_mode:
            inds.append("\u25c9 RAW")
        if self.paused:
            inds.append("\u23f8 PAUSED")
        if self.log_file:
            inds.append(f"\u25cf LOG: {self.log_filename}")
        if not self.auto_scroll:
            inds.append("\u2191 SCROLL LOCK")
        if self.filter_str:
            inds.append(f"\u25b8 FILTER: {self.filter_str}")
        if self.screenshot_status:
            inds.append(f"\u25a3 {self.screenshot_status}")

        log_y = 1
        if inds:
            self._addline(1, 1, "  \u2502  ".join(inds), curses.color_pair(C_YELLOW) | curses.A_BOLD)
            log_y = 2

        # Log area
        area_h = h - log_y - 1
        visible = self._visible_lines()

        if self.paused and not visible:
            pass  # nothing to draw

        if self.auto_scroll:
            start = max(0, len(visible) - area_h)
        else:
            start = max(0, len(visible) - area_h - self.scroll_offset)

        page = visible[start : start + area_h]

        for i, ln in enumerate(page):
            y = log_y + i
            if y >= h - 1:
                break

            x = 0
            if self.raw_mode:
                # Raw mode keeps the timestamp and the exact wire line.
                ts = f"{ln.ts} "
                self._addline(y, x, ts, curses.A_DIM)
                x += len(ts)
                rem = w - x - 1
                if rem > 0:
                    self._addline(y, x, ln.text, curses.color_pair(C_NORMAL), rem)
            else:
                # Match xbWatson: just the message text, colored. Timestamps
                # and thread IDs stay in the log file, not on screen.
                attr = curses.color_pair(ln.color)
                if ln.is_system:
                    attr |= curses.A_BOLD
                rem = w - x - 1
                if rem > 0:
                    self._addline(y, x, ln.text, attr, rem)

        # Status bar
        if self.conn.stopped_thread is not None:
            # Xbox is halted: show exception bar like xbWatson
            reason = self.conn.stopped_reason
            left = f" STOPPED: {reason} (thread {self.conn.stopped_thread})"
            keys = "C:Continue  G:Go  X:Reboot  Q:Quit"
            pad = w - len(left) - len(keys) - 1
            if pad < 1:
                bar = left[:w - 1]
            else:
                bar = left + " " * pad + keys + " "
            # Draw in red to make it obvious
            h_pos = h - 1
            self.scr.attron(curses.color_pair(C_RED) | curses.A_BOLD | curses.A_REVERSE)
            self.scr.addnstr(h_pos, 0, bar.ljust(w), w - 1)
            self.scr.attroff(curses.color_pair(C_RED) | curses.A_BOLD | curses.A_REVERSE)
        else:
            connected_icon = "\u25cf" if self.conn.connected else "\u25cb"
            conn_label = "LIVE" if self.conn.connected else "DISCONNECTED"
            left = f" {connected_icon} {conn_label}"
            left += f"  \u2502  {self.line_count} lines"
            if self.filter_str:
                left += f"  \u2502  filter: {self.filter_str} ({len(visible)} match)"

            keys = "B:Browse S:Screenshot F:Filter R:Raw G:Go L:Log P:Pause C:Clear I:Info X:Reboot Esc:Menu Q:Quit"
            pad = w - len(left) - len(keys) - 1
            if pad < 1:
                bar = left[:w - 1]
            else:
                bar = left + " " * pad + keys + " "
            self._status(bar)
        self.scr.refresh()

    def _key_monitor(self):
        key = self.scr.getch()
        if key == -1:
            return True

        if key in (ord("q"), ord("Q")):
            return False

        if key == 27:  # Esc - back to menu, connection stays alive
            self.mode = self.MENU
            self.cursor = 0

        elif key in (ord("s"), ord("S")):
            self._take_screenshot()

        elif key in (ord("f"), ord("F")):
            self.input_buf = self.filter_str
            self.mode = self.FILTER_INPUT
            curses.curs_set(1)

        elif key in (ord("r"), ord("R")):
            self.raw_mode = not self.raw_mode

        elif key in (ord("p"), ord("P")):
            self.paused = not self.paused

        elif key in (ord("c"), ord("C")):
            if self.conn.stopped_thread is not None:
                # Xbox is halted: Continue execution (like xbWatson)
                threading.Thread(
                    target=self.conn.continue_execution, daemon=True
                ).start()
            else:
                self.log_lines.clear()
                self.line_count = 0
                self.scroll_offset = 0
                self.auto_scroll = True
                self.screenshot_status = ""

        elif key in (ord("g"), ord("G")):
            if self.conn.xbox_ip:
                threading.Thread(
                    target=self.conn.force_go, daemon=True
                ).start()

        elif key in (ord("l"), ord("L")):
            self._toggle_log()

        elif key in (ord("i"), ord("I")):
            self.probe_lines = []
            self.mode = self.PROBE
            threading.Thread(target=self._probe_thread, daemon=True).start()

        elif key in (ord("d"), ord("D")):
            if self.conn.xbox_ip:
                self.config["default_ip"] = self.conn.xbox_ip
                save_config(self.config)

        elif key in (ord("x"), ord("X")):
            if self.conn.xbox_ip:
                if self.conn.stopped_thread is not None:
                    # Xbox is halted: reboot immediately like xbWatson
                    self._do_reboot()
                else:
                    self._begin_confirm(
                        "Reboot the Xbox?", self._do_reboot, self.MONITOR
                    )

        elif key in (ord("b"), ord("B")):
            if self.conn.xbox_ip:
                self._begin_drives(self.MONITOR)

        elif key == curses.KEY_UP:
            self.auto_scroll = False
            self.scroll_offset += 1
            mx = max(0, len(self._visible_lines()) - 1)
            self.scroll_offset = min(self.scroll_offset, mx)

        elif key == curses.KEY_DOWN:
            self.scroll_offset = max(0, self.scroll_offset - 1)
            if self.scroll_offset == 0:
                self.auto_scroll = True

        elif key == curses.KEY_PPAGE:
            self.auto_scroll = False
            self.scroll_offset += self._size()[0] - 3
            mx = max(0, len(self._visible_lines()) - 1)
            self.scroll_offset = min(self.scroll_offset, mx)

        elif key == curses.KEY_NPAGE:
            self.scroll_offset -= self._size()[0] - 3
            if self.scroll_offset <= 0:
                self.scroll_offset = 0
                self.auto_scroll = True

        elif key == curses.KEY_HOME:
            self.auto_scroll = False
            self.scroll_offset = max(0, len(self._visible_lines()) - 1)

        elif key == curses.KEY_END:
            self.scroll_offset = 0
            self.auto_scroll = True

        return True

    def _toggle_log(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None
            self.log_filename = ""
        else:
            self.log_filename = time.strftime("xbmacson_%Y%m%d_%H%M%S.log")
            log_dir = self.config.get("log_dir", "")
            path = os.path.join(log_dir, self.log_filename) if log_dir else self.log_filename
            try:
                self.log_file = open(path, "a")
                # Dump existing buffer so we capture what's already on screen
                for ln in self.log_lines:
                    pfx = f"[{ln.ts}]"
                    if ln.thread_id:
                        pfx += f" [T{ln.thread_id}]"
                    self.log_file.write(f"{pfx} {ln.text}\n")
                self.log_file.flush()
            except OSError:
                self.log_file = None
                self.log_filename = ""

    # ── Screenshot ───────────────────────────────────────────────────────

    def _take_screenshot(self):
        if not self.conn.xbox_ip:
            return
        self.screenshot_status = "Capturing..."
        threading.Thread(target=self._screenshot_thread, daemon=True).start()

    def _screenshot_thread(self):
        save_dir = self.config.get("screenshot_dir", SCREENSHOT_DIR)
        result = take_screenshot(self.conn.xbox_ip, save_dir)
        if result.startswith("Error") or result.startswith("Incomplete"):
            self.screenshot_status = f"Screenshot failed: {result}"
        else:
            fname = os.path.basename(result)
            self.screenshot_status = f"Saved: {fname}"
        threading.Timer(5.0, self._clear_screenshot_status).start()

    def _clear_screenshot_status(self):
        self.screenshot_status = ""

    # ── Reboot ───────────────────────────────────────────────────────────

    def _do_reboot(self):
        if not self.conn.xbox_ip:
            return
        self.msg_queue.put(
            LogLine(time.strftime("%H:%M:%S"), None,
                    "Reboot command sent. Xbox is restarting...",
                    C_YELLOW, is_system=True)
        )
        # Send REBOOT on a one-shot connection and leave the monitor thread
        # running so it reconnects on its own once the box comes back up.
        xbdm_query(self.conn.xbox_ip, "REBOOT")

    # ── Confirm Dialog ───────────────────────────────────────────────────

    def _begin_confirm(self, msg, callback, return_mode):
        self.confirm_msg = msg
        self.confirm_cb = callback
        self.confirm_return = return_mode
        self.mode = self.CONFIRM

    def _draw_confirm(self):
        self.scr.erase()
        h, w = self._size()
        self._header("Confirm")
        y = h // 2 - 1
        self._center(y, self.confirm_msg, curses.color_pair(C_YELLOW) | curses.A_BOLD)
        self._center(y + 2, "Y = Yes     N / Esc = Cancel", curses.color_pair(C_DIM))
        self._status(" Y: Confirm | N/Esc: Cancel")
        self.scr.refresh()

    def _key_confirm(self):
        key = self.scr.getch()
        if key == -1:
            return True
        if key in (ord("y"), ord("Y")):
            if self.confirm_cb:
                self.confirm_cb()
            self.mode = self.confirm_return
        elif key in (27, ord("n"), ord("N")):
            self.mode = self.confirm_return
        return True

    # ── Filter Input (overlays monitor) ──────────────────────────────────

    def _draw_filter(self):
        # Draw monitor underneath
        self._draw_monitor()
        # Overwrite status bar with filter input
        h, w = self._size()
        prompt = " Filter: "
        line = prompt + self.input_buf
        self.scr.attron(curses.color_pair(C_INPUT))
        self.scr.addnstr(h - 1, 0, line.ljust(w), w - 1)
        self.scr.attroff(curses.color_pair(C_INPUT))
        cx = len(line)
        if cx < w:
            self.scr.move(h - 1, cx)
        self.scr.refresh()

    def _key_filter(self):
        key = self.scr.getch()
        if key == -1:
            return True
        if key == 27:
            curses.curs_set(0)
            self.mode = self.MONITOR
        elif key in (curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            self.filter_str = self.input_buf
            self.mode = self.MONITOR
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.input_buf = self.input_buf[:-1]
            self.filter_str = self.input_buf  # live filter
        elif 32 <= key <= 126:
            self.input_buf += chr(key)
            self.filter_str = self.input_buf
        return True

    # ── Probe ────────────────────────────────────────────────────────────

    def _probe_thread(self):
        ip = self.conn.xbox_ip
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((ip, XBDM_PORT))

            greeting = sock.recv(RECV_BUFSIZE).decode("utf-8", errors="replace").strip()
            self.probe_lines.append(f"Greeting:  {greeting}")

            for cmd in [b"DMVERSION\r\n", b"DBGNAME\r\n", b"XBEINFO running\r\n", b"MODULES\r\n"]:
                try:
                    sock.sendall(cmd)
                    resp = sock.recv(RECV_BUFSIZE).decode("utf-8", errors="replace").strip()
                    label = cmd.decode().strip()
                    self.probe_lines.append(f"{label:<16} \u2192 {resp}")
                except Exception:
                    pass

            sock.close()
        except Exception as e:
            self.probe_lines.append(f"Probe failed: {e}")

    def _draw_probe(self):
        self.scr.erase()
        h, w = self._size()
        self._header(f"Xbox Info: {self.conn.xbox_ip}")

        y = 2
        for line in self.LOGO:
            if y < h - 2:
                self._center(y, line, curses.color_pair(C_GREEN) | curses.A_BOLD)
            y += 1

        y += 1
        if y < h - 2:
            self._center(
                y, "System Info",
                curses.color_pair(C_DIM) | curses.A_DIM,
            )
        y += 2

        col = max(2, (w - 52) // 2)
        if not self.probe_lines:
            self._addline(y, col, "   Probing...", curses.color_pair(C_DIM) | curses.A_DIM)
        else:
            for line in self.probe_lines:
                if y >= h - 2:
                    break
                self._addline(y, col, f"   {line}", curses.color_pair(C_CYAN))
                y += 1

        self._status(" Esc Back to monitor")
        self.scr.refresh()

    def _key_probe(self):
        key = self.scr.getch()
        if key == -1:
            return True
        if key in (27, curses.KEY_ENTER, 10, 13):
            self.mode = self.MONITOR
        elif key in (ord("q"), ord("Q")):
            return False
        return True

    # ── Settings ─────────────────────────────────────────────────────────

    def _draw_settings(self):
        self.scr.erase()
        h, w = self._size()
        self._header("Settings")

        y = 2
        for line in self.LOGO:
            if y < h - 2:
                self._center(y, line, curses.color_pair(C_GREEN) | curses.A_BOLD)
            y += 1

        y += 1
        if y < h - 2:
            self._center(
                y, "Settings Menu",
                curses.color_pair(C_DIM) | curses.A_DIM,
            )
        y += 2

        dip = self.config.get("default_ip", "") or "(not set)"
        ldir = self.config.get("log_dir", "") or "(current directory)"
        ar = "Yes" if self.config.get("auto_reconnect", True) else "No"
        dl = "Yes" if self.config.get("default_logging", False) else "No"

        items = [
            f"Default Xbox IP:    {dip}",
            f"Log Directory:      {ldir}",
            f"Auto-Reconnect:     {ar}",
            f"Default Logging:    {dl}",
            "Clear Recent Consoles",
            "Back",
        ]

        if self.cursor >= len(items):
            self.cursor = 0

        col = max(2, (w - 52) // 2)
        for i, text in enumerate(items):
            if y >= h - 2:
                break
            if i == self.cursor:
                self._addline(y, col, f" > {text}".ljust(50), curses.color_pair(C_SELECTED) | curses.A_BOLD, 50)
            else:
                self._addline(y, col, f"   {text}", curses.color_pair(C_CYAN))
            y += 1

        self._status(" \u2191\u2193 Navigate  Enter Edit/Toggle  Esc Back")
        self.scr.refresh()

    def _key_settings(self):
        key = self.scr.getch()
        if key == -1:
            return True
        if key == 27:
            self.mode = self.MENU
            self.cursor = 0
        elif key in (curses.KEY_UP, ord("k")):
            self.cursor = (self.cursor - 1) % 6
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = (self.cursor + 1) % 6
        elif key in (curses.KEY_ENTER, 10, 13):
            if self.cursor == 0:  # default IP
                self.input_buf = self.config.get("default_ip", "")
                self.input_prompt = "Default Xbox IP: "
                self.input_cb = self._on_set_ip
                self.input_return_mode = self.SETTINGS
                self.mode = self.TEXT_INPUT
                curses.curs_set(1)
            elif self.cursor == 1:  # log dir
                self.input_buf = self.config.get("log_dir", "")
                self.input_prompt = "Log Directory: "
                self.input_cb = self._on_set_logdir
                self.input_return_mode = self.SETTINGS
                self.mode = self.TEXT_INPUT
                curses.curs_set(1)
            elif self.cursor == 2:  # auto-reconnect toggle
                self.config["auto_reconnect"] = not self.config.get("auto_reconnect", True)
                save_config(self.config)
            elif self.cursor == 3:  # default logging toggle
                self.config["default_logging"] = not self.config.get("default_logging", False)
                save_config(self.config)
            elif self.cursor == 4:  # clear recent
                self.config["last_connected"] = []
                save_config(self.config)
            elif self.cursor == 5:  # back
                self.mode = self.MENU
                self.cursor = 0
        return True

    def _on_set_ip(self, val):
        self.config["default_ip"] = val.strip()
        save_config(self.config)
        self.mode = self.SETTINGS

    def _on_set_logdir(self, val):
        self.config["log_dir"] = val.strip()
        save_config(self.config)
        self.mode = self.SETTINGS

    # ── Drives Mode ─────────────────────────────────────────────────────

    def _begin_drives(self, return_mode):
        self.drives = []
        self.drives_loading = True
        self.drives_cursor = 0
        self.drives_return = return_mode
        self.mode = self.DRIVES
        threading.Thread(target=self._drives_thread, daemon=True).start()

    def _drives_thread(self):
        resp = xbdm_query(self.conn.xbox_ip, "DRIVELIST")
        self.drives = parse_drivelist(resp)
        self.drives_loading = False

    def _draw_drives(self):
        self.scr.erase()
        h, w = self._size()
        self._header(f"File Browser: {self.conn.xbox_ip}")

        y = 2
        for line in self.LOGO:
            if y < h - 2:
                self._center(y, line, curses.color_pair(C_GREEN) | curses.A_BOLD)
            y += 1

        y += 1
        if y < h - 2:
            self._center(
                y, "File Browser",
                curses.color_pair(C_DIM) | curses.A_DIM,
            )
        y += 2

        col = max(2, (w - 52) // 2)
        if self.drives_loading:
            spin = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
            ch = spin[int(time.time() * 10) % len(spin)]
            self._addline(y, col, f" {ch} Loading drives...", curses.color_pair(C_CYAN))
        elif not self.drives:
            self._addline(y, col, "   No drives found.", curses.color_pair(C_YELLOW))
        else:
            if self.drives_cursor >= len(self.drives):
                self.drives_cursor = 0
            for i, drv in enumerate(self.drives):
                if y >= h - 2:
                    break
                text = f"{drv}:\\"
                if i == self.drives_cursor:
                    self._addline(
                        y, col, f" > {text}".ljust(50),
                        curses.color_pair(C_SELECTED) | curses.A_BOLD, 50,
                    )
                else:
                    self._addline(y, col, f"   {text}", curses.color_pair(C_CYAN))
                y += 1

        self._status(" \u2191\u2193 Navigate  Enter Open  Esc Back")
        self.scr.refresh()

    def _key_drives(self):
        key = self.scr.getch()
        if key == -1:
            return True
        if key == 27:
            self.mode = self.drives_return
            self.cursor = 0
        elif self.drives and not self.drives_loading:
            n = len(self.drives)
            if key in (curses.KEY_UP, ord("k")):
                self.drives_cursor = (self.drives_cursor - 1) % n
            elif key in (curses.KEY_DOWN, ord("j")):
                self.drives_cursor = (self.drives_cursor + 1) % n
            elif key in (curses.KEY_ENTER, 10, 13):
                drv = self.drives[self.drives_cursor]
                self._browse_path(f"{drv}:\\")
        elif key in (ord("q"), ord("Q")):
            return False
        return True

    # ── Browser Mode ────────────────────────────────────────────────────

    def _browse_path(self, path):
        self.browser_path = path
        self.browser_entries = []
        self.browser_cursor = 0
        self.browser_scroll = 0
        self.browser_loading = True
        self.mode = self.BROWSER
        threading.Thread(target=self._browser_thread, daemon=True).start()

    def _browser_thread(self):
        resp = xbdm_query(self.conn.xbox_ip, f'DIRLIST name="{self.browser_path}"')
        self.browser_entries = parse_dirlist(resp)
        self.browser_loading = False

    def _draw_browser(self):
        self.scr.erase()
        h, w = self._size()
        self._header(f"File Browser: {self.browser_path}")

        y = 2
        for line in self.LOGO:
            if y < h - 2:
                self._center(y, line, curses.color_pair(C_GREEN) | curses.A_BOLD)
            y += 1

        y += 1
        if y < h - 2:
            self._center(
                y, self.browser_path,
                curses.color_pair(C_DIM) | curses.A_DIM,
            )
        y += 2

        col = max(2, (w - 52) // 2)

        # Build display list: parent entry + directory contents
        display = []
        display.append(("..", True, 0, True))  # parent nav
        for e in self.browser_entries:
            display.append((e["name"], e["is_dir"], e["size"], False))

        nd = len(display)
        if self.browser_cursor >= nd:
            self.browser_cursor = max(0, nd - 1)

        if self.browser_loading:
            spin = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
            ch = spin[int(time.time() * 10) % len(spin)]
            self._addline(y, col, f" {ch} Loading...", curses.color_pair(C_CYAN))
        elif nd <= 1:
            self._addline(y, col, "   Empty directory.", curses.color_pair(C_YELLOW))
        else:
            area_h = h - y - 1
            # Adjust scroll to keep cursor visible
            if self.browser_cursor < self.browser_scroll:
                self.browser_scroll = self.browser_cursor
            if self.browser_cursor >= self.browser_scroll + area_h:
                self.browser_scroll = self.browser_cursor - area_h + 1

            page = display[self.browser_scroll:self.browser_scroll + area_h]

            for i, (name, is_dir, size, is_parent) in enumerate(page):
                if y >= h - 1:
                    break
                didx = self.browser_scroll + i

                if is_dir:
                    icon = "\u2190" if is_parent else "\u25b6"
                    size_str = "<DIR>"
                else:
                    icon = " "
                    size_str = _fmt_size(size)

                entry_text = f"{icon} {name:<33} {size_str:>10}"

                if didx == self.browser_cursor:
                    attr = curses.color_pair(C_SELECTED) | curses.A_BOLD
                    self._addline(y, col, f" > {entry_text}".ljust(50), attr, 50)
                else:
                    if is_dir:
                        color = C_CYAN
                    elif name.lower().endswith(".xbe"):
                        color = C_GREEN
                    else:
                        color = C_NORMAL
                    self._addline(y, col, f"   {entry_text}", curses.color_pair(color))
                y += 1

        self._status(" \u2191\u2193 Navigate  Enter Open/Launch  Backspace Up  R Refresh  Esc Drives")
        self.scr.refresh()

    def _key_browser(self):
        key = self.scr.getch()
        if key == -1:
            return True
        if key == 27:
            self.mode = self.DRIVES
        elif key in (ord("q"), ord("Q")):
            return False
        elif self.browser_loading:
            return True
        else:
            # Display list: ["..", *entries]: cursor 0 = parent
            nd = 1 + len(self.browser_entries)
            if key in (curses.KEY_UP, ord("k")):
                self.browser_cursor = max(0, self.browser_cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.browser_cursor = min(nd - 1, self.browser_cursor + 1)
            elif key == curses.KEY_PPAGE:
                area_h = self._size()[0] - 3
                self.browser_cursor = max(0, self.browser_cursor - area_h)
            elif key == curses.KEY_NPAGE:
                area_h = self._size()[0] - 3
                self.browser_cursor = min(nd - 1, self.browser_cursor + area_h)
            elif key == curses.KEY_HOME:
                self.browser_cursor = 0
            elif key == curses.KEY_END:
                self.browser_cursor = nd - 1
            elif key in (curses.KEY_ENTER, 10, 13):
                if self.browser_cursor == 0:
                    # ".." parent entry
                    self._browser_go_up()
                else:
                    entry = self.browser_entries[self.browser_cursor - 1]
                    if entry["is_dir"]:
                        self._browse_path(self.browser_path + entry["name"] + "\\")
                    elif entry["name"].lower().endswith(".xbe"):
                        full_path = self.browser_path + entry["name"]
                        self._begin_confirm(
                            f'Launch {entry["name"]}?',
                            lambda p=full_path: self._launch_xbe(p),
                            self.BROWSER,
                        )
            elif key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT):
                self._browser_go_up()
            elif key in (ord("r"), ord("R")):
                self._browse_path(self.browser_path)
        return True

    def _browser_go_up(self):
        """Navigate up one directory or back to drives."""
        path = self.browser_path.rstrip("\\")
        idx = path.rfind("\\")
        if idx >= 0 and idx > 1:
            self._browse_path(path[:idx + 1])
        else:
            self.mode = self.DRIVES

    def _launch_xbe(self, full_path):
        self.msg_queue.put(
            LogLine(time.strftime("%H:%M:%S"), None,
                    f'Launching {full_path}...',
                    C_YELLOW, is_system=True)
        )
        # Launch on a one-shot connection and keep the monitor running so it
        # reconnects and catches the new title's startup output.
        xbdm_query(self.conn.xbox_ip, f'MAGICBOOT title="{full_path}" debug')
        self.mode = self.MONITOR


# ── Entry Point ──────────────────────────────────────────────────────────

def main():
    # Optional: pass IP as single argument for quick-connect
    quick_ip = None
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
        quick_ip = sys.argv[1]

    def run(stdscr):
        app = XbMacsonTUI(stdscr)
        if quick_ip:
            app._start_monitor(quick_ip)
        app.run()

    curses.wrapper(run)


if __name__ == "__main__":
    main()
