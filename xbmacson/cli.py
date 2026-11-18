"""Command line front end.

Every subcommand runs without prompts and exits with one of the codes below,
so it can sit in a build script or a test loop.
"""

import argparse
import json
import os
import queue
import re
import sys
import time

from . import __version__
from . import scan as _scan
from . import stack as _stack
from .client import Console, parse_address
from .config import load_config
from .notify import Formatter, NotifyListener, exception_name
from .profiles import PROFILES, get_profile
from .protocol import ConnectError, XbdmError
from .sampler import sample
from .symbols import load_symbolizer

EXIT_OK = 0
EXIT_ERROR = 1        # the console refused a command, or the operation failed
EXIT_USAGE = 2        # bad arguments or unreadable input file
EXIT_CONNECT = 3      # could not reach the console
EXIT_TIMEOUT = 4      # waited and nothing came (log --until, crash)
EXIT_INTERRUPTED = 130

COMMANDS = ("info", "log", "deploy", "get", "launch", "screenshot", "threads",
            "mem", "modules", "ls", "crash", "profile", "reboot", "go", "scan",
            "cmd", "tui")

ANSI = {"red": "31", "green": "32", "yellow": "33", "blue": "34",
        "magenta": "35", "cyan": "36", "normal": None}


class UsageError(Exception):
    pass


# -- helpers --------------------------------------------------------------

def _out(args):
    return getattr(args, "_stdout", None) or sys.stdout


def _err(args):
    return getattr(args, "_stderr", None) or sys.stderr


def say(args, text=""):
    print(text, file=_out(args))


def note(args, text):
    print(text, file=_err(args))


def emit_json(args, obj):
    say(args, json.dumps(obj, indent=2, sort_keys=True))


def opt(args, name, default=None):
    return getattr(args, name, default)


def resolve_console(args, cfg):
    target = (opt(args, "console") or os.environ.get("XBMACSON_CONSOLE")
              or cfg.get("default_ip") or "")
    if not target:
        raise UsageError("no console given: use --console <console> or set "
                         "default_ip in ~/.xbmacson.json")
    profile = get_profile(opt(args, "profile") or os.environ.get("XBMACSON_PROFILE") or "og")
    return Console(target, profile, timeout=float(opt(args, "timeout") or 5.0),
                   port=opt(args, "port"))


def colorize(args, text, color):
    use = opt(args, "color", "auto")
    stream = _out(args)
    if use == "never" or (use == "auto" and not getattr(stream, "isatty", lambda: False)()):
        return text
    code = ANSI.get(color)
    return f"\x1b[{code}m{text}\x1b[0m" if code else text


def parse_int(text):
    try:
        return int(text, 0)
    except ValueError:
        raise UsageError(f"not a number: {text!r}")


def parse_addr(text):
    try:
        return parse_address(text)
    except ValueError as e:
        raise UsageError(str(e))


def load_map(args, modules=None):
    path = opt(args, "map")
    if path and not os.path.isfile(path):
        raise UsageError(f"map file not found: {path}")
    base = opt(args, "base")
    try:
        return load_symbolizer(path, modules, parse_addr(base) if base else None)
    except ValueError as e:
        raise UsageError(f"{path}: {e}")


def hexdump(data, addr):
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{addr + off:08x}  {hx:<47}  {asc}")
    return lines


# -- commands -------------------------------------------------------------

def cmd_info(args, con):
    info = {"console": con.host, "port": con.port, "profile": con.profile.key}
    for key, fn in (("name", con.dbgname), ("xbdm", con.dmversion)):
        try:
            info[key] = fn()
        except XbdmError as e:
            info[key] = None
            info.setdefault("errors", {})[key] = str(e)
    if con.profile.key == "x360":
        try:
            info["type"] = con.consoletype()
        except XbdmError:
            pass
    try:
        info["running"] = con.xbeinfo("running")
    except XbdmError as e:
        info["running"] = None
    try:
        mods = con.modules()
        info["modules"] = [m.as_dict() for m in mods]
    except XbdmError:
        mods = []
        info["modules"] = []
    try:
        info["threads"] = con.threads()
    except XbdmError:
        info["threads"] = []
    if opt(args, "json"):
        emit_json(args, info)
        return EXIT_OK
    say(args, f"name:     {info.get('name') or '?'}")
    say(args, f"xbdm:     {info.get('xbdm') or '?'}")
    if "type" in info:
        say(args, f"type:     {info['type']}")
    run = info.get("running") or {}
    say(args, f"running:  {run.get('name', '?')}")
    say(args, f"threads:  {len(info['threads'])}")
    say(args, "modules:")
    for m in mods:
        flags = (" [" + ",".join(m.flags) + "]") if m.flags else ""
        say(args, f"  0x{m.base:08x} 0x{m.size:08x}  {m.name}{flags}")
    return EXIT_OK


