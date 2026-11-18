"""Shared test setup. Every test talks to a FakeXbdm on 127.0.0.1 and uses
its own config file, so nothing reaches a real console or the user's
~/.xbmacson.json."""

import io
import os
import struct
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="xbmacson-test-")
os.environ["XBMACSON_CONFIG"] = os.path.join(_tmp, "config.json")
os.environ.pop("XBMACSON_CONSOLE", None)
os.environ.pop("XBMACSON_PROFILE", None)
# Notification listeners bind loopback only during tests.
os.environ["XBMACSON_BIND"] = "127.0.0.1"

from fake_xbdm import FakeXbdm  # noqa: E402
from xbmacson import cli  # noqa: E402

SAMPLE_MAP = os.path.join(HERE, "data", "sample.map")

STACK_LO = 0xCFFFE000
STACK_HI = 0xD0000000
CRASH_TID = 28


def tmpdir():
    return tempfile.mkdtemp(prefix="xbmacson-test-", dir=_tmp)


def og_box(**kw):
    """An original Xbox with a title whose thread 28 faults in
    player_update, called from game_tick, called from main_loop, called
    from the kernel."""
    box = FakeXbdm("og", **kw)
    box.add_module("xboxkrnl.exe", 0x80010000, 0x100000)
    box.add_module("default.xbe", 0x10000, 0x5000, "xbe")
    code = bytearray(0x1000)
    # Call instructions before each return address, for the scan check.
    code[0x150 - 5] = 0xE8        # call rel32 before game_tick+0x50
    code[0x010 - 5] = 0xE8        # call rel32 before main_loop+0x10
    code[0x188 - 2:0x188] = b"\xff\xd0"  # call eax before game_tick+0x88
    box.add_memory(0x10000, bytearray(0x1000) + code + bytearray(0x3000))
    box.add_memory(STACK_LO, bytearray(STACK_HI - STACK_LO))
    box.add_memory(0x80010000, bytearray(0x1000))
    # ebp chain
    box.write_u32(0xCFFFF100, 0xCFFFF200)
    box.write_u32(0xCFFFF104, 0x00011150)   # game_tick+0x50
    box.write_u32(0xCFFFF200, 0xCFFFF300)
    box.write_u32(0xCFFFF204, 0x00011010)   # main_loop+0x10
    box.write_u32(0xCFFFF300, 0x00000000)
    box.write_u32(0xCFFFF304, 0x80010100)   # xboxkrnl.exe+0x100
    # A stale value the chain does not reach, preceded by "call eax".
    box.write_u32(0xCFFFF0F8, 0x00011188)
    # A data pointer that must not show up as a frame.
    box.write_u32(0xCFFFF0FC, 0x00014010)
    box.add_thread(CRASH_TID, start=0x11000, base=STACK_HI, limit=STACK_LO,
                   context={"Ebp": 0xCFFFF100, "Esp": 0xCFFFF0F0,
                            "Eip": 0x00011234, "EFlags": 0x202,
                            "Eax": 1, "Ebx": 2, "Ecx": 3, "Edx": 4,
                            "Esi": 5, "Edi": 6})
    box.add_thread(29, start=0x11300, context={"Eip": 0x80010200,
                                               "Esp": 0xCFFFE800, "Ebp": 0})
    box.files["E:\\games\\halo\\default.xbe"] = b"XBEH" + bytes(60)
    box.dirs["E:\\games\\halo\\"] = None
    return box.start()


def run_cli(argv):
    """Run the CLI in-process. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def run_cli_async(argv):
    """Start the CLI in a thread; returns (thread, result dict)."""
    result = {}

    def go():
        result["code"], result["out"], result["err"] = run_cli(argv)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, result


def u32(v):
    return struct.pack("<I", v)
