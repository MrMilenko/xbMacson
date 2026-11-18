# crash and profile

Both commands read a thread's registers with `getcontext` and name addresses
with a map file.

## Map files

`--map` takes the MSVC-style map that lld-link writes for `/map`, such as
`build/xbox/halo.map` from warthog-xbox. xbMacson reads:

- `Preferred load address is ...`
- the section table, to learn which segments hold code;
- the `Publics by Value` list (and `Static symbols` if present).

COFF pseudo symbols (`.bf`, `.ef`, `.lf`) and assembler labels (`$name$12`)
are skipped. C decoration is removed for display (`_foo@12` shows as `foo`);
C++ names are left as they are. If the title loaded somewhere other than the
preferred address, xbMacson uses the base of the module flagged `xbe`, or
`--base`. Plain `nm` output (`00011000 T _name`) also works.

Addresses outside the map but inside a loaded module print as
`module+0xoffset` (for example `xboxkrnl.exe+0x1a2c`).

## crash

```
xbmacson crash --console <console> --map build/xbox/halo.map
```

1. Opens a notification channel and waits for an exception (or `--on
   rip,assert,break`). First-chance exceptions are skipped unless
   `--first-chance` is given.
2. Reads the faulting thread's registers (`getcontext ... control int`) and
   its stack bounds (`threadinfo` base and limit).
3. Walks the ebp chain: `[ebp]` is the caller's ebp, `[ebp+4]` the return
   address. It stops when ebp leaves the stack, stops increasing, or the
   return address is not code.
4. Scans `--scan` bytes (default 4096) up from esp for words that point
   into code, and marks the ones that follow a call instruction (`E8 rel32`
   or `FF /2`). The scan finds frames that the chain misses in code built
   without frame pointers, and it also finds stale values, so treat
   unmarked hits with doubt.
5. Prints the xbWatson text, registers, the chain and the scan. `--json`
   prints the same as JSON.

The thread is left stopped so you can look further (`mem`, `threads`).
`--resume` sends `continue` and `go` afterwards; so does `xbmacson go
--thread <id>`.

`--thread <id>` skips the wait and inspects a thread that is already
stopped.

On the Xbox 360 the chain walk follows the PowerPC back chain (`[r1]`, with
the saved link register 8 bytes below the caller's frame). That walk is
untested on hardware.

## profile

```
xbmacson profile --console <console> --map build/xbox/halo.map --thread 28 --rate 100 --seconds 10
```

Each tick reads the pc of each chosen thread (`getcontext ... control`) and
records it. At the end the samples are ranked by function (or `--by module`,
`--by address`). `--samples out.csv` keeps every sample. Without `--thread`
every thread is sampled.

### Suspend or not

`--suspend auto` (the default) first reads the context of the running
thread. If XBDM answers, that is what every sample does. If XBDM answers
`408- not stopped`, every later sample does `suspend`, `getcontext`,
`resume`. The report says which happened.

Why a plain read can be valid on the original Xbox: it has one CPU core.
While XBDM handles our request, the target thread is not running, so the
context XBDM has for it is where the scheduler last left it. That reasoning
has not been checked on hardware. The Xbox 360 has six hardware threads, so
a thread can be running while XBDM answers; there `--suspend always` is the
safer choice until it is checked.

### Cost

Each sample is one round trip per thread without suspend, three with it.
On a wired LAN a round trip is usually around a millisecond, so:

- 100 samples/s on one thread costs about 100 requests per second of XBDM
  work on the console, and that CPU time comes out of the title's.
- With suspend, the thread is held for about two round trips per sample,
  so at 100 samples/s it is stopped roughly 10 to 30 percent of the time.
  That slows the title and moves timing-sensitive work (audio, network,
  frame pacing).
- Samples land where the scheduler stopped the thread. A thread waiting in
  the kernel shows up in `xboxkrnl.exe`.

If the console cannot keep up, the achieved rate in the report drops below
the requested one; the profiler does not burst to catch up. Use the lowest
rate that gives stable rankings, and compare runs at the same rate.
