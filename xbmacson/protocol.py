"""XBDM wire format: reply codes, errors and key=value parsing.

XBDM is a line protocol. The client sends one ASCII command ending in CRLF and
the console answers with a line that starts with a three digit code. See
docs/protocol.md for the parts this tool relies on and where they come from.
"""

import re

# Success codes
OK = 200
CONNECTED = 201
MULTILINE = 202
BINARY = 203
SEND_BINARY = 204
DEDICATED = 205

# Error codes seen most often. The full list is in docs/protocol.md.
ERR_UNEXPECTED = 400
ERR_MAX_CONNECTIONS = 401
ERR_FILE_NOT_FOUND = 402
ERR_NO_SUCH_MODULE = 403
ERR_MEMORY_NOT_MAPPED = 404
ERR_NO_SUCH_THREAD = 405
ERR_UNKNOWN_COMMAND = 407
ERR_NOT_STOPPED = 408
ERR_FILE_MUST_BE_COPIED = 409
ERR_ALREADY_EXISTS = 410
ERR_DIRECTORY_NOT_EMPTY = 411
ERR_BAD_FILENAME = 412
ERR_CANNOT_CREATE = 413
ERR_ACCESS_DENIED = 414

ERROR_NAMES = {
    400: "unexpected error",
    401: "max number of connections exceeded",
    402: "file not found",
    403: "no such module",
    404: "memory not mapped",
    405: "no such thread",
    406: "failed to set system time",
    407: "unknown command",
    408: "not stopped",
    409: "file must be copied",
    410: "file already exists",
    411: "directory not empty",
    412: "filename is invalid",
    413: "file cannot be created",
    414: "access denied",
    415: "no room on device",
    416: "not debuggable",
    417: "type invalid",
    418: "data not available",
    420: "box not locked",
    421: "key exchange required",
    422: "dedicated connection required",
}


class ConnectError(OSError):
    """The console could not be reached or did not greet us."""


class XbdmError(Exception):
    """The console answered a command with an error code (4xx)."""

    def __init__(self, code, text, command=""):
        self.code = code
        self.text = text
        self.command = command
        msg = f"{code}- {text}" if text else str(code)
        if command:
            msg = f"{command.split(' ', 1)[0]}: {msg}"
        super().__init__(msg)


class Reply:
    """One answer from XBDM.

    ``text`` is the status line after the code. ``lines`` holds the body of a
    202 multiline reply. ``data`` holds the payload of a 203 binary reply when
    the caller read one.
    """

    __slots__ = ("code", "text", "lines", "data")

    def __init__(self, code, text="", lines=None, data=None):
        self.code = code
        self.text = text
        self.lines = lines if lines is not None else []
        self.data = data

    @property
    def ok(self):
        return 200 <= self.code < 300

    def body(self):
        """The useful text of the reply: multiline body or the status text."""
        if self.code == MULTILINE:
            return "\n".join(self.lines)
        return self.text

    def raise_for_error(self, command=""):
        if not self.ok:
            raise XbdmError(self.code, self.text, command)
        return self

    def __repr__(self):
        return f"Reply({self.code}, {self.text!r}, lines={len(self.lines)})"


_STATUS_RE = re.compile(r"^(\d{3})(?:-\s?(.*))?$")


def parse_status(line):
    """Split a status line into (code, text). Returns (None, line) if it is
    not a status line."""
    m = _STATUS_RE.match(line)
    if not m:
        return None, line
    return int(m.group(1)), (m.group(2) or "").strip()


_KV_RE = re.compile(r'\s*([^\s=]+)(?:=("(?:[^"\\]|\\.)*"|\S*))?')


def parse_kv(text):
    """Parse an XBDM key=value line into a dict.

    Keys are lower-cased, so ``Eip=0x10`` and ``eip=0x10`` both land under
    ``eip``. Quoted values lose their quotes. A bare word with no ``=`` is a
    flag and maps to True (for example ``xbe`` in a module line, or ``stop``
    in a notification).
    """
    out = {}
    pos = 0
    n = len(text)
    while pos < n:
        m = _KV_RE.match(text, pos)
        if not m or m.end() == pos:
            break
        key = m.group(1).lower()
        val = m.group(2)
        if val is None:
            out[key] = True
        else:
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            out[key] = val
        pos = m.end()
    return out


def to_int(value, default=None):
    """Turn an XBDM number (``0x1f``, ``31`` or a flag) into an int."""
    if value is None or value is True or value is False:
        return default
    try:
        return int(value, 0)
    except (TypeError, ValueError):
        try:
            return int(value, 16)
        except (TypeError, ValueError):
            return default


def qword(kv, lo, hi):
    """Combine a lo/hi pair such as sizelo/sizehi into one integer."""
    return (to_int(kv.get(hi), 0) << 32) | to_int(kv.get(lo), 0)


def quote(path):
    """Quote a path for a ``name="..."`` argument."""
    return '"' + str(path).replace('"', "") + '"'


def hex32(n):
    return f"0x{n & 0xFFFFFFFF:08x}"
