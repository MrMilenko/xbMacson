"""Map files: turn addresses into function+offset.

Supported inputs:

- MSVC-style link maps, which is what lld-link writes for ``/map`` (the
  ``halo.map`` in warthog-xbox's ``build/xbox/``). The parser reads the
  preferred load address, the section table (to know which segments hold
  code) and the ``Publics by Value`` and ``Static symbols`` lists.
- ``nm`` style lines (``00011000 T _name``), as a fallback.
"""

import bisect
import re

_PREF_RE = re.compile(r"Preferred load address is\s+([0-9a-fA-F]+)")
_SECTION_RE = re.compile(
    r"^\s*([0-9a-fA-F]{4}):([0-9a-fA-F]{8})\s+([0-9a-fA-F]+)H\s+(\S+)\s+(CODE|DATA)\s*$")
_PUBLIC_RE = re.compile(
    r"^\s*([0-9a-fA-F]{4}):([0-9a-fA-F]{8})\s+(\S+)\s+([0-9a-fA-F]{8,16})"
    r"(?:\s+(f))?(?:\s+(i))?\s*(.*)$")
_NM_RE = re.compile(r"^\s*([0-9a-fA-F]{8,16})\s+([A-Za-z])\s+(\S+)\s*$")
_STDCALL_RE = re.compile(r"@\d+$")


def undecorate(name):
    """Strip C decoration: ``_foo`` and ``_foo@12`` and ``@foo@8`` -> ``foo``.
    C++ names (starting with ``?``) are left alone."""
    if name.startswith("?"):
        return name
    if name.startswith("@"):  # fastcall
        return _STDCALL_RE.sub("", name[1:])
    if name.startswith("_"):
        return _STDCALL_RE.sub("", name[1:])
    return name


def _is_label(name):
    """COFF pseudo symbols (.bf, .ef, .lf) and assembler labels ($x$12) are
    not functions; skip them so they do not hide the real name."""
    return name.startswith(".") or name.startswith("$")


class Symbol:
    __slots__ = ("addr", "name", "obj", "size")

    def __init__(self, addr, name, obj="", size=0):
        self.addr = addr
        self.name = name
        self.obj = obj
        self.size = size

    @property
    def display(self):
        return undecorate(self.name)


class Location:
    """Result of a lookup."""

    __slots__ = ("addr", "symbol", "offset", "module")

    def __init__(self, addr, symbol=None, offset=0, module=None):
        self.addr = addr
        self.symbol = symbol
        self.offset = offset
        self.module = module

    @property
    def function(self):
        return self.symbol.display if self.symbol else None

    def __str__(self):
        if self.symbol:
            return f"{self.symbol.display}+0x{self.offset:x}"
        if self.module:
            return f"{self.module.name}+0x{self.addr - self.module.base:x}"
        return "?"

    def as_dict(self):
        return {
            "addr": self.addr,
            "function": self.function,
            "offset": self.offset if self.symbol else None,
            "object": self.symbol.obj if self.symbol else None,
            "module": self.module.name if self.module else None,
            "text": str(self),
        }


