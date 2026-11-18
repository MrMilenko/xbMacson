"""Notifications: parsing, xbWatson-style formatting, and the listener.

XBDM pushes one line per event on a notification channel. Two ways to get one:

- ``notifyat``: we listen on a local port and ask XBDM to dial us. XBDM keeps
  the registration, so it dials again after a reset, a reboot or a title
  launch. This is what xbWatson does and what the original Xbox profile uses.
- ``notify``: turn an ordinary command connection into the channel. Simple,
  but the channel dies with the connection. OXDK's 360 tool uses this.
"""

import os
import socket
import threading
import time

from .connection import enable_keepalive
from .protocol import ConnectError, XbdmError, parse_kv, to_int

# -- events ---------------------------------------------------------------

STRING_KINDS = ("debugstr", "assert", "rip")
EXEC_STATES = ("stopped", "started", "rebooting", "pending")


class Event:
    """One notification line, parsed.

    ``kind`` is the first word (debugstr, exception, modload ...), or
    "status" for a message from the listener itself. ``kv`` holds the
    key=value fields with lower-case keys, ``string`` the free text of
    debugstr/assert/rip, and ``stop`` is true when XBDM halted the thread.
    """

    __slots__ = ("kind", "kv", "string", "raw", "stop", "ts", "color")

    def __init__(self, kind, kv=None, string="", raw="", stop=False, color=None):
        self.kind = kind
        self.kv = kv or {}
        self.string = string
        self.raw = raw
        self.stop = stop
        self.ts = time.strftime("%H:%M:%S")
        self.color = color

    @property
    def thread(self):
        t = self.kv.get("thread")
        return t if isinstance(t, str) else None

    @property
    def thread_int(self):
        return to_int(self.thread)

    def get_int(self, key, default=None):
        return to_int(self.kv.get(key), default)

    def __repr__(self):
        return f"Event({self.kind}, {self.raw!r})"


def status_event(text, color="cyan"):
    return Event("status", string=text, raw=text, color=color)


def parse_notification(line):
    line = line.rstrip("\r\n")
    if not line.strip() or line.strip() == "null":
        return Event("null", raw=line)
    parts = line.split(None, 1)
    kind = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    string = ""
    if kind in STRING_KINDS:
        idx = rest.find("string=")
        if idx >= 0 and (idx == 0 or rest[idx - 1] == " "):
            string = rest[idx + len("string="):]
            rest = rest[:idx]
    kv = parse_kv(rest)
    stop = kv.get("stop") is True
    return Event(kind, kv, string, line, stop)


# -- formatting -----------------------------------------------------------

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


def exception_name(code):
    return EXCEPTION_NAMES.get(code, f"0x{code:08X}")


def classify_line(text):
    """Pick a color for a debug string from its words."""
    lower = text.lower()
    if any(w in lower for w in (
            "error", "fail", "assert", "crash", "fatal", "exception",
            "abort", "panic", "corrupt", "violation", "denied")):
        return "red"
    if any(w in lower for w in (
            "warn", "invalid", "not found", "deprecated", "timeout",
            "retry", "slow", "missing", "skip", "fallback")):
        return "yellow"
    if any(w in lower for w in (
            "init", "start", "loaded", "success", "connected", "ready",
            "complete", "done", "ok", "mounted", "registered")):
        return "green"
    if any(w in lower for w in ("debug", "trace", "verbose")):
        return "blue"
    if text.startswith("[") and "]" in text:
        return "cyan"
    return "normal"


class Line:
    """A line ready for the screen or a log file."""

    __slots__ = ("ts", "thread_id", "text", "color", "is_system", "raw")

    def __init__(self, text, color="normal", is_system=False, thread_id=None,
                 ts=None, raw=""):
        self.ts = ts or time.strftime("%H:%M:%S")
        self.thread_id = thread_id
        self.text = text
        self.color = color
        self.is_system = is_system
        self.raw = raw or text

    def log_text(self):
        pfx = f"[{self.ts}]"
        if self.thread_id:
            pfx += f" [T{self.thread_id}]"
        return f"{pfx} {self.text}"


