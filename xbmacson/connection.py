"""One TCP command connection to XBDM."""

import socket
import struct

from .protocol import (
    BINARY, CONNECTED, MULTILINE, OK, SEND_BINARY,
    ConnectError, Reply, XbdmError, parse_kv, parse_status, to_int,
)

WIRE_ENCODING = "latin-1"


def split_host(target, default_port):
    """``"<console>"`` or ``"<console>:<port>"`` -> (host, port)."""
    host, port = target, default_port
    if target.count(":") == 1:
        h, p = target.rsplit(":", 1)
        if p.isdigit():
            host, port = h, int(p)
    return host, port


def enable_keepalive(sock, idle=4, interval=2, count=3):
    """Turn on TCP keepalive tuned to notice a dead peer in about 10s.

    A rebooting console usually drops the link without a clean close. Options
    vary by platform, so set whatever exists.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for opt, val in (("TCP_KEEPIDLE", idle), ("TCP_KEEPALIVE", idle),
                     ("TCP_KEEPINTVL", interval), ("TCP_KEEPCNT", count)):
        if hasattr(socket, opt):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
            except OSError:
                pass
    if hasattr(sock, "ioctl") and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS,
                       (1, idle * 1000, interval * 1000))
        except OSError:
            pass


class Connection:
    """A command connection. Not thread safe: callers serialize access.

    ``command()`` handles the text replies (200, 201, 202, 205 and errors).
    Binary replies (203) and uploads (204) need the caller to say how long the
    payload is, so they have their own methods.
    """

    def __init__(self, host, port, timeout=5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self._rf = None
        self.greeting = ""

    # -- lifetime --------------------------------------------------------

    def open(self):
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=self.timeout)
        except OSError as e:
            raise ConnectError(f"cannot connect to {self.host}:{self.port}: {e}")
        self.sock.settimeout(self.timeout)
        self._rf = self.sock.makefile("rb")
        try:
            line = self.read_line()
        except OSError as e:
            self.close(send_bye=False)
            raise ConnectError(f"no greeting from {self.host}:{self.port}: {e}")
        code, _ = parse_status(line)
        # 201 is the normal greeting; very early XBDM builds send 200.
        if code not in (CONNECTED, OK):
            self.close(send_bye=False)
            raise ConnectError(f"unexpected greeting: {line!r}")
        self.greeting = line
        return self

    def close(self, send_bye=True):
        if self.sock is None:
            return
        if send_bye:
            try:
                self.sock.sendall(b"bye\r\n")
            except OSError:
                pass
        for obj in (self._rf, self.sock):
            try:
                obj.close()
            except Exception:
                pass
        self.sock = None
        self._rf = None

    @property
    def is_open(self):
        return self.sock is not None

    def __enter__(self):
        if not self.is_open:
            self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- raw I/O ---------------------------------------------------------

    def send_line(self, text):
        self.sock.sendall(text.encode(WIRE_ENCODING, "replace") + b"\r\n")

    def read_line(self):
        raw = self._rf.readline(65536)
        if not raw:
            raise ConnectionResetError("connection closed by console")
        return raw.decode(WIRE_ENCODING).rstrip("\r\n")

    def read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self._rf.read(n - len(buf))
            if not chunk:
                raise ConnectionResetError(
                    f"connection closed after {len(buf)} of {n} bytes")
            buf += chunk
        return bytes(buf)

    def send_bytes(self, data):
        self.sock.sendall(data)

    # -- replies ---------------------------------------------------------

    def read_reply(self):
        """Read a status line, plus the body if it is a 202."""
        line = self.read_line()
        code, text = parse_status(line)
        if code is None:
            raise XbdmError(400, f"malformed reply: {line!r}")
        lines = []
        if code == MULTILINE:
            while True:
                body = self.read_line()
                if body == ".":
                    break
                lines.append(body)
        return Reply(code, text, lines)

    def command(self, cmd, check=True):
        """Send ``cmd`` and return its Reply. Raises XbdmError on 4xx when
        ``check`` is true."""
        self.send_line(cmd)
        reply = self.read_reply()
        if check:
            reply.raise_for_error(cmd)
        return reply

    def command_binary(self, cmd, length=None, length_prefix=False,
                       header_line=False, size_key=None):
        """Send a command that answers 203 and read the payload.

        - ``length``: the payload size is known in advance (getmem2).
        - ``length_prefix``: a 32-bit little-endian size comes first (getfile).
        - ``header_line`` + ``size_key``: a key=value line comes first and one
          of its keys gives the size (screenshot uses framebuffersize).

        Returns a Reply whose ``data`` is the payload and, for header_line,
        whose ``text`` is that header line.
        """
        self.send_line(cmd)
        reply = self.read_reply()
        if reply.code != BINARY:
            reply.raise_for_error(cmd)
            raise XbdmError(400, f"expected binary reply, got {reply.code}", cmd)
        if header_line:
            header = self.read_line()
            kv = parse_kv(header)
            length = to_int(kv.get(size_key))
            if length is None:
                raise XbdmError(400, f"no {size_key} in {header!r}", cmd)
            reply.text = header
        elif length_prefix:
            (length,) = struct.unpack("<I", self.read_exact(4))
        if length is None:
            raise ValueError("binary length unknown")
        reply.data = self.read_exact(length)
        return reply

    def command_send(self, cmd, data):
        """Send a command that answers 204, then upload ``data``."""
        self.send_line(cmd)
        reply = self.read_reply()
        if reply.code != SEND_BINARY:
            reply.raise_for_error(cmd)
            raise XbdmError(400, f"expected 204, got {reply.code}", cmd)
        self.send_bytes(data)
        return self.read_reply().raise_for_error(cmd)