def _listener_queue(con, args, cfg):
    q = queue.Queue()
    listener = NotifyListener(
        con, q.put, channel=opt(args, "channel"),
        bind_host=opt(args, "bind"),
        auto_reconnect=cfg.get("auto_reconnect", True),
        reconnect_timeout=float(opt(args, "reconnect_timeout") or 60.0))
    return listener, q


def cmd_log(args, con, cfg):
    fmt = Formatter(console_label=con.profile.name)
    listener, q = _listener_queue(con, args, cfg)
    until = re.compile(args.until) if args.until else None
    logf = open(args.file, "a") if args.file else None
    duration = args.duration
    deadline = time.monotonic() + duration if duration else None
    matched = False
    listener.start()
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                ev = q.get(timeout=0.2)
            except queue.Empty:
                if listener.gave_up and q.empty():
                    return EXIT_CONNECT
                continue
            if ev.kind == "status":
                fmt.console_name = listener.console_name or fmt.console_name
            if args.raw:
                if ev.kind == "status":
                    note(args, ev.string)
                    continue
                text = f"{ev.ts} {ev.raw}"
                say(args, text)
                if logf:
                    logf.write(text + "\n")
                    logf.flush()
                hay = ev.raw
            else:
                lines = fmt.format(ev)
                hay = "\n".join(ln.text for ln in lines)
                for ln in lines:
                    if ev.kind == "status":
                        if not args.quiet:
                            note(args, ln.text)
                        continue
                    text = ln.log_text() if args.timestamps else ln.text
                    say(args, colorize(args, text, ln.color))
                    if logf:
                        logf.write(ln.log_text() + "\n")
                        logf.flush()
            if until and ev.kind != "status" and until.search(hay):
                matched = True
                break
    finally:
        listener.stop(unregister=True)
        if logf:
            logf.close()
    if until and not matched:
        return EXIT_TIMEOUT
    return EXIT_OK


def cmd_deploy(args, con):
    if not os.path.isfile(args.local):
        raise UsageError(f"no such file: {args.local}")
    n = con.deploy(args.local, args.remote,
                   reboot_if_in_use=not args.no_reboot,
                   status=lambda m: note(args, m),
                   reboot_wait=float(args.reboot_wait))
    say(args, f"sent {n} bytes to {args.remote}")
    if args.launch:
        con.launch(args.remote)
        say(args, f"launched {args.remote}")
    return EXIT_OK


def cmd_get(args, con):
    data = con.get_file(args.remote)
    with open(args.local, "wb") as f:
        f.write(data)
    say(args, f"received {len(data)} bytes to {args.local}")
    return EXIT_OK


def cmd_launch(args, con):
    con.launch(args.path)
    say(args, f"launched {args.path}")
    return EXIT_OK


def cmd_screenshot(args, con, cfg):
    out = args.out or os.path.expanduser(cfg.get("screenshot_dir") or ".")
    path = con.save_screenshot(out)
    say(args, path)
    return EXIT_OK