class MapFile:
    def __init__(self):
        self.preferred_base = None
        self.code_ranges = []    # [(start_va, end_va)] from CODE sections
        self.symbols = []        # sorted code symbols
        self._addrs = []
        self.slide = 0           # actual load base minus preferred base

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return cls.parse(f.read().splitlines())

    @classmethod
    def parse(cls, lines):
        m = cls()
        sections = []
        publics = []
        nm = []
        for line in lines:
            if m.preferred_base is None:
                pm = _PREF_RE.search(line)
                if pm:
                    m.preferred_base = int(pm.group(1), 16)
                    continue
            sm = _SECTION_RE.match(line)
            if sm:
                sections.append((int(sm.group(1), 16), int(sm.group(2), 16),
                                 int(sm.group(3), 16), sm.group(4), sm.group(5)))
                continue
            pm = _PUBLIC_RE.match(line)
            if pm:
                seg = int(pm.group(1), 16)
                if seg == 0:
                    continue  # absolute symbols
                publics.append((seg, int(pm.group(2), 16), int(pm.group(4), 16),
                                pm.group(3), pm.group(7).strip()))
                continue
            nmm = _NM_RE.match(line)
            if nmm and nmm.group(2) in "TtWw":
                nm.append((int(nmm.group(1), 16), nmm.group(3)))

        if publics:
            m._build_from_link_map(sections, publics)
        elif nm:
            m._build_from_nm(nm)
        if not m.symbols:
            raise ValueError("no code symbols found in map file")
        return m

    def _build_from_link_map(self, sections, publics):
        # A segment counts as code when one of its sections is class CODE.
        # Section offsets are relative to the segment; each public gives both
        # its segment offset and its VA, which places the segment in memory.
        seg_start = {}
        for seg, off, va, _name, _obj in publics:
            seg_start.setdefault(seg, va - off)
        code_segs = set()
        ranges = []
        for seg, off, length, _name, klass in sections:
            if klass != "CODE" or seg not in seg_start:
                continue
            code_segs.add(seg)
            start = seg_start[seg] + off
            ranges.append([seg, start, start + length])
        # Import thunks can sit just past the reported section length; let
        # the last range of each code segment reach its last public.
        last = {}
        for seg, _off, va, _name, _obj in publics:
            if seg in code_segs:
                last[seg] = max(last.get(seg, va), va)
        for seg, va in last.items():
            mine = [r for r in ranges if r[0] == seg]
            top = max(mine, key=lambda r: r[2])
            top[2] = max(top[2], va + 16)
        ranges = [(lo, hi) for _seg, lo, hi in ranges]
        ranges.sort()
        merged = []
        for lo, hi in ranges:
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        self.code_ranges = merged
        self._finish([Symbol(va, name, obj)
                      for seg, _off, va, name, obj in publics
                      if seg in code_segs and self._in_ranges(va)
                      and not _is_label(name)])

    def _in_ranges(self, a):
        for lo, hi in self.code_ranges:
            if lo <= a < hi:
                return True
        return False

    def _build_from_nm(self, nm):
        syms = [Symbol(a, n) for a, n in nm]
        self._finish(syms)
        if self.symbols:
            self.code_ranges = [(self.symbols[0].addr,
                                 self.symbols[-1].addr + 0x1000)]

    def _finish(self, syms):
        syms.sort(key=lambda s: s.addr)
        # Keep one name per address (the first, which is usually the real
        # function rather than an alias).
        dedup = []
        for s in syms:
            if dedup and dedup[-1].addr == s.addr:
                continue
            dedup.append(s)
        for a, b in zip(dedup, dedup[1:]):
            a.size = b.addr - a.addr
        self.symbols = dedup
        self._addrs = [s.addr for s in dedup]

    # -- queries ---------------------------------------------------------

    def rebase(self, actual_base):
        """Tell the map where the image really loaded."""
        if self.preferred_base is not None and actual_base is not None:
            self.slide = actual_base - self.preferred_base

    def in_code(self, addr):
        return self._in_ranges(addr - self.slide)

    def lookup(self, addr):
        """Symbol containing ``addr`` and the offset into it, or None when the
        address is outside the map's code."""
        if not self.in_code(addr):
            return None
        a = addr - self.slide
        i = bisect.bisect_right(self._addrs, a) - 1
        if i < 0:
            return None
        s = self.symbols[i]
        return s, a - s.addr


class Symbolizer:
    """Map file plus the module list: names every address it can."""

    def __init__(self, mapfile=None, modules=None):
        self.map = mapfile
        self.modules = list(modules or [])

    def module_for(self, addr):
        for m in self.modules:
            if m.contains(addr):
                return m
        return None

    def locate(self, addr):
        mod = self.module_for(addr)
        if self.map is not None:
            hit = self.map.lookup(addr)
            if hit:
                return Location(addr, hit[0], hit[1], mod)
        return Location(addr, None, 0, mod)

    def is_code(self, addr):
        """Could ``addr`` be a return address? Inside the map's code, or in
        some other loaded module (the kernel, a DLL). Without a map, any
        module address counts."""
        if self.map is not None and self.map.in_code(addr):
            return True
        mod = self.module_for(addr)
        if mod is None:
            return False
        return self.map is None or not mod.is_xbe


def load_symbolizer(map_path, modules=None, base=None):
    mf = MapFile.load(map_path) if map_path else None
    if mf is not None:
        if base is None and modules:
            # The title's own module is flagged xbe on the original Xbox.
            xbe = next((m for m in modules if m.is_xbe), None)
            if xbe is not None:
                base = xbe.base
        if base is not None:
            mf.rebase(base)
    return Symbolizer(mf, modules)
