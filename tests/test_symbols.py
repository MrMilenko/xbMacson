import os
import unittest

import helpers
from xbmacson.client import Module
from xbmacson.symbols import MapFile, Symbolizer, load_symbolizer, undecorate


class MapTests(unittest.TestCase):
    def setUp(self):
        self.m = MapFile.load(helpers.SAMPLE_MAP)

    def test_header(self):
        self.assertEqual(self.m.preferred_base, 0x10000)
        self.assertEqual(self.m.code_ranges, [(0x11000, 0x12014)])

    def test_lookup(self):
        s, off = self.m.lookup(0x11234)
        self.assertEqual((s.display, off, s.obj), ("player_update", 0x34, "player.c.obj"))
        s, off = self.m.lookup(0x11000)
        self.assertEqual(s.name, "_main_loop")   # not the .bf pseudo symbol
        s, off = self.m.lookup(0x11490)
        self.assertEqual(s.name, "?render@Scene@@QAEXXZ")  # not the $loop label
        s, _ = self.m.lookup(0x12005)
        self.assertEqual(s.display, "ExThunk")   # thunk past .text's length
        self.assertIsNone(self.m.lookup(0x13000))  # data
        self.assertIsNone(self.m.lookup(0x10fff))

    def test_undecorate(self):
        self.assertEqual(undecorate("_game_tick@4"), "game_tick")
        self.assertEqual(undecorate("@fast_math@8"), "fast_math")
        self.assertEqual(undecorate("_plain"), "plain")
        self.assertEqual(undecorate("?render@Scene@@QAEXXZ"), "?render@Scene@@QAEXXZ")

    def test_rebase(self):
        self.m.rebase(0x20000)
        s, off = self.m.lookup(0x21234)
        self.assertEqual((s.display, off), ("player_update", 0x34))
        self.assertIsNone(self.m.lookup(0x11234))

    def test_symbolizer_modules(self):
        mods = [Module("xboxkrnl.exe", 0x80010000, 0x100000),
                Module("default.xbe", 0x10000, 0x5000, flags=("xbe",))]
        sym = load_symbolizer(helpers.SAMPLE_MAP, mods)
        self.assertEqual(str(sym.locate(0x11150)), "game_tick+0x50")
        self.assertEqual(str(sym.locate(0x80010100)), "xboxkrnl.exe+0x100")
        self.assertEqual(str(sym.locate(0x5)), "?")
        self.assertTrue(sym.is_code(0x11150))
        self.assertTrue(sym.is_code(0x80010100))
        self.assertFalse(sym.is_code(0x14010))   # XBE data
        nomap = Symbolizer(None, mods)
        self.assertTrue(nomap.is_code(0x14010))

    def test_nm_format(self):
        m = MapFile.parse(["00011000 T _alpha", "00011080 t _beta",
                           "00013000 D _data"])
        self.assertEqual(m.lookup(0x11090)[0].display, "beta")

    def test_empty(self):
        with self.assertRaises(ValueError):
            MapFile.parse(["nothing here"])

    @unittest.skipUnless(os.environ.get("XBMACSON_TEST_MAP"),
                         "set XBMACSON_TEST_MAP to a real halo.map to run")
    def test_real_map(self):
        m = MapFile.load(os.environ["XBMACSON_TEST_MAP"])
        self.assertGreater(len(m.symbols), 1000)
        self.assertEqual(m.preferred_base, 0x10000)
        first = m.symbols[0]
        self.assertEqual(m.lookup(first.addr + 1)[0].addr, first.addr)


if __name__ == "__main__":
    unittest.main()