def cmd_threads(args, con):
    tids = con.threads()
    sym = load_map(args, con.modules()) if args.context or opt(args, "map") else None
    rows = []
    for tid in tids:
        row = {"thread": tid}
        try:
            row.update(con.thread_info(tid))
        except XbdmError as e:
            row["error"] = str(e)
        if args.context:
            try:
                ctx = con.get_context(tid, suspend=args.suspend, parts="control")
                pc = ctx.get(con.profile.pc_reg)
                row["pc"] = pc
                if pc is not None and sym is not None:
                    row["where"] = str(sym.locate(pc))
            except XbdmError as e:
                row["context_error"] = str(e)
        rows.append(row)
    if opt(args, "json"):
        emit_json(args, rows)
        return EXIT_OK
    for r in rows:
        line = f"{r['thread']:>6}  0x{r['thread']:08x}"
        if "start" in r and isinstance(r["start"], int):
            line += f"  start=0x{r['start']:08x}"
        if "priority" in r:
            line += f"  pri={r['priority']}"
        if "suspend" in r:
            line += f"  suspend={r['suspend']}"
        if "pc" in r and r["pc"] is not None:
            line += f"  pc=0x{r['pc']:08x}"
        if "where" in r:
            line += f"  {r['where']}"
        if "context_error" in r:
            line += f"  ({r['context_error']})"
        say(args, line)
    return EXIT_OK


def cmd_mem(args, con):
    addr = parse_addr(args.addr)
    length = parse_int(args.length)
    if length <= 0 or length > 16 * 1024 * 1024:
        raise UsageError("length must be between 1 and 16 MB")
    data = bytearray()
    chunk = 0x10000
    for off in range(0, length, chunk):
        data += con.read_memory(addr + off, min(chunk, length - off))
    if args.out:
        with open(args.out, "wb") as f:
            f.write(data)
        say(args, f"wrote {len(data)} bytes to {args.out}")
    elif args.format == "raw":
        stream = _out(args)
        buf = getattr(stream, "buffer", None)
        (buf or stream).write(bytes(data) if buf else bytes(data).hex())
    else:
        for line in hexdump(bytes(data), addr):
            say(args, line)
    return EXIT_OK


def cmd_modules(args, con):
    mods = con.modules()
    if opt(args, "json"):
        emit_json(args, [m.as_dict() for m in mods])
        return EXIT_OK
    for m in mods:
        flags = (" [" + ",".join(m.flags) + "]") if m.flags else ""
        say(args, f"0x{m.base:08x} 0x{m.size:08x}  {m.name}{flags}")
    return EXIT_OK


def cmd_ls(args, con):
    if not args.path:
        drives = con.drives()
        if opt(args, "json"):
            emit_json(args, drives)
        else:
            for d in drives:
                say(args, f"{d}:\\")
        return EXIT_OK
    path = args.path
    if not path.endswith("\\"):
        path += "\\"
    entries = con.dirlist(path)
    if opt(args, "json"):
        emit_json(args, [e.as_dict() for e in entries])
        return EXIT_OK
    for e in entries:
        size = "<DIR>" if e.is_dir else str(e.size)
        say(args, f"{size:>12}  {e.name}")
    return EXIT_OK


CRASH_KINDS = ("exception", "rip", "assert", "break")


def _wait_for_stop(args, con, cfg, kinds):
    """Run a listener until a matching stop event. Returns (event, lines)."""
    fmt = Formatter(console_label=con.profile.name)
    listener, q = _listener_queue(con, args, cfg)
    deadline = time.monotonic() + args.wait if args.wait else None
    listener.start()
    hit = None
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                ev = q.get(timeout=0.2)
            except queue.Empty:
                if listener.gave_up and q.empty():
                    raise ConnectError("notification channel could not be opened")
                continue
            fmt.console_name = listener.console_name
            lines = fmt.format(ev)
            if args.echo:
                for ln in lines:
                    note(args, ln.text)
            if ev.kind not in kinds:
                continue
            if ev.kind == "exception":
                first = ev.kv.get("first") is True
                if first and not args.first_chance:
                    continue
            elif not ev.stop:
                continue
            hit = (ev, lines)
            break
    finally:
        # Keep the debugger attached: the thread is stopped and we still
        # want to read it. Only drop the dial-back registration.
        listener.stop(unregister=True, disconnect=False)
    return hit


def _context_report(con, tid, args):
    ctx = con.get_context(tid, suspend=args.suspend, parts="control int")
    stack_lo = stack_hi = None
    try:
        info = con.thread_info(tid)
        base, limit = info.get("base"), info.get("limit")
        if isinstance(base, int) and isinstance(limit, int) and limit < base:
            stack_lo, stack_hi = limit, base
    except XbdmError:
        pass
    return ctx, stack_lo, stack_hi


