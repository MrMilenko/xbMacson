# XBDM: the parts xbMacson relies on

This page lists the pieces of the XBDM protocol that xbMacson uses and where
each one comes from. Every source is public community documentation or
public code. No XDK material was used.

## Sources

1. **xboxdevwiki, "Xbox Debug Monitor"**:
   https://xboxdevwiki.net/Xbox_Debug_Monitor
   Reply codes, the `getfile` length prefix, `magicboot ... debug`, and the
   note that build 3944 greets with `200- connected` instead of `201`.
2. **xbdm_gdb_bridge** by Erik Abair (Unlicense):
   https://github.com/abaire/xbdm_gdb_bridge
   `src/rdcp/xbdm_requests.h` (command arguments for `getcontext`, `getmem`,
   `getmem2`, `threads`, `threadinfo`, `continue`, `isstopped`, `reboot`,
   `notifyat`, `suspend`, `resume`), `src/rdcp/types/thread_context.h`
   (context key names), `src/rdcp/xbdm_stop_reasons.h` and
   `src/notification/xbdm_notification.cpp` (notification fields).
3. **OXDK** `xbox360/tools/xbdm/xbdm.py` (public domain):
   https://github.com/Horrible-Dev/OXDK
   Xbox 360 port 730, `sendfile`/`getfile`, the 413 deploy case,
   `magicboot cold`, `notify reconnectport=0`, and the 360 screenshot format
   with its tiling function.
