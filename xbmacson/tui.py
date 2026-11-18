"""Curses terminal UI. Same screens and keys as the original single-file
xbMacson, now running on the library."""

import curses
import os
import queue
import threading
import time

from . import scan
from .client import Console
from .config import SCREENSHOT_DIR, load_config, remember, save_config
from .notify import Formatter, Line, NotifyListener
from .profiles import get_profile
from .protocol import XbdmError

MAX_LOG_LINES = 10000

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

COLOR_PAIRS = {
    "normal": C_NORMAL, "red": C_RED, "green": C_GREEN, "yellow": C_YELLOW,
    "cyan": C_CYAN, "magenta": C_MAGENTA, "blue": C_BLUE,
}

# Thread color pairs: 20-27
THREAD_COLORS_BASE = 20
NUM_THREAD_COLORS = 7


class Session:
    """The monitor's live connection: a Console for commands, a
    NotifyListener for the channel, and a Formatter that turns events into
    screen lines on ``msg_queue``."""

    def __init__(self, msg_queue, profile, auto_reconnect=True):
        self.msg_queue = msg_queue
        self.profile = profile
        self.auto_reconnect = auto_reconnect
        self.console = None
        self.listener = None
        self.formatter = Formatter(console_label=profile.name)
        self.xbox_ip = ""

    # Attributes the screens read.
    @property
    def running(self):
        return bool(self.listener and self.listener.running)

    @property
    def connected(self):
        return bool(self.listener and self.listener.connected)

    @property
    def xbox_name(self):
        return self.listener.console_name if self.listener else ""

    @property
    def dm_version(self):
        return self.listener.dm_version if self.listener else ""

    @property
    def stopped_thread(self):
        return self.formatter.stopped_thread

    @property
    def stopped_reason(self):
        return self.formatter.stopped_reason

    def _on_event(self, ev):
        self.formatter.console_name = self.xbox_name
        if ev.kind == "status" and ev.string.startswith("Connected to"):
            self.formatter.reset()
        for line in self.formatter.format(ev):
            self.msg_queue.put(line)

    def _emit(self, text, color, thread_id=None):
        self.msg_queue.put(Line(text, color, True, thread_id))

    def connect(self, ip):
        self.disconnect()
        self.xbox_ip = ip
        self.console = Console(ip, self.profile)
        self.xbox_ip = self.console.host
        self.formatter = Formatter(console_label=self.profile.name)
        self.listener = NotifyListener(self.console, self._on_event,
                                       auto_reconnect=self.auto_reconnect)
        self.listener.start()

    def disconnect(self):
        if self.listener is not None:
            self.listener.stop(unregister=True)
            self.listener = None
        if self.console is not None:
            self.console.close()
        self.formatter.clear_stop()

    def continue_execution(self):
        """Resume after a stop: continue + go, as xbWatson does."""
        tid = self.formatter.stopped_thread
        if tid is None or self.console is None:
            return
        self.formatter.clear_stop()
        try:
            n = int(tid, 0)
        except (TypeError, ValueError):
            n = None
        try:
            if n is not None:
                self.console.continue_thread(n)
            self.console.go()
            self._emit("Execution resumed.", "green")
        except (OSError, XbdmError) as e:
            self._emit(f"Failed to resume: {e}", "red")

    def force_go(self):
        """Send continue (if a thread stopped) and go regardless of local
        state, in case the console is stuck waiting."""
        tid = self.formatter.stopped_thread
        self.formatter.clear_stop()
        if self.console is None:
            return
        try:
            if tid:
                try:
                    self.console.continue_thread(int(tid, 0))
                except ValueError:
                    pass
            self.console.go()
            self._emit("GO sent.", "green")
        except (OSError, XbdmError) as e:
            self._emit(f"GO failed: {e}", "red")


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

    def __init__(self, stdscr, profile=None):
        self.scr = stdscr
        self.config = load_config()
        self.profile = get_profile(profile or "og")
        self.mode = self.MENU
        self.msg_queue = queue.Queue()
        self.conn = Session(self.msg_queue, self.profile,
                            auto_reconnect=self.config.get("auto_reconnect", True))

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
            if not isinstance(item, Line):
                continue
            self.line_count += 1
            self.log_lines.append(item)
            if self.log_file and not self.paused:
                self.log_file.write(item.log_text() + "\n")
                self.log_file.flush()
            if len(self.log_lines) > MAX_LOG_LINES:
                self.log_lines = self.log_lines[-MAX_LOG_LINES:]

    def _remember_ip(self, ip, name=""):
        remember(self.config, ip, name)
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
        local_ip, subnet = scan.local_subnet()
        if not subnet:
            self.scan_error = "Could not determine local subnet"
            self.scan_running = False
            return
        self.scan_subnet = f"{subnet}.x (from {local_ip})"
        try:
            scan.scan(subnet, port=self.profile.port,
                      found=self.scan_results.append)
        except OSError as e:
            self.scan_error = str(e)
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
                y, col, f" {ch} Scanning port {self.profile.port}{subnet_info}...",
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
                    self._addline(y, x, ln.raw, curses.color_pair(C_NORMAL), rem)
            else:
                # Match xbWatson: just the message text, colored. Timestamps
                # and thread IDs stay in the log file, not on screen.
                attr = curses.color_pair(COLOR_PAIRS.get(ln.color, C_NORMAL))
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
                    self.log_file.write(ln.log_text() + "\n")
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
        save_dir = os.path.expanduser(self.config.get("screenshot_dir") or SCREENSHOT_DIR)
        try:
            path = self.conn.console.save_screenshot(save_dir)
            self.screenshot_status = f"Saved: {os.path.basename(path)}"
        except (OSError, ValueError, XbdmError) as e:
            self.screenshot_status = f"Screenshot failed: Error: {e}"
        t = threading.Timer(5.0, self._clear_screenshot_status)
        t.daemon = True
        t.start()

    def _clear_screenshot_status(self):
        self.screenshot_status = ""

    # ── Reboot ───────────────────────────────────────────────────────────

    def _do_reboot(self):
        if not self.conn.xbox_ip:
            return
        self.msg_queue.put(Line(
            f"Reboot command sent. {self.profile.name} is restarting...",
            "yellow", is_system=True))
        # Send reboot on a one-shot connection and leave the listener running
        # so it reconnects on its own once the box comes back up.
        try:
            self.conn.console.reboot()
        except (OSError, XbdmError) as e:
            self.msg_queue.put(Line(f"Reboot failed: {e}", "red", is_system=True))

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
        for label, text in self.conn.console.probe():
            if label == "Greeting":
                self.probe_lines.append(f"Greeting:  {text}")
                continue
            parts = text.split("\n") if text else [""]
            self.probe_lines.append(f"{label:<16} \u2192 {parts[0]}")
            for extra in parts[1:]:
                self.probe_lines.append(f"{'':<16}   {extra}")

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
        try:
            self.drives = self.conn.console.drives()
        except (OSError, XbdmError):
            self.drives = []
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
        try:
            entries = self.conn.console.dirlist(self.browser_path)
            self.browser_entries = [e.as_dict() for e in entries]
        except (OSError, XbdmError):
            self.browser_entries = []
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
                    elif name.lower().endswith(self.profile.exe_ext):
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
                    elif entry["name"].lower().endswith(self.profile.exe_ext):
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
        self.msg_queue.put(Line(f"Launching {full_path}...", "yellow",
                                is_system=True))
        # Keep the listener running so it reconnects and catches the new
        # title's startup output.
        try:
            self.conn.console.launch(full_path)
        except (OSError, XbdmError) as e:
            self.msg_queue.put(Line(f"Launch failed: {e}", "red", is_system=True))
        self.mode = self.MONITOR


def main(quick_ip=None, profile=None):
    # Esc goes back a screen; do not make it wait a full second for a
    # possible escape sequence.
    os.environ.setdefault("ESCDELAY", "25")

    def run(stdscr):
        app = XbMacsonTUI(stdscr, profile)
        if quick_ip:
            app._start_monitor(quick_ip)
        app.run()

    curses.wrapper(run)
    return 0