def cmd_crash(args, con, cfg):
    kinds = tuple(k.strip() for k in args.on.split(",") if k.strip())
    for k in kinds:
        if k not in CRASH_KINDS:
            raise UsageError(f"--on: unknown event {k!r} (use {','.join(CRASH_KINDS)})")
    if opt(args, "map") and not os.path.isfile(args.map):
        raise UsageError(f"map file not found: {args.map}")
    event = None
    summary = []
    if args.thread is not None:
        tid = parse_int(args.thread)
    else:
        note(args, "waiting for a crash ... (Ctrl+C to stop)")
        hit = _wait_for_stop(args, con, cfg, kinds)
        if hit is None:
            note(args, "no crash before the timeout")
            return EXIT_TIMEOUT
        event, lines = hit
        summary = [ln.text for ln in lines if not ln.text.startswith("STOPPED:")]
        tid = event.thread_int
        if tid is None:
            raise XbdmError(400, f"event has no thread: {event.raw}")

    modules = []
    try:
        modules = con.modules()
    except XbdmError:
        pass
    sym = load_map(args, modules)
    ctx, lo, hi = _context_report(con, tid, args)
    chain, scan = _stack.walk(con, ctx, sym, lo, hi, scan_bytes=args.scan,
                              max_frames=args.frames)

    if args.resume:
        con.continue_thread(tid)
        con.go()

    report = {
        "thread": tid,
        "event": event.raw if event else None,
        "summary": summary,
        "registers": dict(ctx),
        "stack": {"low": lo, "high": hi},
        "frames": [f.as_dict() for f in chain],
        "scan": [f.as_dict() for f in scan],
        "suspended_for_context": bool(con.context_needs_suspend),
        "resumed": bool(args.resume),
    }
    if event is not None and event.kind == "exception":
        code = event.get_int("code", 0)
        report["exception"] = {"code": code, "name": exception_name(code),
                               "address": event.get_int("address", 0),
                               "first_chance": event.kv.get("first") is True}
    if not args.resume:
        note(args, "thread left stopped; run 'xbmacson go' or pass --resume to continue")
    if opt(args, "json"):
        emit_json(args, report)
        return EXIT_OK

    for text in summary:
        say(args, text)
    if "exception" in report:
        e = report["exception"]
        say(args, f"exception 0x{e['code']:08X} ({e['name']}) at 0x{e['address']:08X}")
    say(args, f"thread {tid} (0x{tid:x})")
    say(args, "")
    say(args, "registers:")
    order = [r for r in ("eip", "esp", "ebp", "eflags", "eax", "ebx", "ecx",
                         "edx", "esi", "edi", "iar", "lr", "ctr", "msr", "cr")
             if r in ctx]
    order += sorted(k for k in ctx if k not in order)
    for i in range(0, len(order), 4):
        say(args, "  " + "  ".join(f"{k}=0x{ctx[k]:08x}" for k in order[i:i + 4]))
    say(args, "")
    method = "ebp chain" if con.profile.arch == "x86" else "back chain"
    say(args, f"call stack ({method}):")
    for f in chain:
        where = str(f.location) if f.location else "?"
        obj = f" ({f.location.symbol.obj})" if f.location and f.location.symbol and f.location.symbol.obj else ""
        say(args, f"  #{f.index:<2} 0x{f.pc:08x}  {where}{obj}")
    if scan:
        say(args, "")
        say(args, f"stack scan ({args.scan} bytes from sp; 'call' = follows a call instruction):")
        for f in scan:
            mark = "call" if f.verified else ("    " if f.verified is False else "")
            say(args, f"  [0x{f.frame:08x}] 0x{f.pc:08x}  {mark}  {f.location}")
    if con.context_needs_suspend:
        say(args, "")
        say(args, "note: XBDM needed the thread suspended to read its context")
    return EXIT_OK