4. **xbMacson 1.x** (this repository's earlier single-file version, MIT):
   the `notifyat` reverse-connect model, the xbWatson-style message text,
   the original Xbox screenshot header, both `drivelist` shapes and
   `dirlist`, all used against real hardware by its author.

Where a detail below has no source, it says so.

## Connection

- TCP. Original Xbox: port 731. Xbox 360: port 730 (source 3).
- On connect the console sends a greeting, normally `201- connected`; build
  3944 sends `200- connected` (source 1). xbMacson accepts both.
- Commands are one ASCII line ending in CRLF. Command words are case
  insensitive. Arguments are `key=value` pairs; strings are quoted
  (`name="E:\dir\file.xbe"`); numbers are decimal or `0x` hex.
- `bye` closes the connection politely.
- The console allows a small number of connections at once (`401- max
  number of connections exceeded`, source 1). xbMacson keeps one command
  connection open per console and one notification channel.

## Replies

| Code | Meaning | How the body is framed |
|------|---------|------------------------|
| 200 | OK | text after `200- ` on the same line |
| 201 | connected | greeting only |
| 202 | multiline response follows | lines until a line holding only `.` |
| 203 | binary response follows | raw bytes; the command decides the length |
| 204 | send binary data | the client uploads now, then gets a final reply |
| 205 | connection dedicated | the connection now carries notifications |
| 4xx | error | text after the code |

Errors seen in practice (source 1): 400 unexpected error, 401 max
connections, 402 file not found, 403 no such module, 404 memory not mapped,
405 no such thread, 407 unknown command, 408 not stopped, 409 file must be
copied, 410 file already exists, 411 directory not empty, 412 filename is
invalid, 413 file cannot be created, 414 access denied, 415 no room on
device, 416 not debuggable, 417 type invalid, 418 data not available,
420 box not locked, 421 key exchange required, 422 dedicated connection
required.

Binary lengths, per command:

- `getmem2 addr=A length=N`: exactly N bytes (source 2).
- `getfile name="..."`: a 32-bit little-endian byte count, then the bytes
  (sources 1 and 3).
- `screenshot`: one `key=value` text line, then `framebuffersize` bytes
  (sources 3 and 4).

## Commands used

| Command | Used for | Source |
|---------|----------|--------|
| `dbgname`, `dmversion` | banner, scanner, `info` | 4 |
| `consoletype`, `systeminfo` | 360 `info` | 3 |
| `xbeinfo running` | running title | 4 |
| `modules` | module list: `name="..." base= size= check= timestamp=` plus flags such as `xbe`, `tls` | 2, 4 |
| `threads` | 202 body, one thread id per line | 2 |
| `threadinfo thread=T` | `suspend= priority= tlsbase= start= base= limit= createlo= createhi=` | 2 |
| `getcontext thread=T control int [fp]` | registers as `Ebp=0x.. Esp=0x.. Eip=0x.. EFlags=0x.. Eax=..` (202 body) | 2 |
| `suspend thread=T`, `resume thread=T` | profiler fallback | 2 |
| `isstopped thread=T` | 200 if stopped, 408 if not | 2 |
| `continue thread=T [exception]`, `go` | resume after a stop (408 means it was not stopped) | 2, 4 |
| `getmem2 addr= length=` | memory, binary | 2 |
| `getmem addr= length=` | memory as hex text, `??` for unreadable bytes; fallback when `getmem2` is unknown | 2 |
| `drivelist` | either `200- CDEFXYZ` or a 202 body of `drivename="C"` lines | 4 |
| `dirlist name="X:\dir\"` | `name="..." sizehi= sizelo= ... [directory]` | 4 |
| `getfile`, `sendfile name= length=` | download, upload (204 then data, then 200) | 1, 3 |
| `magicboot title="..." debug` | launch an XBE and keep XBDM loaded | 1, 4 |
| `magicboot title="..." directory="..."` | launch a 360 XEX | 3 |
| `magicboot cold` | 360 reboot before a redeploy | 3 |
| `reboot [warm] [wait] [stop] [nodebug]` | reboot | 2 |
| `debugger connect` / `disconnect` | debug session around the listener | 2, 4 |
| `notifyat port=P [drop] [debug]` | ask XBDM to dial us; `drop` removes it | 2, 4 |
| `notify reconnectport=0` | make this connection the notification channel | 3 |
| `screenshot` | framebuffer | 3, 4 |

xbMacson sends thread ids as `0x` hex (as source 2 does) and reads them in
either form.

## Notification channels

**notifyat** (original Xbox default). xbMacson binds a local port, sends
`debugger connect` and `notifyat port=P` on its command connection, and
waits. XBDM connects back to that port and pushes one line per event. XBDM
keeps the registration across in-game resets, reboots and title launches,
so after a drop xbMacson just waits for the next dial-in. If none comes in
30 seconds it registers again. On exit it sends `notifyat port=P drop` and
`debugger disconnect` (source 4).

**notify** (Xbox 360 default). Send `notify reconnectport=0` on a fresh
connection and read lines from it (source 3). If the link drops, xbMacson
opens a new connection and asks again. What `reconnectport=0` asks of the
console is not documented in any source; xbMacson sends it because the
OXDK tool does and that tool works.

`--channel` picks either one on either console.

## Notification lines

Each line is a keyword, then `key=value` fields and bare flags. xbMacson
reads the fields as an unordered set (as source 2 does), so a `stop` flag
counts wherever it appears.

| Keyword | Fields | Source |
|---------|--------|--------|
| `debugstr` | `thread=`, one of `cr` / `lf` / `crlf`, then `string=` to end of line. `cr` pieces join until an `lf` | 2, 4 |
| `assert` | `[stop] thread= string=...` | 4 |
| `rip` | `[stop] thread= [string=...]` | 4 |
| `exception` | `code= thread= address= [read=|write=] [first] [noncont]` and `stop` when halted | 2, 4 |
| `break`, `singlestep` | `addr= thread=` | 2, 4 |
| `data` | `read=|write=|execute= addr= thread=` | 2, 4 |
| `execution` | `stopped`, `started`, `rebooting` or `pending` | 2, 4 |
| `modload` | `name= base= size= check= timestamp= [tls] [xbe]` | 4 |
| `sectload`, `sectunload` | `name= base= size= index= flags=` | 4 |
| `modunload` | `name=` | 4 |
| `create`, `terminate` | `thread= [start=]` | 2, 4 |
| `null` | keepalive | 4 |

Formatting follows xbWatson's wording, from the earlier xbMacson (source 4):
access violations read "The instruction at address 0x... referenced memory
at address 0x....  The memory could not be read/written.", other
exceptions name the code and say first or second chance, and only
second-chance exceptions with `stop` halt the monitor. Single steps, stack
trace requests and thread create/terminate lines are dropped.

## Screenshots

Header keys: `pitch= width= height= format= offsetx= offsety=
framebuffersize=` (sources 3 and 4).

- Original Xbox: linear 32-bit, B G R X in memory, rows `pitch` bytes apart
  (source 4).
- Xbox 360: `format` bit 0x100 marks a tiled surface. Texels are byte
  swapped so memory order is B G R A, and positions go through the Xenos 2D
  tiling function in `xbmacson/screenshot.py`, taken from source 3. A
  surface without the tiled bit is decoded as linear with the same byte
  order; no source shows such a surface.

PNG files are written with `zlib` and `struct` only.

## Deploy when the file is in use

Uploading over a file the running title holds answers `413- file cannot be
created`. `deploy` then reboots (original Xbox: `reboot`; 360: `magicboot
cold`), waits until `dbgname` answers again, and retries a few times with
growing waits, because the dashboard can hold the file for a moment after
XBDM is back (source 3). `--no-reboot` turns this off.

## Not verified on hardware

The rebuild was written and tested against the fake server in `tests/`, not
a console. These points come from the sources above and still need a check
on a real devkit:

- whether `getcontext` on a running original Xbox thread answers or needs
  the thread suspended (see `docs/profiling.md`);
- `getmem2` and `threadinfo` reply shapes on the debug BIOS in use (taken
  from source 2);
- the `reboot` path for an in-use file on the original Xbox (the 413 case is
  documented for the 360 only);
- Xbox 360 notification lines, thread id format and `getcontext` key names
  (`Iar`, `Lr`, `Gpr1` are assumed) and the PowerPC back-chain walk;
- whether `debugger disconnect` on exit changes anything for a thread that
  is still stopped.
