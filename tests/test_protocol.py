import unittest

import helpers  # noqa: F401  (sets up an isolated environment)
from xbmacson.notify import Formatter, classify_line, parse_notification
from xbmacson.protocol import Reply, XbdmError, parse_kv, parse_status, to_int


class ParseTests(unittest.TestCase):
    def test_status(self):
        self.assertEqual(parse_status("200- OK"), (200, "OK"))
        self.assertEqual(parse_status("202- multiline response follows"),
                         (202, "multiline response follows"))
        self.assertEqual(parse_status("408- not stopped"), (408, "not stopped"))
        self.assertEqual(parse_status("201-connected"), (201, "connected"))
        self.assertEqual(parse_status("hello"), (None, "hello"))

    def test_kv(self):
        kv = parse_kv('name="xboxkrnl.exe" base=0x80010000 size=0x1000 tls xbe')
        self.assertEqual(kv["name"], "xboxkrnl.exe")
        self.assertEqual(to_int(kv["base"]), 0x80010000)
        self.assertIs(kv["xbe"], True)
        self.assertIs(kv["tls"], True)
        kv = parse_kv("Ebp=0x10 Esp=0x20 EFlags=0x202")
        self.assertEqual(to_int(kv["eflags"]), 0x202)
        kv = parse_kv('name="E:\\with space\\a.xbe" sizelo=0x10')
        self.assertEqual(kv["name"], "E:\\with space\\a.xbe")

    def test_to_int(self):
        self.assertEqual(to_int("0x1f"), 31)
        self.assertEqual(to_int("31"), 31)
        self.assertEqual(to_int("ff"), 255)
        self.assertIsNone(to_int(True))

    def test_reply(self):
        r = Reply(202, "multiline response follows", ["a", "b"])
        self.assertTrue(r.ok)
        self.assertEqual(r.body(), "a\nb")
        with self.assertRaises(XbdmError) as cm:
            Reply(413, "file cannot be created").raise_for_error("sendfile x")
        self.assertEqual(cm.exception.code, 413)


class NotificationTests(unittest.TestCase):
    def fmt(self, *lines, name="box"):
        f = Formatter(console_name=name)
        out = []
        for line in lines:
            out += [ln.text for ln in f.format(parse_notification(line))]
        return f, out

    def test_debugstr_join(self):
        f, out = self.fmt("debugstr thread=4 cr string=Hello, ",
                          "debugstr thread=5 lf string=other",
                          "debugstr thread=4 lf string=world")
        self.assertEqual(out, ["other", "Hello, world"])

    def test_debugstr_keeps_equals(self):
        _, out = self.fmt("debugstr thread=4 lf string=a=b string=c")
        self.assertEqual(out, ["a=b string=c"])

    def test_debugstr_no_terminator_field(self):
        _, out = self.fmt("debugstr thread=4 string=plain")
        self.assertEqual(out, ["plain"])

    def test_access_violation(self):
        _, out = self.fmt("exception code=0xc0000005 thread=28 "
                          "address=0x00011234 write=0x00000010")
        self.assertEqual(out, [
            "The instruction at address 0x00011234 referenced memory at "
            "address 0x00000010.  The memory could not be written."])

    def test_exception_stop_anywhere(self):
        # "stop" right after the keyword, and at the end, both count.
        for line in ("exception stop code=0xc0000094 thread=7 address=0x00011000",
                     "exception code=0xc0000094 thread=7 address=0x00011000 stop"):
            f, out = self.fmt(line)
            self.assertIn("Integer Divide by Zero", out[0])
            self.assertIn("second chance", out[0])
            self.assertEqual(f.stopped_thread, "7")
            self.assertEqual(f.stopped_reason, "Exception (Integer Divide by Zero)")

    def test_first_chance_does_not_stop(self):
        f, out = self.fmt("exception stop code=0x80000003 thread=7 "
                          "address=0x00011000 first")
        self.assertIn("breakpoint exception", out[0])
        self.assertIsNone(f.stopped_thread)

    def test_noncont(self):
        _, out = self.fmt("exception code=0xc00000fd thread=7 address=0x1 noncont")
        self.assertEqual(out[-1], "Exception is non-continuable.")

    def test_assert_and_rip(self):
        f, out = self.fmt("assert stop thread=3 string=x != 0",
                          name="milenko")
        self.assertEqual(out[0], "Xbox Assertion Failed - thread 3 [milenko]")
        self.assertEqual(out[1], "x != 0")
        self.assertEqual(f.stopped_reason, "Assertion Failed")
        f, out = self.fmt("rip stop thread=3 string=bad thing")
        self.assertEqual(out[0], "A RIP error has occurred on the Xbox (thread 3).")
        self.assertEqual(out[1], "The error description was: bad thing")

    def test_modules_and_quiet_kinds(self):
        _, out = self.fmt('modload name="default.xbe" base=0x00010000 '
                          'size=0x00200000 check=0x0 timestamp=0x0 xbe',
                          'sectload name="XNET" base=0x00300000 size=0x1000 index=3 flags=0',
                          'sectunload name="XNET" base=0x00300000 size=0x1000',
                          'modunload name="xbdm.dll"',
                          "create thread=9 start=0x00011000",
                          "terminate thread=9",
                          "singlestep addr=0x1 thread=2",
                          "stacktrace thread=2",
                          "null")
        self.assertEqual(out, [
            "Loaded: default.xbe base=0x00010000 size=0x00200000 [XBE]",
            "Section loaded: XNET base=0x00300000 size=0x00001000",
            "Section unloaded: XNET",
            "Unloaded: xbdm.dll"])

    def test_break_data_execution_unknown(self):
        f, out = self.fmt("break addr=0x00011000 thread=2 stop",
                          "data write=0x00020000 addr=0x00011004 thread=2",
                          "execution rebooting",
                          "execution started",
                          "mystery thing=1")
        self.assertEqual(out[0], "Break: 0x00011000 thread=2")
        self.assertEqual(out[1], "STOPPED: Breakpoint (thread 2)")
        self.assertEqual(out[2], "Databreak: write=0x00020000 addr=0x00011004 thread=2")
        self.assertEqual(out[3], "xbMacson: Xbox is restarting.")
        self.assertEqual(out[4], "Execution started.")
        self.assertEqual(out[5], "mystery thing=1")

    def test_classify(self):
        self.assertEqual(classify_line("Fatal error"), "red")
        self.assertEqual(classify_line("warning: x"), "yellow")
        self.assertEqual(classify_line("init done"), "green")
        self.assertEqual(classify_line("[net] hello"), "cyan")
        self.assertEqual(classify_line("plain"), "normal")


if __name__ == "__main__":
    unittest.main()
