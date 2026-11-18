"""Stack walking from a thread context and memory reads.

Two methods, used together:

- Frame chain. On x86 with frame pointers, ``[ebp]`` is the caller's ebp and
  ``[ebp+4]`` the return address. On the 360 (PowerPC), ``[r1]`` is the back
  chain and the saved link register sits 8 bytes below the caller's frame.
  Exact when the code keeps frame pointers; it stops at the first function
  that does not.
- Stack scan. Read a block from the stack pointer up and keep every aligned
  word that points into code. On x86 the candidates can be checked: a real
  return address follows a call instruction. Finds frames the chain misses,
  with some false positives (stale values, function pointers).
"""

import struct

from .protocol import XbdmError


class Frame:
    __slots__ = ("index", "pc", "frame", "method", "location", "verified")

    def __init__(self, index, pc, frame, method, verified=None):
        self.index = index
        self.pc = pc
        self.frame = frame          # frame pointer or stack slot address
        self.method = method        # "context", "ebp", "backchain", "scan"
        self.location = None
        self.verified = verified    # scan: preceded by a call instruction

    def as_dict(self):
        d = {"index": self.index, "pc": self.pc, "frame": self.frame,
             "method": self.method, "verified": self.verified}
        if self.location is not None:
            d.update(self.location.as_dict())
        return d


class MemoryReader:
    """Caches 4 KB pages so a walk does not re-read the same memory."""

    PAGE = 0x1000

    def __init__(self, console):
        self.console = console
        self.endian = console.profile.endian
        self._pages = {}
        self.reads = 0

    def _page(self, base):
        if base not in self._pages:
            try:
                self.reads += 1
                self._pages[base] = self.console.read_memory(base, self.PAGE)
            except XbdmError:
                self._pages[base] = None
        return self._pages[base]

    def read(self, addr, length):
        out = bytearray()
        while length > 0:
            base = addr & ~(self.PAGE - 1)
            page = self._page(base)
            if page is None:
                return None
            off = addr - base
            take = min(length, self.PAGE - off)
            out += page[off:off + take]
            addr += take
            length -= take
        return bytes(out)

    def u32(self, addr):
        b = self.read(addr, 4)
        if b is None or len(b) < 4:
            return None
        return struct.unpack(self.endian + "I", b)[0]


def _in_stack(addr, lo, hi):
    if lo is None or hi is None:
        return True
    return lo <= addr < hi


def walk_ebp(mem, eip, ebp, stack_lo=None, stack_hi=None, max_frames=64,
             is_code=None):
    """Follow the x86 ebp chain. Frame 0 is the faulting eip."""
    frames = [Frame(0, eip, ebp, "context")]
    seen = set()
    while len(frames) < max_frames:
        if ebp is None or ebp == 0 or ebp & 3 or ebp in seen:
            break
        if not _in_stack(ebp, stack_lo, stack_hi):
            break
        seen.add(ebp)
        next_ebp = mem.u32(ebp)
        ret = mem.u32(ebp + 4)
        if ret is None or ret == 0:
            break
        if is_code is not None and not is_code(ret):
            break
        frames.append(Frame(len(frames), ret, ebp, "ebp"))
        if next_ebp is None or next_ebp <= ebp:
            break
        ebp = next_ebp
    return frames


def walk_backchain(mem, pc, lr, sp, stack_lo=None, stack_hi=None,
                   max_frames=64, is_code=None):
    """Follow the PowerPC back chain (Xbox 360 ABI: the link register is
    saved at -8 from the caller's stack pointer). Frame 0 is the pc, frame 1
    the live link register."""
    frames = [Frame(0, pc, sp, "context")]
    if lr:
        frames.append(Frame(1, lr, sp, "lr"))
    seen = set()
    while len(frames) < max_frames:
        if not sp or sp & 3 or sp in seen or not _in_stack(sp, stack_lo, stack_hi):
            break
        seen.add(sp)
        caller_sp = mem.u32(sp)
        if not caller_sp or caller_sp <= sp:
            break
        saved_lr = mem.u32(caller_sp - 8)
        if saved_lr is None or saved_lr == 0:
            break
        if is_code is not None and not is_code(saved_lr):
            break
        if not frames or frames[-1].pc != saved_lr:
            frames.append(Frame(len(frames), saved_lr, caller_sp, "backchain"))
        sp = caller_sp
    return frames


def follows_call_x86(mem, ret):
    """Is the instruction before ``ret`` a call? Checks the common forms:
    E8 rel32, FF /2 with modrm (register, [reg], [reg+disp8], [reg+disp32],
    [disp32]) and their SIB variants."""
    pre = mem.read(ret - 7, 7)
    if pre is None:
        return False
    if pre[2] == 0xE8:                          # call rel32 (5 bytes)
        return True
    for n in (2, 3, 4, 6, 7):                   # call r/m32
        op = pre[7 - n]
        if op != 0xFF:
            continue
        modrm = pre[7 - n + 1]
        if (modrm >> 3) & 7 != 2:
            continue
        mod, rm = modrm >> 6, modrm & 7
        size = 2
        if mod == 0 and rm == 5:
            size = 6
        elif mod == 1:
            size = 3
        elif mod == 2:
            size = 6
        if rm == 4 and mod != 3:
            size += 1
        if size == n:
            return True
    return False


def scan_stack(mem, sp, length, is_code, endian="<", stack_hi=None,
               check_call=None, max_hits=64):
    """Aligned words from ``sp`` that point into code."""
    if stack_hi is not None:
        length = max(0, min(length, stack_hi - sp))
    data = mem.read(sp, length) if length else b""
    if data is None:
        # Fall back to reading as far as we can, page by page.
        data = b""
        for off in range(0, length, 0x100):
            chunk = mem.read(sp + off, min(0x100, length - off))
            if chunk is None:
                break
            data += chunk
    hits = []
    fmt = endian + "I"
    for off in range(0, len(data) - 3, 4):
        (val,) = struct.unpack_from(fmt, data, off)
        if val and is_code(val):
            verified = check_call(val) if check_call else None
            hits.append(Frame(len(hits), val, sp + off, "scan", verified))
            if len(hits) >= max_hits:
                break
    return hits


def walk(console, ctx, symbolizer, stack_lo=None, stack_hi=None,
         scan_bytes=4096, max_frames=64):
    """Both walks for one thread. Returns (chain_frames, scan_frames)."""
    profile = console.profile
    mem = MemoryReader(console)
    is_code = symbolizer.is_code if (symbolizer.map or symbolizer.modules) else None
    pc = ctx.get(profile.pc_reg)
    sp = ctx.get(profile.sp_reg)
    if profile.arch == "x86":
        chain = walk_ebp(mem, pc, ctx.get("ebp"), stack_lo, stack_hi,
                         max_frames, is_code)
        check = (lambda a: follows_call_x86(mem, a))
    else:
        chain = walk_backchain(mem, pc, ctx.get("lr"), sp, stack_lo, stack_hi,
                               max_frames, is_code)
        check = None
    scan = []
    if scan_bytes and sp and is_code is not None:
        scan = scan_stack(mem, sp, scan_bytes, is_code, profile.endian,
                          stack_hi, check)
    for f in chain + scan:
        f.location = symbolizer.locate(f.pc)
    return chain, scan
