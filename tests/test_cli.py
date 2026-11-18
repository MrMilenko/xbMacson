import json
import os
import time
import unittest
from unittest import mock

import helpers
from fake_xbdm import FakeXbdm
from helpers import run_cli, run_cli_async
from xbmacson import cli

_real_sleep = time.sleep


def fast_sleep(s):
    _real_sleep(min(s, 0.05))


class CliTestCase(unittest.TestCase):
    profile = "og"

    def setUp(self):
        self.box = helpers.og_box()
        self.base = ["--console", self.box.target, "--profile", self.profile,
                     "--timeout", "3", "--color", "never"]

    def tearDown(self):
        self.box.stop()

    def run_cmd(self, *args):
        return run_cli([args[0]] + self.base + list(args[1:]))

    def start_cmd(self, *args):
        return run_cli_async([args[0]] + self.base + list(args[1:]))

    def wait(self, t, result, timeout=10):
        t.join(timeout)
        self.assertFalse(t.is_alive(), "command did not finish")
        return result["code"], result["out"], result["err"]


class InfoTests(CliTestCase):
    def test_info_text(self):
        code, out, _ = self.run_cmd("info")
        self.assertEqual(code, 0)
        self.assertIn("name:     fakebox", out)
        self.assertIn("running:  E:\\games\\halo\\default.xbe", out)
        self.assertIn("threads:  2", out)
        self.assertIn("default.xbe [xbe]", out)

    def test_info_json(self):
        code, out, _ = self.run_cmd("info", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["name"], "fakebox")
        self.assertEqual(data["threads"], [28, 29])

    def test_modules_threads(self):
        code, out, _ = self.run_cmd("modules")
        self.assertEqual(code, 0)
        self.assertIn("0x80010000 0x00100000  xboxkrnl.exe", out)
        code, out, _ = self.run_cmd("threads", "--context", "--map", helpers.SAMPLE_MAP)
        self.assertEqual(code, 0)
        self.assertIn("pc=0x00011234  player_update+0x34", out)
        self.assertIn("xboxkrnl.exe+0x200", out)
        code, out, _ = self.run_cmd("threads", "--json")
        self.assertEqual([r["thread"] for r in json.loads(out)], [28, 29])


class LogTests(CliTestCase):
    def test_until(self):
        t, res = self.start_cmd("log", "--until", "ready", "--duration", "8",
                                "--bind", "127.0.0.1")
        self.assertTrue(self.box.wait_for_channel())
        self.box.push("debugstr thread=4 lf string=loading")
        self.box.push("debugstr thread=4 cr string=game ")
        self.box.push("debugstr thread=4 lf string=ready")
        self.box.push("debugstr thread=4 lf string=never seen")
        code, out, err = self.wait(t, res)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["loading", "game ready"])
        self.assertIn("Connected to 127.0.0.1 (fakebox).", err)
        self.assertEqual(self.box.registrations(), [])

    def test_until_times_out(self):
        t, res = self.start_cmd("log", "--until", "never", "--duration", "0.5",
                                "--bind", "127.0.0.1", "-q")
        code, out, err = self.wait(t, res)
        self.assertEqual(code, cli.EXIT_TIMEOUT)
        self.assertEqual(err, "")

    def test_file_and_raw(self):
        d = helpers.tmpdir()
        path = os.path.join(d, "out.log")
        t, res = self.start_cmd("log", "--raw", "--file", path, "--until", "hello",
                                "--duration", "8", "--bind", "127.0.0.1")
        self.assertTrue(self.box.wait_for_channel())
        self.box.push("debugstr thread=4 lf string=hello")
        code, out, _ = self.wait(t, res)
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("debugstr thread=4 lf string=hello"))
        with open(path) as f:
            self.assertIn("debugstr thread=4 lf string=hello", f.read())

    def test_formatted_file_has_thread(self):
        d = helpers.tmpdir()
        path = os.path.join(d, "out.log")
        t, res = self.start_cmd("log", "--file", path, "--until", "boom",
                                "--duration", "8", "--bind", "127.0.0.1")
        self.assertTrue(self.box.wait_for_channel())
        self.box.push("debugstr thread=4 lf string=boom")
        self.wait(t, res)
        with open(path) as f:
            self.assertRegex(f.read(), r"^\[\d\d:\d\d:\d\d\] \[T4\] boom$")

    def test_dedicated_channel(self):
        t, res = self.start_cmd("log", "--channel", "notify", "--until", "x",
                                "--duration", "8")
        self.assertTrue(self.box.wait_for_channel())
        self.box.push("debugstr thread=4 lf string=x")
        code, out, _ = self.wait(t, res)
        self.assertEqual((code, out.strip()), (0, "x"))