def cmd_profile(args, con):
    if opt(args, "map") and not os.path.isfile(args.map):
        raise UsageError(f"map file not found: {args.map}")
    if args.rate <= 0 or args.seconds <= 0:
        raise UsageError("--rate and --seconds must be positive")
    threads = [parse_int(t) for t in args.thread] if args.thread else con.threads()
    if not threads:
        raise XbdmError(405, "no threads to sample")
    modules = []
    try:
        modules = con.modules()
    except XbdmError:
        pass
    sym = load_map(args, modules)
    note(args, f"sampling {len(threads)} thread(s) at {args.rate:g}/s for {args.seconds:g}s ...")
    res = sample(con, threads, rate=args.rate, seconds=args.seconds,
                 suspend=args.suspend)
    rows = res.rank(sym, by=args.by)[:args.top]
    if args.samples:
        with open(args.samples, "w") as f:
            f.write("time,thread,pc,where\n")
            for s in res.samples:
                f.write(f"{s.t:.6f},{s.thread},0x{s.pc:08x},{sym.locate(s.pc)}\n")
    summary = {
        "samples": len(res.samples),
        "seconds": round(res.elapsed, 3),
        "threads": threads,
        "requested_rate": args.rate,
        "achieved_rate": round(res.achieved_rate, 2),
        "mean_read_ms": round(res.mean_latency_ms, 3),
        "suspended": bool(res.needs_suspend),
        "errors": {str(k): v for k, v in res.errors.items()},
        "top": [{"name": n, "count": c, "percent": round(p, 2)} for n, c, p in rows],
    }
    if opt(args, "json"):
        emit_json(args, summary)
        return EXIT_OK if res.samples else EXIT_ERROR
    say(args, f"{len(res.samples)} samples in {res.elapsed:.2f}s "
              f"({res.achieved_rate:.1f}/s per thread, asked {args.rate:g}); "
              f"mean read {res.mean_latency_ms:.2f} ms; "
              f"{'suspend/resume per sample' if res.needs_suspend else 'no suspend needed'}")
    say(args, "")
    say(args, f"{'%':>6} {'count':>7}  {args.by}")
    for name, count, pct in rows:
        say(args, f"{pct:6.2f} {count:7d}  {name}")
    for tid, err in res.errors.items():
        note(args, f"thread {tid}: {err}")
    return EXIT_OK if res.samples else EXIT_ERROR


def cmd_reboot(args, con):
    con.reboot(warm=args.warm, wait=args.stop_at_start)
    say(args, "reboot sent")
    if args.wait:
        if not con.wait_until_up(timeout=float(args.wait)):
            note(args, "console did not come back in time")
            return EXIT_TIMEOUT
        say(args, "console is back")
    return EXIT_OK


def cmd_go(args, con):
    if args.thread is not None:
        con.continue_thread(parse_int(args.thread), exception=args.exception)
    con.go()
    say(args, "go sent")
    return EXIT_OK


def cmd_scan(args, cfg):
    port = opt(args, "port") or get_profile(opt(args, "profile") or "og").port
    hosts = None
    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    try:
        found = _scan.scan(args.subnet, port=port, hosts=hosts,
                           timeout=float(opt(args, "timeout") or 1.5),
                           found=lambda r: say(args, f"{r[0]:<18} {r[1]}"))
    except OSError as e:
        raise UsageError(str(e))
    if opt(args, "json"):
        emit_json(args, [{"console": h, "name": n} for h, n in found])
    return EXIT_OK if found else EXIT_ERROR


def cmd_raw(args, con):
    r = con.command(args.raw, check=False)
    say(args, f"{r.code}- {r.text}")
    for line in r.lines:
        say(args, line)
    return EXIT_OK if r.ok else EXIT_ERROR


# -- parser ---------------------------------------------------------------

def _common():
    p = argparse.ArgumentParser(add_help=False)
    S = argparse.SUPPRESS
    p.add_argument("-c", "--console", default=S, metavar="HOST[:PORT]",
                   help="console to talk to (default: $XBMACSON_CONSOLE or "
                        "default_ip from ~/.xbmacson.json)")
    p.add_argument("-p", "--profile", default=S, choices=sorted(
        [k for k in PROFILES] + ["xbox", "360"]),
        help="og (original Xbox, port 731) or x360 (Xbox 360, port 730)")
    p.add_argument("--port", type=int, default=S, help="override the XBDM port")
    p.add_argument("--timeout", type=float, default=S, help="socket timeout in seconds (5)")
    p.add_argument("--json", action="store_true", default=S, help="JSON output where supported")
    p.add_argument("--color", choices=("auto", "always", "never"), default=S)
    return p