class Formatter:
    """Turns events into the lines xbWatson would show.

    Keeps per-thread buffers so ``cr`` debug strings join into one line, and
    records when the console stops (``stopped_thread``/``stopped_reason``).
    """

    def __init__(self, console_name="", console_label="Xbox"):
        self.console_name = console_name
        self.console_label = console_label
        self._bufs = {}
        self.stopped_thread = None
        self.stopped_reason = ""

    def reset(self):
        self._bufs = {}
        self.stopped_thread = None
        self.stopped_reason = ""

    def clear_stop(self):
        self.stopped_thread = None
        self.stopped_reason = ""

    def format(self, ev):
        out = []

        def sysline(text, color="red", tid=None):
            out.append(Line(text, color, True, tid, ev.ts, ev.raw))

        def stopped(tid, reason):
            self.stopped_thread = tid
            self.stopped_reason = reason
            sysline(f"STOPPED: {reason} (thread {tid})", "red", tid)

        k = ev.kind
        tid = ev.thread
        if k == "null":
            return out
        if k == "status":
            sysline(ev.string, ev.color or "cyan")
            return out
        if k == "debugstr":
            msg = ev.string
            if ev.kv.get("cr") is True:
                self._bufs[tid] = self._bufs.get(tid, "") + msg
            else:
                full = self._bufs.pop(tid, "") + msg
                if full.strip():
                    out.append(Line(full, classify_line(full), False, tid, ev.ts, ev.raw))
            if ev.stop:
                stopped(tid, "Debug String")
            return out
        if k == "assert":
            sysline(f"{self.console_label} Assertion Failed - thread {tid} "
                    f"[{self.console_name}]", "red", tid)
            if ev.string.strip():
                sysline(ev.string.strip(), "red", tid)
            if ev.stop:
                stopped(tid, "Assertion Failed")
            return out
        if k == "exception":
            code = ev.get_int("code", 0)
            addr = ev.get_int("address", 0)
            name = exception_name(code)
            first = ev.kv.get("first") is True
            rw = "write" if "write" in ev.kv else ("read" if "read" in ev.kv else None)
            if code == 0xC0000005 and rw:
                target = ev.get_int(rw, 0)
                verb = "written" if rw == "write" else "read"
                sysline(f"The instruction at address 0x{addr:08X} referenced "
                        f"memory at address 0x{target:08X}.  "
                        f"The memory could not be {verb}.", "red", tid)
            elif code == 0x80000003:
                sysline(f"A breakpoint exception (0x{addr:08X}) has been reached "
                        f"in the application at location 0x{addr:08X}.", "red", tid)
            else:
                chance = "first" if first else "second"
                sysline(f"An exception ({name}) occurred in the application "
                        f"at location 0x{addr:08X} ({chance} chance).", "red", tid)
            if ev.kv.get("noncont") is True:
                sysline("Exception is non-continuable.", "red", tid)
            if ev.stop and not first:
                # xbWatson only stops for second-chance exceptions.
                stopped(tid, f"Exception ({name})")
            return out
        if k == "break":
            addr = ev.get_int("addr", 0)
            sysline(f"Break: 0x{addr:08X} thread={tid}", "red", tid)
            if ev.stop:
                stopped(tid, "Breakpoint")
            return out
        if k == "singlestep":
            return out  # xbWatson never shows these
        if k == "data":
            access = next((a for a in ("read", "write", "execute") if a in ev.kv), "?")
            data_addr = ev.get_int(access, 0)
            addr = ev.get_int("addr", 0)
            sysline(f"Databreak: {access}=0x{data_addr:08X} "
                    f"addr=0x{addr:08X} thread={tid}", "red", tid)
            if ev.stop:
                stopped(tid, "Data Breakpoint")
            return out
        if k == "execution":
            state = next((s for s in EXEC_STATES if s in ev.kv), None)
            if state is None:
                state = (ev.raw.split(None, 1) + [""])[1].strip().lower()
            msgs = {
                "rebooting": f"xbMacson: {self.console_label} is restarting.",
                "pending": "Execution pending.",
                "started": "Execution started.",
                "stopped": "Execution stopped.",
            }
            sysline(msgs.get(state, f"Execution: {state}"), "yellow")
            return out
        if k == "rip":
            sysline(f"A RIP error has occurred on the {self.console_label} "
                    f"(thread {tid}).", "red", tid)
            if ev.string.strip():
                sysline(f"The error description was: {ev.string.strip()}", "red", tid)
            if ev.stop:
                stopped(tid, "RIP Error")
            return out
        if k == "stacktrace":
            return out
        if k == "modload":
            name = ev.kv.get("name", "?")
            base = ev.get_int("base")
            size = ev.get_int("size")
            b = f"0x{base:08X}" if base is not None else "0x?"
            s = f"0x{size:08X}" if size is not None else "0x?"
            xbe = " [XBE]" if ev.kv.get("xbe") is True else ""
            sysline(f"Loaded: {name} base={b} size={s}{xbe}", "green")
            return out
        if k in ("sectload", "sectunload"):
            name = ev.kv.get("name", "?")
            if k == "sectload":
                base = ev.get_int("base")
                size = ev.get_int("size")
                b = f"0x{base:08X}" if base is not None else "0x?"
                s = f"0x{size:08X}" if size is not None else "0x?"
                sysline(f"Section loaded: {name} base={b} size={s}", "green")
            else:
                sysline(f"Section unloaded: {name}", "yellow")
            return out
        if k == "modunload":
            sysline(f"Unloaded: {ev.kv.get('name', '?')}", "yellow")
            return out
        if k in ("create", "destroy", "terminate"):
            return out  # thread churn buries the real output
        # Unknown: show it as it came.
        sysline(ev.raw, classify_line(ev.raw))
        return out


