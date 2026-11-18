"""Framebuffer decoding and a stdlib PNG writer."""

import struct
import zlib


def png_bytes(width, height, rgb, level=6):
    """Encode packed 8-bit RGB rows as a PNG."""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + chunk(b"IEND", b""))


def write_png(path, width, height, rgb):
    with open(path, "wb") as f:
        f.write(png_bytes(width, height, rgb))


def decode_linear(fb, width, height, pitch):
    """Linear 32-bit surface with B, G, R, X byte order in memory.

    This is the original Xbox X8R8G8B8 front buffer (little endian). Returns
    packed RGB.
    """
    out = bytearray(width * height * 3)
    row_bytes = width * 4
    for y in range(height):
        row = fb[y * pitch:y * pitch + row_bytes]
        if len(row) < row_bytes:
            row = row + bytes(row_bytes - len(row))
        o = y * width * 3
        end = o + width * 3
        out[o:end:3] = row[2::4]
        out[o + 1:end:3] = row[1::4]
        out[o + 2:end:3] = row[0::4]
    return bytes(out)


def xenos_tiled_offset(x, y, width, texel_pitch):
    """Texel index of (x, y) in a tiled Xbox 360 (Xenos) 2D surface.

    Taken from OXDK's xbox360/tools/xbdm/xbdm.py (public domain). The last
    term mixes x and y, so it is not a plain permutation of address bits.
    """
    aligned_width = (width + 31) & ~31
    log_bpp = (texel_pitch >> 2) + ((texel_pitch >> 1) >> (texel_pitch >> 2))
    macro = ((x >> 5) + (y >> 5) * (aligned_width >> 5)) << (log_bpp + 7)
    micro = ((x & 7) + ((y & 6) << 2)) << log_bpp
    offset = (macro + ((micro & ~15) << 1) + (micro & 15)
              + ((y & 8) << (3 + log_bpp)) + ((y & 1) << 4))
    return ((((offset & ~511) << 3) + ((offset & 448) << 2) + (offset & 63)
             + ((y & 16) << 7) + (((((y & 8) >> 2) + (x >> 3)) & 3) << 6))
            >> log_bpp)


def decode_xenos_tiled(fb, width, height):
    """Untile a 32-bit Xenos front buffer. Texels are byte swapped
    (GPUENDIAN_8IN32), so memory order is B, G, R, A. Returns packed RGB."""
    ntex = len(fb) // 4
    out = bytearray(width * height * 3)
    o = 0
    for y in range(height):
        for x in range(width):
            t = xenos_tiled_offset(x, y, width, 4)
            if t < ntex:
                off = t * 4
                out[o] = fb[off + 2]
                out[o + 1] = fb[off + 1]
                out[o + 2] = fb[off]
            o += 3
    return bytes(out)


XENOS_TILED_FLAG = 0x100


class Screenshot:
    def __init__(self, width, height, rgb, meta):
        self.width = width
        self.height = height
        self.rgb = rgb
        self.meta = meta

    def save(self, path):
        write_png(path, self.width, self.height, self.rgb)
        return path


def decode(meta, fb, kind):
    """Decode a screenshot reply. ``meta`` is the parsed header line."""
    from .protocol import to_int
    width = to_int(meta.get("width"))
    height = to_int(meta.get("height"))
    pitch = to_int(meta.get("pitch"), width * 4 if width else 0)
    fmt = to_int(meta.get("format"), 0)
    if not width or not height:
        raise ValueError(f"screenshot header has no size: {meta}")
    if kind == "xenos" and fmt & XENOS_TILED_FLAG:
        rgb = decode_xenos_tiled(fb, width, height)
    else:
        rgb = decode_linear(fb, width, height, pitch)
    return Screenshot(width, height, rgb, meta)