class FileTests(CliTestCase):
    def test_deploy_get_launch(self):
        d = helpers.tmpdir()
        local = os.path.join(d, "default.xbe")
        with open(local, "wb") as f:
            f.write(b"fresh")
        self.box.files_in_use.add("E:\\games\\halo\\default.xbe")
        with mock.patch("xbmacson.client.time.sleep", fast_sleep):
            code, out, err = self.run_cmd("deploy", local, "E:\\games\\halo\\default.xbe",
                                          "--reboot-wait", "5", "--launch")
        self.assertEqual(code, 0, err)
        self.assertIn("sent 5 bytes", out)
        self.assertIn("rebooting", err)
        self.assertEqual(self.box.reboots, 1)
        self.assertEqual(self.box.launched, ["E:\\games\\halo\\default.xbe"])
        back = os.path.join(d, "back.xbe")
        code, out, _ = self.run_cmd("get", "E:\\games\\halo\\default.xbe", back)
        self.assertEqual(code, 0)
        with open(back, "rb") as f:
            self.assertEqual(f.read(), b"fresh")

    def test_deploy_in_use_no_reboot(self):
        d = helpers.tmpdir()
        local = os.path.join(d, "a")
        with open(local, "wb") as f:
            f.write(b"x")
        self.box.files_in_use.add("E:\\a")
        code, _, err = self.run_cmd("deploy", local, "E:\\a", "--no-reboot")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("413", err)

    def test_deploy_missing_local(self):
        code, _, _ = self.run_cmd("deploy", "/nonexistent/file", "E:\\a")
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_launch(self):
        code, out, _ = self.run_cmd("launch", "E:\\games\\halo\\default.xbe")
        self.assertEqual(code, 0)
        self.assertIn('magicboot title="E:\\games\\halo\\default.xbe" debug', self.box.commands)

    def test_ls(self):
        code, out, _ = self.run_cmd("ls")
        self.assertEqual(out.split(), ["C:\\", "D:\\", "E:\\", "X:\\", "Y:\\", "Z:\\"])
        code, out, _ = self.run_cmd("ls", "E:\\games\\halo")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["64", "default.xbe"])
        code, _, err = self.run_cmd("ls", "E:\\nope")
        self.assertEqual(code, cli.EXIT_ERROR)

    def test_screenshot(self):
        self.box.screen = (16, 4, bytes(range(16 * 4 * 3 % 256)) * 1 + bytes(16 * 4 * 3 - 192))
        d = helpers.tmpdir()
        out_path = os.path.join(d, "shot.png")
        code, out, _ = self.run_cmd("screenshot", out_path)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), out_path)
        with open(out_path, "rb") as f:
            self.assertEqual(f.read(4), b"\x89PNG")
        code, out, _ = self.run_cmd("screenshot", d)
        self.assertTrue(os.path.basename(out.strip()).startswith("xbox_"))


class MemTests(CliTestCase):
    def test_hexdump(self):
        code, out, _ = self.run_cmd("mem", "0xCFFFF100", "8")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(),
                         "cffff100  00 f2 ff cf 50 11 01 00" + " " * 24 + "  ....P...")

    def test_out_file(self):
        d = helpers.tmpdir()
        path = os.path.join(d, "m.bin")
        code, _, _ = self.run_cmd("mem", "cffff104", "0x4", "--out", path)
        self.assertEqual(code, 0)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), helpers.u32(0x11150))

    def test_unmapped(self):
        code, _, err = self.run_cmd("mem", "0x10", "4")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("404", err)

    def test_bad_args(self):
        self.assertEqual(self.run_cmd("mem", "zz", "4")[0], cli.EXIT_USAGE)
        self.assertEqual(self.run_cmd("mem", "0x10", "0")[0], cli.EXIT_USAGE)