# -- listener -------------------------------------------------------------

class NotifyListener:
    """Keeps a notification channel open and hands every event to ``on_event``.

    ``console`` is a client.Console used for the command side (dbgname,
    debugger connect, notifyat). Status messages arrive as events of kind
    "status" so a single callback sees everything in order.
    """

    def __init__(self, console, on_event, channel=None, bind_host=None,
                 auto_reconnect=True, reconnect_timeout=60.0,
                 accept_timeout=30.0, recv_timeout=30.0,
                 reconnect_interval=0.4):
        self.console = console
        self.on_event = on_event
        self.channel = channel or console.profile.channel
        # All interfaces by default, so the console can reach us.
        self.bind_host = bind_host or os.environ.get("XBMACSON_BIND") or "0.0.0.0"
        self.auto_reconnect = auto_reconnect
        self.reconnect_timeout = reconnect_timeout
        self.accept_timeout = accept_timeout
        self.recv_timeout = recv_timeout
        self.reconnect_interval = reconnect_interval

        self.running = False
        self.connected = False
        self.sessions = 0           # notification sessions accepted so far
        self.console_name = ""
        self.dm_version = ""
        self.notify_port = 0
        self.gave_up = False
        self._listen = None
        self._sock = None
        self._thread = None

    @property
    def label(self):
        return self.console.profile.name

    def _status(self, text, color="cyan"):
        self.on_event(status_event(text, color))

    # -- control ---------------------------------------------------------

    def start(self):
        self.running = True
        self.gave_up = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, unregister=True, disconnect=None):
        """Stop listening. ``unregister`` drops our notifyat entry so the
        console stops dialing a port nobody listens on. ``disconnect`` (default:
        same as unregister) also ends the debug session; leave it off to keep
        a stopped thread stopped."""
        self.running = False
        self.connected = False
        if disconnect is None:
            disconnect = unregister
        if unregister and self.channel == "notifyat" and self.notify_port:
            try:
                self.console.notifyat(self.notify_port, drop=True)
            except (OSError, XbdmError):
                pass
        if disconnect:
            try:
                self.console.debugger(False)
            except (OSError, XbdmError):
                pass
        for s in (self._sock, self._listen):
            if s is not None:
                # shutdown wakes a thread blocked in recv/accept; close alone
                # does not on every platform.
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass
        self._sock = None
        self._listen = None
        self.notify_port = 0

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    # -- internals -------------------------------------------------------

    def _banner(self):
        self._status(f"Connecting to {self.console.host}:{self.console.port} ...")
        for attr, fn in (("console_name", self.console.dbgname),
                         ("dm_version", self.console.dmversion)):
            try:
                setattr(self, attr, fn().strip())
            except (OSError, XbdmError):
                pass

    def _connected_banner(self):
        name = f" ({self.console_name})" if self.console_name else ""
        self._status(f"Connected to {self.console.host}{name}.", "green")

    def _give_up(self, text):
        self._status(text, "red")
        self.gave_up = True
        self.running = False

    def _pump(self, rf):
        """Read lines from a binary file object until the session ends.
        Returns on a clean close and raises OSError on a dead link."""
        while self.running:
            raw = rf.readline(65536)
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                self.on_event(parse_notification(line))

    def _run(self):
        try:
            if self.channel == "notify":
                self._run_dedicated()
            else:
                self._run_notifyat()
        finally:
            self.connected = False
            if self.gave_up and self._listen is not None:
                try:
                    self._listen.close()
                except OSError:
                    pass
                self._listen = None

    def _run_notifyat(self):
        first = True
        shown_error = False
        deadline = None
        registered = False

        try:
            self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listen.bind((self.bind_host, 0))
            self._listen.listen(2)
            self.notify_port = self._listen.getsockname()[1]
        except OSError as e:
            self._give_up(f"Could not bind a local port: {e}")
            return

        while self.running:
            if not registered:
                try:
                    if first:
                        self._banner()
                    try:
                        self.console.debugger(True)
                    except XbdmError:
                        pass
                    self.console.notifyat(self.notify_port)
                    registered = True
                    deadline = None
                except (OSError, XbdmError):
                    if not self.running:
                        break
                    if not shown_error:
                        self._status(f"Waiting for {self.label}...", "yellow")
                        shown_error = True
                    if deadline is None:
                        deadline = time.monotonic() + self.reconnect_timeout
                    if time.monotonic() > deadline:
                        self._give_up(f"Could not reach {self.label} within "
                                      f"{int(self.reconnect_timeout)}s; giving up.")
                        break
                    time.sleep(self.reconnect_interval)
                    continue

            # Wait for XBDM to dial us. If it does not, it may have dropped
            # our registration after a failed call, so register again.
            try:
                self._listen.settimeout(self.accept_timeout)
                sock, _ = self._listen.accept()
            except socket.timeout:
                if not self.running:
                    break
                if not shown_error:
                    self._status(f"Waiting for {self.label}...", "yellow")
                    shown_error = True
                if deadline is None:
                    deadline = time.monotonic() + self.reconnect_timeout
                if time.monotonic() > deadline:
                    self._give_up(f"{self.label} did not dial back within "
                                  f"{int(self.reconnect_timeout)}s; giving up.")
                    break
                registered = False
                continue
            except OSError as e:
                if not self.running:
                    break
                if not shown_error:
                    self._status(f"Accept failed: {e}", "red")
                    shown_error = True
                registered = False
                time.sleep(self.reconnect_interval)
                continue

            self._sock = sock
            enable_keepalive(sock)
            sock.settimeout(self.recv_timeout)
            self.connected = True
            self.sessions += 1
            deadline = None
            if first:
                self._connected_banner()
                first = False
            shown_error = False

            lost = False
            rf = sock.makefile("rb")
            try:
                self._pump(rf)
            except OSError:
                lost = True
            finally:
                self.connected = False
                try:
                    rf.close()
                    sock.close()
                except OSError:
                    pass
                self._sock = None
            if not self.running:
                break
            if lost and not shown_error:
                self._status("Connection lost, reconnecting...", "yellow")
                shown_error = True
            if not self.auto_reconnect:
                self._give_up("Notification channel closed.")
                break
            # XBDM kept our registration and will dial again once it is up.
            time.sleep(self.reconnect_interval)

    def _run_dedicated(self):
        first = True
        shown_error = False
        deadline = None
        while self.running:
            try:
                if first:
                    self._banner()
                conn = self.console.connect()
            except (ConnectError, OSError):
                if not self.running:
                    break
                if not shown_error:
                    self._status(f"Waiting for {self.label}...", "yellow")
                    shown_error = True
                if deadline is None:
                    deadline = time.monotonic() + self.reconnect_timeout
                if time.monotonic() > deadline:
                    self._give_up(f"Could not reach {self.label} within "
                                  f"{int(self.reconnect_timeout)}s; giving up.")
                    break
                time.sleep(self.reconnect_interval)
                continue
            lost = False
            try:
                conn.command("notify reconnectport=0", check=True)
                sock = conn.sock
                self._sock = sock
                enable_keepalive(sock)
                sock.settimeout(self.recv_timeout)
                self.connected = True
                self.sessions += 1
                deadline = None
                if first:
                    self._connected_banner()
                    first = False
                shown_error = False
                # Keep reading through the same buffered reader, so nothing it
                # already pulled off the socket is lost.
                self._pump(conn._rf)
            except XbdmError as e:
                self._give_up(f"notify refused: {e}")
            except OSError:
                lost = True
            finally:
                self.connected = False
                conn.close(send_bye=False)
                self._sock = None
            if not self.running:
                break
            if lost and not shown_error:
                self._status("Connection lost, reconnecting...", "yellow")
                shown_error = True
            if not self.auto_reconnect:
                self._give_up("Notification channel closed.")
                break
            time.sleep(self.reconnect_interval)
