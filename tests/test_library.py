import os
import queue
import time
import unittest

import helpers
from fake_xbdm import FakeXbdm
from xbmacson import scan, stack
from xbmacson.client import Console
from xbmacson.notify import Formatter, NotifyListener
from xbmacson.protocol import ConnectError, XbdmError
from xbmacson.sampler import sample
from xbmacson.screenshot import decode_linear
from xbmacson.symbols import load_symbolizer


def gradient(w, h):
    return bytes(b for y in range(h) for x in range(w)
                 for b in (x * 7 & 255, y * 11 & 255, (x + y) & 255))


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.box = helpers.og_box()
        self.con = Console(self.box.target, "og", timeout=3)

    def tearDown(self):
        self.con.close()
        self.box.stop()

    def test_info(self):
        self.assertEqual(self.con.dbgname(), "fakebox")
        self.assertEqual(self.con.dmversion(), "1.00.5838.1")
        self.assertEqual(self.con.xbeinfo()["name"], "E:\\games\\halo\\default.xbe")
        mods = self.con.modules()
        self.assertEqual([m.name for m in mods], ["xboxkrnl.exe", "default.xbe"])
        self.assertTrue(mods[1].is_xbe)

    def test_one_connection_reused(self):
        for _ in range(5):
            self.con.dbgname()
        self.assertEqual(sum(1 for c in self.box.commands if c == "dbgname"), 5)

    def test_reconnect_after_drop(self):
        self.con.dbgname()
        self.box.reboot()
        time.sleep(self.box.reboot_downtime + 0.1)
        self.assertEqual(self.con.dbgname(), "fakebox")

    def test_threads_and_context(self):
        self.assertEqual(self.con.threads(), [28, 29])
        info = self.con.thread_info(28)
        self.assertEqual(info["base"], helpers.STACK_HI)
        ctx = self.con.get_context(28)
        self.assertEqual(ctx["eip"], 0x11234)
        self.assertEqual(ctx["ebp"], 0xCFFFF100)
        self.assertFalse(self.con.context_needs_suspend)
        self.assertFalse(any(c.startswith("suspend") for c in self.box.commands))

    def test_context_auto_suspend(self):
        self.box.require_suspend = True
        ctx = self.con.get_context(28)
        self.assertEqual(ctx["eip"], 0x11234)
        self.assertTrue(self.con.context_needs_suspend)
        cmds = [c.split()[0] for c in self.box.commands]
        self.assertEqual(cmds[-4:], ["getcontext", "suspend", "getcontext", "resume"])
        self.assertEqual(self.box.threads[28].suspend, 0)
        # Later reads go straight to suspend/read/resume.
        self.con.get_context(28)
        cmds = [c.split()[0] for c in self.box.commands]
        self.assertEqual(cmds[-3:], ["suspend", "getcontext", "resume"])

    def test_context_never_suspend_reports(self):
        self.box.require_suspend = True
        with self.assertRaises(XbdmError) as cm:
            self.con.get_context(28, suspend="never")
        self.assertEqual(cm.exception.code, 408)

    def test_memory(self):
        self.assertEqual(self.con.read_u32(0xCFFFF104), 0x11150)
        data = self.con.read_memory(0xCFFFF100, 8)
        self.assertEqual(len(data), 8)
        self.assertEqual(self.con.read_memory_hex(0xCFFFF104, 4), helpers.u32(0x11150))
        with self.assertRaises(XbdmError) as cm:
            self.con.read_memory(0x5, 4)
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(XbdmError):
            self.con.read_memory_hex(0x5, 4)

    def test_files(self):
        self.assertEqual(self.con.drives(), list("CDEXYZ"))
        self.box.drivelist_multiline = True
        self.assertEqual(self.con.drives(), list("CDEXYZ"))
        entries = self.con.dirlist("E:\\games\\")
        self.assertEqual([(e.name, e.is_dir) for e in entries], [("halo", True)])
        entries = self.con.dirlist("E:\\games\\halo\\")
        self.assertEqual([(e.name, e.size) for e in entries], [("default.xbe", 64)])
        self.con.send_file("E:\\x.bin", b"\x00\x01payload")
        self.assertEqual(self.con.get_file("E:\\x.bin"), b"\x00\x01payload")
        with self.assertRaises(XbdmError) as cm:
            self.con.get_file("E:\\missing")
        self.assertEqual(cm.exception.code, 402)

    def test_deploy_reboots_when_in_use(self):
        d = helpers.tmpdir()
        local = os.path.join(d, "default.xbe")
        with open(local, "wb") as f:
            f.write(b"new build")
        self.box.files_in_use.add("E:\\games\\halo\\default.xbe")
        msgs = []
        n = self.con.deploy(local, "E:\\games\\halo\\default.xbe", status=msgs.append,
                            reboot_wait=5, poll=0.1, retry_waits=(0.05,))
        self.assertEqual(n, 9)
        self.assertEqual(self.box.reboots, 1)
        self.assertEqual(self.box.files["E:\\games\\halo\\default.xbe"], b"new build")
        self.assertIn("rebooting", msgs[0])

    def test_deploy_no_reboot(self):
        d = helpers.tmpdir()
        local = os.path.join(d, "a")
        with open(local, "wb") as f:
            f.write(b"x")
        self.box.files_in_use.add("E:\\a")
        with self.assertRaises(XbdmError) as cm:
            self.con.deploy(local, "E:\\a", reboot_if_in_use=False)
        self.assertEqual(cm.exception.code, 413)
        self.assertEqual(self.box.reboots, 0)

    def test_launch_and_reboot(self):
        self.con.launch("E:\\games\\halo\\default.xbe")
        self.assertIn('magicboot title="E:\\games\\halo\\default.xbe" debug', self.box.commands)
        self.con.reboot()
        self.assertEqual(self.box.reboots, 1)

    def test_screenshot_linear(self):
        w, h = 33, 7
        rgb = gradient(w, h)
        self.box.screen = (w, h, rgb)
        shot = self.con.screenshot()
        self.assertEqual((shot.width, shot.height), (w, h))
        self.assertEqual(shot.rgb, rgb)
        path = self.con.save_screenshot(helpers.tmpdir())
        with open(path, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_probe(self):
        rows = dict(self.con.probe())
        self.assertTrue(rows["Greeting"].startswith("201"))
        self.assertEqual(rows["DBGNAME"], "fakebox")
        self.assertIn("xboxkrnl.exe", rows["MODULES"])

    def test_connect_refused(self):
        dead = Console("127.0.0.1:1", timeout=1)
        with self.assertRaises(ConnectError):
            dead.dbgname()

    def test_bad_greeting(self):
        self.box.greeting = "500 go away"
        with self.assertRaises(ConnectError):
            Console(self.box.target).dbgname()


class X360Tests(unittest.TestCase):
    def setUp(self):
        self.box = FakeXbdm("x360", name="jasper").start()
        self.con = Console(self.box.target, "x360", timeout=3)

    def tearDown(self):
        self.con.close()
        self.box.stop()

    def test_port_and_launch(self):
        self.assertEqual(Console("<console>", "x360").port, 730)
        self.con.launch("HDD:\\Devkit\\game\\default.xex")
        self.assertIn('magicboot title="HDD:\\Devkit\\game\\default.xex" '
                      'directory="HDD:\\Devkit\\game"', self.box.commands)

    def test_untiled_screenshot(self):
        w, h = 64, 40
        rgb = gradient(w, h)
        self.box.screen = (w, h, rgb)
        shot = self.con.screenshot()
        self.assertEqual(shot.rgb, rgb)

    def test_big_endian_memory_and_context(self):
        self.box.add_memory(0x70000000, bytes(16))
        self.box.write_u32(0x70000004, 0x82001234)
        self.assertEqual(self.con.read_u32(0x70000004), 0x82001234)
        self.box.add_thread(0xF9000038, context={"Iar": 0x82001000, "Lr": 0x82000F00,
                                                 "Gpr1": 0x70000000})
        ctx = self.con.get_context(0xF9000038)
        self.assertEqual(ctx.pc(self.con.profile), 0x82001000)

    def test_deploy_uses_magicboot_cold(self):
        d = helpers.tmpdir()
        local = os.path.join(d, "default.xex")
        with open(local, "wb") as f:
            f.write(b"xex")
        self.box.files_in_use.add("HDD:\\g\\default.xex")
        self.con.deploy(local, "HDD:\\g\\default.xex", reboot_wait=5, poll=0.1,
                        retry_waits=(0.05,))
        self.assertIn("magicboot cold", self.box.commands)
        self.assertEqual(self.box.files["HDD:\\g\\default.xex"], b"xex")


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.box = helpers.og_box()
        self.con = Console(self.box.target, timeout=3)
        self.q = queue.Queue()

    def tearDown(self):
        self.con.close()
        self.box.stop()

    def drain_until(self, pred, timeout=5):
        end = time.monotonic() + timeout
        seen = []
        while time.monotonic() < end:
            try:
                ev = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            seen.append(ev)
            if pred(ev):
                return seen
        self.fail(f"timed out; saw {seen}")

    def listener(self, **kw):
        return NotifyListener(self.con, self.q.put, bind_host="127.0.0.1",
                              reconnect_interval=0.05, **kw)

    def test_notifyat_survives_reboot(self):
        lst = self.listener().start()
        try:
            self.drain_until(lambda e: e.kind == "status" and e.string.startswith("Connected"))
            self.assertTrue(self.box.wait_for_channel())
            self.assertEqual(lst.console_name, "fakebox")
            self.assertIn("debugger connect", self.box.commands)
            self.box.push("debugstr thread=4 lf string=before reboot")
            self.drain_until(lambda e: e.kind == "debugstr" and e.string == "before reboot")
            self.box.reboot()
            # XBDM dials again on its own; no new notifyat is needed.
            regs_before = sum(1 for c in self.box.commands if c.startswith("notifyat"))
            deadline = time.monotonic() + 5
            while lst.sessions < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(lst.sessions, 2)
            self.assertEqual(sum(1 for c in self.box.commands if c.startswith("notifyat")),
                             regs_before)
            self.box.push("debugstr thread=4 lf string=after reboot")
            self.drain_until(lambda e: e.kind == "debugstr" and e.string == "after reboot")
        finally:
            port = lst.notify_port
            lst.stop()
            lst.join(2)
        self.assertIn(f"notifyat port={port} drop", self.box.commands)
        self.assertEqual(self.box.registrations(), [])
        self.assertIn("debugger disconnect", self.box.commands)

    def test_dedicated_channel(self):
        lst = self.listener(channel="notify").start()
        try:
            self.drain_until(lambda e: e.kind == "status" and e.string.startswith("Connected"))
            self.assertTrue(self.box.wait_for_channel())
            self.assertIn("notify reconnectport=0", self.box.commands)
            self.box.push("exception code=0xc0000005 thread=28 address=0x00011234 read=0x0 stop")
            evs = self.drain_until(lambda e: e.kind == "exception")
            f = Formatter()
            lines = [ln.text for ln in f.format(evs[-1])]
            self.assertIn("could not be read", lines[0])
            self.assertEqual(f.stopped_thread, "28")
        finally:
            lst.stop()
            lst.join(2)

    def test_gives_up(self):
        dead = Console("127.0.0.1:1", timeout=0.5)
        q = queue.Queue()
        lst = NotifyListener(dead, q.put, bind_host="127.0.0.1",
                             reconnect_timeout=0.3, reconnect_interval=0.05).start()
        lst.join(5)
        self.assertTrue(lst.gave_up)
        texts = []
        while not q.empty():
            texts.append(q.get().string)
        self.assertIn("Waiting for Xbox...", texts)
        self.assertTrue(texts[-1].startswith("Could not reach Xbox within"))


class StackTests(unittest.TestCase):
    def setUp(self):
        self.box = helpers.og_box()
        self.con = Console(self.box.target, timeout=3)

    def tearDown(self):
        self.con.close()
        self.box.stop()

    def test_walk(self):
        mods = self.con.modules()
        sym = load_symbolizer(helpers.SAMPLE_MAP, mods)
        ctx = self.con.get_context(28)
        chain, scanned = stack.walk(self.con, ctx, sym, helpers.STACK_LO, helpers.STACK_HI)
        self.assertEqual([str(f.location) for f in chain],
                         ["player_update+0x34", "game_tick+0x50", "main_loop+0x10",
                          "xboxkrnl.exe+0x100"])
        found = {str(f.location): f.verified for f in scanned}
        self.assertTrue(found["game_tick+0x88"])     # stale, after "call eax"
        self.assertTrue(found["game_tick+0x50"])     # after call rel32
        self.assertNotIn("g_state", " ".join(found))  # data pointer skipped

    def test_chain_stops_outside_stack(self):
        mods = self.con.modules()
        sym = load_symbolizer(helpers.SAMPLE_MAP, mods)
        ctx = self.con.get_context(28)
        ctx["ebp"] = 0x12345678
        chain, _ = stack.walk(self.con, ctx, sym, helpers.STACK_LO, helpers.STACK_HI,
                              scan_bytes=0)
        self.assertEqual(len(chain), 1)


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self.box = helpers.og_box()
        self.box.threads[28].pcs = [0x11210, 0x11220, 0x11110, 0x80010200]
        self.con = Console(self.box.target, timeout=3)

    def tearDown(self):
        self.con.close()
        self.box.stop()

    def test_rank(self):
        res = sample(self.con, [28], rate=400, seconds=0.4)
        self.assertGreater(len(res.samples), 8)
        sym = load_symbolizer(helpers.SAMPLE_MAP, self.con.modules())
        rows = res.rank(sym)
        self.assertEqual(rows[0][0], "player_update")
        names = [r[0] for r in rows]
        self.assertIn("game_tick", names)
        self.assertIn("[xboxkrnl.exe]", names)
        self.assertAlmostEqual(sum(r[2] for r in rows), 100.0)
        self.assertFalse(res.needs_suspend)

    def test_suspend_when_required(self):
        self.box.require_suspend = True
        res = sample(self.con, [28], rate=200, seconds=0.2)
        self.assertTrue(res.needs_suspend)
        self.assertGreater(len(res.samples), 2)
        self.assertEqual(self.box.threads[28].suspend, 0)

    def test_vanished_thread_dropped(self):
        res = sample(self.con, [28, 999], rate=200, seconds=0.2)
        self.assertIn(999, res.errors)
        self.assertTrue(all(s.thread == 28 for s in res.samples))


class ScanTests(unittest.TestCase):
    def test_check_host(self):
        with FakeXbdm(name="scanme") as box:
            self.assertEqual(scan.check_host("127.0.0.1", box.port), ("127.0.0.1", "scanme"))
            hits = scan.scan(port=box.port, hosts=["127.0.0.1"])
            self.assertEqual(hits, [("127.0.0.1", "scanme")])
        self.assertIsNone(scan.check_host("127.0.0.1", 1, timeout=0.5))


class PngTests(unittest.TestCase):
    def test_linear_pitch(self):
        fb = bytes([1, 2, 3, 0, 4, 5, 6, 0, 9, 9]) + bytes([7, 8, 9, 0, 1, 1, 1, 0, 9, 9])
        self.assertEqual(decode_linear(fb, 2, 2, 10),
                         bytes([3, 2, 1, 6, 5, 4, 9, 8, 7, 1, 1, 1]))


if __name__ == "__main__":
    unittest.main()