class CrashTests(CliTestCase):
    def test_wait_for_exception(self):
        t, res = self.start_cmd("crash", "--map", helpers.SAMPLE_MAP, "--wait", "8",
                                "--bind", "127.0.0.1", "--resume")
        self.assertTrue(self.box.wait_for_channel())
        # A first-chance exception is skipped by default.
        self.box.push("exception code=0x80000003 thread=29 address=0x00011000 first")
        self.box.threads[28].stopped = True
        self.box.push("exception stop code=0xc0000005 thread=28 address=0x00011234 "
                      "write=0x00000010")
        code, out, err = self.wait(t, res)
        self.assertEqual(code, 0, err)
        self.assertIn("The instruction at address 0x00011234 referenced memory at "
                      "address 0x00000010.  The memory could not be written.", out)
        self.assertIn("exception 0xC0000005 (Access Violation) at 0x00011234", out)
        self.assertIn("thread 28 (0x1c)", out)
        self.assertIn("eip=0x00011234", out)
        lines = out.splitlines()
        frames = [l for l in lines if l.strip().startswith("#")]
        self.assertEqual(len(frames), 4)
        self.assertIn("player_update+0x34 (player.c.obj)", frames[0])
        self.assertIn("game_tick+0x50 (game.c.obj)", frames[1])
        self.assertIn("main_loop+0x10", frames[2])
        self.assertIn("xboxkrnl.exe+0x100", frames[3])
        self.assertIn("call  game_tick+0x88", out)
        self.assertIn("continue thread=0x1c", self.box.commands)
        self.assertIn("go", self.box.commands)
        # Registration dropped, debugger left attached until --resume.
        self.assertEqual(self.box.registrations(), [])
        self.assertNotIn("debugger disconnect", self.box.commands)

    def test_json_and_thread_mode(self):
        self.box.threads[28].stopped = True
        code, out, err = self.run_cmd("crash", "--map", helpers.SAMPLE_MAP,
                                      "--thread", "28", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual([f["function"] for f in data["frames"]],
                         ["player_update", "game_tick", "main_loop", None])
        self.assertEqual(data["frames"][3]["module"], "xboxkrnl.exe")
        self.assertEqual(data["registers"]["eip"], 0x11234)
        self.assertEqual(data["stack"], {"low": helpers.STACK_LO, "high": helpers.STACK_HI})
        self.assertFalse(data["resumed"])
        self.assertIn("thread left stopped", err)

    def test_rip_event(self):
        t, res = self.start_cmd("crash", "--map", helpers.SAMPLE_MAP, "--wait", "8",
                                "--bind", "127.0.0.1", "--on", "rip,assert")
        self.assertTrue(self.box.wait_for_channel())
        self.box.push("rip stop thread=28 string=out of memory")
        code, out, _ = self.wait(t, res)
        self.assertEqual(code, 0)
        self.assertIn("The error description was: out of memory", out)

    def test_timeout(self):
        code, _, err = self.run_cmd("crash", "--wait", "0.4", "--bind", "127.0.0.1")
        self.assertEqual(code, cli.EXIT_TIMEOUT)
        self.assertIn("no crash", err)

    def test_bad_inputs(self):
        self.assertEqual(self.run_cmd("crash", "--map", "/nope.map")[0], cli.EXIT_USAGE)
        self.assertEqual(self.run_cmd("crash", "--on", "nonsense")[0], cli.EXIT_USAGE)

    def test_no_map(self):
        code, out, _ = self.run_cmd("crash", "--thread", "28")
        self.assertEqual(code, 0)
        self.assertIn("default.xbe+0x1234", out)


class ProfileTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.box.threads[28].pcs = [0x11210, 0x11220, 0x11110, 0x80010200]

    def test_text(self):
        code, out, err = self.run_cmd("profile", "--map", helpers.SAMPLE_MAP,
                                      "--thread", "28", "--rate", "300",
                                      "--seconds", "0.4")
        self.assertEqual(code, 0, err)
        self.assertIn("no suspend needed", out)
        rows = [l for l in out.splitlines() if l[:6].strip().replace(".", "").isdigit()]
        self.assertTrue(rows[0].endswith("player_update"))

    def test_json_suspend_and_samples(self):
        self.box.require_suspend = True
        d = helpers.tmpdir()
        csv = os.path.join(d, "s.csv")
        code, out, _ = self.run_cmd("profile", "--map", helpers.SAMPLE_MAP,
                                    "--thread", "28", "--rate", "200",
                                    "--seconds", "0.3", "--json", "--samples", csv)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["suspended"])
        self.assertEqual(data["top"][0]["name"], "player_update")
        with open(csv) as f:
            self.assertEqual(f.readline().strip(), "time,thread,pc,where")
            self.assertIn("player_update+0x", f.read())

    def test_all_threads_by_module(self):
        code, out, _ = self.run_cmd("profile", "--map", helpers.SAMPLE_MAP,
                                    "--rate", "200", "--seconds", "0.3", "--by", "module")
        self.assertEqual(code, 0)
        self.assertIn("xboxkrnl.exe", out)
        self.assertIn("default.xbe", out)

    def test_bad_rate(self):
        self.assertEqual(self.run_cmd("profile", "--rate", "0")[0], cli.EXIT_USAGE)