def build_parser():
    common = _common()
    ap = argparse.ArgumentParser(
        prog="xbmacson", parents=[common],
        description="Debug monitor and scriptable XBDM client for the original "
                    "Xbox and the Xbox 360. Run with no command for the TUI.",
        epilog="exit codes: 0 ok, 1 console refused or failed, 2 usage, "
               "3 cannot connect, 4 timed out, 130 interrupted")
    ap.add_argument("--version", action="version", version=f"xbmacson {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="command")

    def add(name, help_):
        return sub.add_parser(name, parents=[common], help=help_, description=help_)

    add("info", "name, XBDM version, running title, modules, threads")

    p = add("log", "print debug output as it arrives")
    p.add_argument("--file", help="also append to this log file (with timestamps and threads)")
    p.add_argument("--raw", action="store_true", help="print the wire lines instead of formatted text")
    p.add_argument("--timestamps", action="store_true", help="prefix lines with time and thread")
    p.add_argument("--duration", type=float, default=0, help="stop after this many seconds")
    p.add_argument("--until", metavar="REGEX", help="stop with 0 when a line matches; 4 if --duration ends first")
    p.add_argument("--channel", choices=("notifyat", "notify"), default=None,
                   help="notifyat (console dials us, survives reboots) or notify (one connection)")
    p.add_argument("--bind", help="local address for the notifyat listener (default all)")
    p.add_argument("--reconnect-timeout", type=float, default=60.0)
    p.add_argument("-q", "--quiet", action="store_true", help="no connection status on stderr")

    p = add("deploy", "upload a file; reboot first if the running title holds it")
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--no-reboot", action="store_true", help="fail instead of rebooting when the file is in use")
    p.add_argument("--reboot-wait", type=float, default=90.0, help="seconds to wait for the console after a reboot")
    p.add_argument("--launch", action="store_true", help="launch the file after upload")

    p = add("get", "download a file from the console")
    p.add_argument("remote")
    p.add_argument("local")

    p = add("launch", "boot a title (XBE or XEX path on the console)")
    p.add_argument("path")

    p = add("screenshot", "save the framebuffer as PNG")
    p.add_argument("out", nargs="?", help="PNG path or directory (default: screenshot_dir)")

    p = add("threads", "list threads")
    p.add_argument("--context", action="store_true", help="also read each thread's pc")
    p.add_argument("--map", help="map file to name the pc")
    p.add_argument("--base", help="actual load address if it differs from the map's")
    p.add_argument("--suspend", choices=("auto", "always", "never"), default="auto")

    p = add("mem", "read memory: mem <addr> <len>")
    p.add_argument("addr", help="address, hex (0x prefix optional)")
    p.add_argument("length", help="byte count (decimal or 0x hex)")
    p.add_argument("--out", help="write the bytes to a file")
    p.add_argument("--format", choices=("hex", "raw"), default="hex")

    add("modules", "list loaded modules")

    p = add("ls", "list drives, or a directory")
    p.add_argument("path", nargs="?")

    p = add("crash", "wait for a crash, then print registers and a symbolized stack")
    p.add_argument("--map", help="map file (lld-link /map or nm output)")
    p.add_argument("--base", help="actual load address if it differs from the map's")
    p.add_argument("--thread", help="inspect this stopped thread now instead of waiting")
    p.add_argument("--wait", type=float, default=0, help="give up after this many seconds (default: forever)")
    p.add_argument("--on", default="exception", help="events that count, comma separated: exception,rip,assert,break")
    p.add_argument("--first-chance", action="store_true", help="also trigger on first-chance exceptions")
    p.add_argument("--scan", type=lambda s: int(s, 0), default=4096, help="bytes of stack to scan (0 to skip)")
    p.add_argument("--frames", type=int, default=64)
    p.add_argument("--suspend", choices=("auto", "always", "never"), default="auto")
    p.add_argument("--resume", action="store_true", help="continue the thread and send go afterwards")
    p.add_argument("--echo", action="store_true", help="print debug output on stderr while waiting")
    p.add_argument("--channel", choices=("notifyat", "notify"), default=None)
    p.add_argument("--bind")
    p.add_argument("--reconnect-timeout", type=float, default=60.0)

    p = add("profile", "sample thread pcs and rank functions")
    p.add_argument("--map", help="map file (lld-link /map or nm output)")
    p.add_argument("--base", help="actual load address if it differs from the map's")
    p.add_argument("--thread", action="append", help="thread id (repeat for more; default all)")
    p.add_argument("--rate", type=float, default=100.0, help="samples per second per thread (100)")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--by", choices=("function", "module", "address"), default="function")
    p.add_argument("--suspend", choices=("auto", "always", "never"), default="auto",
                   help="auto: suspend only if XBDM refuses a running thread")
    p.add_argument("--samples", help="write every sample to this CSV file")

    p = add("reboot", "reboot the console")
    p.add_argument("--warm", action="store_true", help="warm reboot (title only)")
    p.add_argument("--stop-at-start", action="store_true", help="ask XBDM to wait at boot (reboot wait)")
    p.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                   help="wait up to this long for XBDM to answer again")

    p = add("go", "resume after a stop")
    p.add_argument("--thread", help="continue this thread first")
    p.add_argument("--exception", action="store_true", help="pass the exception to the thread")

    p = add("scan", "find consoles on the local network")
    p.add_argument("--subnet", help="first three octets, for example 10.0.0")
    p.add_argument("--hosts", help="comma separated hosts to try instead of a subnet")

    p = add("cmd", "send one raw XBDM command and print the reply")
    p.add_argument("raw", metavar="command")

    p = add("tui", "start the terminal UI")
    p.add_argument("host", nargs="?")
    return ap


def main(argv=None, stdout=None, stderr=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    try:
        if stderr is not None or stdout is not None:
            import contextlib
            with contextlib.redirect_stderr(stderr or sys.stderr), \
                    contextlib.redirect_stdout(stdout or sys.stdout):
                args = ap.parse_args(argv)
        else:
            args = ap.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    args._stdout = stdout
    args._stderr = stderr
    cfg = load_config()

    if args.command in (None, "tui"):
        from .tui import main as tui_main
        host = opt(args, "host") or opt(args, "console")
        return tui_main(host, profile=opt(args, "profile"))

    con = None
    try:
        if args.command == "scan":
            return cmd_scan(args, cfg)
        con = resolve_console(args, cfg)
        c = args.command
        if c == "info":
            return cmd_info(args, con)
        if c == "log":
            return cmd_log(args, con, cfg)
        if c == "deploy":
            return cmd_deploy(args, con)
        if c == "get":
            return cmd_get(args, con)
        if c == "launch":
            return cmd_launch(args, con)
        if c == "screenshot":
            return cmd_screenshot(args, con, cfg)
        if c == "threads":
            return cmd_threads(args, con)
        if c == "mem":
            return cmd_mem(args, con)
        if c == "modules":
            return cmd_modules(args, con)
        if c == "ls":
            return cmd_ls(args, con)
        if c == "crash":
            return cmd_crash(args, con, cfg)
        if c == "profile":
            return cmd_profile(args, con)
        if c == "reboot":
            return cmd_reboot(args, con)
        if c == "go":
            return cmd_go(args, con)
        if c == "cmd":
            return cmd_raw(args, con)
        raise UsageError(f"unknown command {c}")
    except UsageError as e:
        note(args, f"xbmacson: {e}")
        return EXIT_USAGE
    except ConnectError as e:
        note(args, f"xbmacson: {e}")
        return EXIT_CONNECT
    except XbdmError as e:
        note(args, f"xbmacson: {e}")
        return EXIT_ERROR
    except (OSError, ValueError) as e:
        note(args, f"xbmacson: {e}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        note(args, "interrupted")
        return EXIT_INTERRUPTED
    finally:
        if con is not None:
            con.close()


if __name__ == "__main__":
    sys.exit(main())