class PowerTests(CliTestCase):
    def test_reboot_wait(self):
        code, out, _ = self.run_cmd("reboot", "--wait", "5")
        self.assertEqual(code, 0)
        self.assertIn("console is back", out)
        self.assertEqual(self.box.reboots, 1)

    def test_go(self):
        self.box.threads[28].stopped = True
        code, out, _ = self.run_cmd("go", "--thread", "28")
        self.assertEqual(code, 0)
        self.assertIn("continue thread=0x1c", self.box.commands)
        self.assertIn("go", self.box.commands)

    def test_raw(self):
        code, out, _ = self.run_cmd("cmd", "dbgname")
        self.assertEqual((code, out.strip()), (0, "200- fakebox"))
        code, out, _ = self.run_cmd("cmd", "frobnicate")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("407", out)


class ExitCodeTests(unittest.TestCase):
    def test_cannot_connect(self):
        code, _, err = run_cli(["info", "--console", "127.0.0.1:1", "--timeout", "1"])
        self.assertEqual(code, cli.EXIT_CONNECT)
        self.assertIn("cannot connect", err)

    def test_no_console(self):
        code, _, err = run_cli(["info"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("no console given", err)

    def test_bad_arguments(self):
        code, _, _ = run_cli(["mem"])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_scan(self):
        with FakeXbdm(name="scanme") as box:
            code, out, _ = run_cli(["scan", "--hosts", "127.0.0.1", "--port", str(box.port)])
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["127.0.0.1", "scanme"])
        code, _, _ = run_cli(["scan", "--hosts", "127.0.0.1", "--port", "1", "--timeout", "0.5"])
        self.assertEqual(code, cli.EXIT_ERROR)

    def test_config_default_console(self):
        with FakeXbdm(name="fromcfg") as box:
            with open(os.environ["XBMACSON_CONFIG"], "w") as f:
                json.dump({"default_ip": box.target}, f)
            try:
                code, out, _ = run_cli(["cmd", "dbgname"])
            finally:
                os.remove(os.environ["XBMACSON_CONFIG"])
        self.assertEqual((code, out.strip()), (0, "200- fromcfg"))


class X360CliTests(unittest.TestCase):
    def setUp(self):
        self.box = FakeXbdm("x360", name="jasper").start()
        self.base = ["--console", self.box.target, "--profile", "x360", "--timeout", "3"]

    def tearDown(self):
        self.box.stop()

    def test_info_and_screenshot(self):
        code, out, _ = run_cli(["info"] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("type:     devkit", out)
        w, h = 40, 8
        self.box.screen = (w, h, bytes((x * 5) & 255 for x in range(w * h * 3)))
        d = helpers.tmpdir()
        code, out, _ = run_cli(["screenshot"] + self.base + [os.path.join(d, "s.png")])
        self.assertEqual(code, 0)

    def test_log_default_channel_is_dedicated(self):
        t, res = run_cli_async(["log"] + self.base + ["--until", "hi", "--duration", "8"])
        self.assertTrue(self.box.wait_for_channel())
        self.box.push("debugstr thread=0xf9000038 lf string=hi")
        t.join(10)
        self.assertEqual(res["code"], 0)
        self.assertIn("notify reconnectport=0", self.box.commands)


if __name__ == "__main__":
    unittest.main()
