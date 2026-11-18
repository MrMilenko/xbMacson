# xbMacson

Cross-platform debug monitor for the original Xbox, and now the Xbox 360.

It talks to XBDM: port 731 on the original Xbox, port 730 on the 360. You get
a terminal UI that streams debug output the way xbWatson shows it, plus
commands you can script: deploy, launch, screenshots, memory reads, a crash
catcher with a symbolized stack, and a sampling profiler. Works on macOS,
Linux and Windows. Pure Python 3 standard library.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)

## Requirements

- Python 3.8 or newer
- An original Xbox with a debug BIOS and XBDM, or an Xbox 360 devkit or
  RGH console with XBDM
- Network access to the console. On macOS, allow Python to accept incoming
  connections when asked, so XBDM can dial back.

The debug BIOS may give the Xbox a second IP address that differs from the
dashboard one. The network scanner finds it.

## Terminal UI

```bash
python3 xbmacson.py                 # menu
python3 xbmacson.py <console>       # connect right away
python3 xbmacson.py tui <console> --profile x360
```

### Main menu

| Key | Action |
|-----|--------|
| `Up/Down` | Navigate |
| `Enter` | Select |
| `S` | Scan network |
| `Q` | Quit |

### Monitor

| Key | Action |
|-----|--------|
| `B` | Browse files (drives, directories, launch XBEs) |
| `S` | Screenshot to PNG |
| `F` | Filter as you type |
| `R` | Raw mode (wire lines with timestamps) |
| `L` | Log to file |
| `P` | Pause / resume |
| `C` | Clear screen, or Continue when the console is stopped |
| `G` | Go (resume, even if the monitor missed the stop) |
| `I` | Console info |
| `X` | Reboot (asks first unless the console is stopped) |
| `D` | Make this console the default |
| `Up/Down`, `PgUp/PgDn`, `Home/End` | Scroll |
| `Esc` | Back to the menu (connection stays up) |
| `Q` | Quit |

### File browser

| Key | Action |
|-----|--------|
| `Up/Down` | Navigate |
| `Enter` | Open a directory, or launch an XBE (asks first) |
| `Backspace` | Up one directory |
| `R` | Refresh |
| `Esc` | Back to drives |

### Filter

| Key | Action |
|-----|--------|
| Type | Filter live |
| `Enter` | Keep the filter |
| `Esc` | Cancel |

## Commands

```bash
python3 xbmacson.py <command> --console <console> [options]
python3 -m xbmacson <command> ...
```

`--console` takes `<console>` or `<console>:<port>`. Without it, the
`XBMACSON_CONSOLE` variable or `default_ip` from the config is used.
`--profile x360` switches to the Xbox 360 (port 730). `--json` gives JSON
where it makes sense.

| Command | What it does |
|---------|--------------|
| `info` | Name, XBDM version, running title, modules, threads |
| `log` | Print debug output. `--file`, `--raw`, `--timestamps`, `--until REGEX`, `--duration S` |
| `deploy <local> <remote>` | Upload. If the running title holds the file, reboot and retry. `--launch`, `--no-reboot` |
| `get <remote> <local>` | Download |
| `launch <path>` | Boot an XBE or XEX |
| `screenshot [out]` | Save the framebuffer as PNG (360 frames are untiled) |
| `threads` | List threads. `--context --map FILE` shows where each one is |
| `mem <addr> <len>` | Hex dump, or `--out FILE` |
| `modules`, `ls [path]` | Modules; drives or a directory |
| `crash --map FILE` | Wait for a crash, print registers and the stack as function+offset |
| `profile --map FILE` | Sample thread pcs and rank functions |
| `reboot`, `go` | Reboot; resume after a stop |
| `scan` | Find consoles on the local network |
| `cmd "<raw>"` | Send one XBDM command |

Exit codes: 0 ok, 1 the console refused or the operation failed, 2 bad
arguments, 3 cannot connect, 4 timed out (`log --until`, `crash --wait`),
130 interrupted.

Examples:

```bash
# Deploy, launch, and wait for the title to say it is up
python3 xbmacson.py deploy --console <console> build/xbox/default.xbe 'E:\games\halo\default.xbe' --launch
python3 xbmacson.py log --console <console> --until 'main menu' --duration 120

# Catch the next crash and name the frames
python3 xbmacson.py crash --console <console> --map build/xbox/halo.map

# Where does thread 28 spend its time?
python3 xbmacson.py profile --console <console> --map build/xbox/halo.map --thread 28 --seconds 10
```

`crash` and `profile` are explained in [docs/profiling.md](docs/profiling.md),
including what sampling costs the console.

## Configuration

`~/.xbmacson.json`, edited from the Settings menu or by hand. Files from
earlier versions keep working.

```json
{
  "default_ip": "<console>",
  "auto_reconnect": true,
  "default_logging": false,
  "log_dir": "",
  "screenshot_dir": "~/Desktop/xbox_screenshots",
  "last_connected": []
}
```

| Setting | Meaning |
|---------|---------|
| `default_ip` | Console offered first in the menu, and the default for commands |
| `auto_reconnect` | Keep listening after the console drops (default true) |
| `default_logging` | Start logging on connect |
| `log_dir` | Where log files go (default: current directory) |
| `screenshot_dir` | Where screenshots go |
| `last_connected` | Recent consoles, kept for you |

## How it works

xbMacson does what xbWatson does for notifications. It listens on a local
port and sends `debugger connect` and `notifyat port=<port>`. XBDM dials
back and pushes debug strings, asserts, RIPs, exceptions and module loads.
XBDM keeps the registration across resets, reboots and title launches, so
xbMacson simply waits for the next dial-in. On exit it sends `notifyat
port=<port> drop`. The 360 profile uses `notify` on one connection instead,
as OXDK's tool does; `--channel` picks either.

Everything else (files, screenshots, launch, reboot) goes over a separate
command connection, so it never disturbs the notification channel.

The protocol pieces used, and where they are documented, are in
[docs/protocol.md](docs/protocol.md).

## Layout

```
xbmacson.py           launcher (TUI or commands)
xbmacson/
  connection.py       one XBDM connection, reply codes, binary framing
  client.py           typed commands: threads, context, memory, files, launch ...
  notify.py           notification parsing, xbWatson formatting, listener
  profiles.py         original Xbox and Xbox 360 differences
  screenshot.py       framebuffer decoding, PNG writer
  symbols.py          map file reader
  stack.py            ebp chain and stack scan
  sampler.py          profiler
  cli.py, tui.py      the two front ends
tests/                fake XBDM server and tests
```

Run the tests with `python3 -m unittest discover -s tests`. They use a fake
XBDM server on 127.0.0.1 and never touch a real console.

## Background

[xbWatson](https://xboxdevwiki.net/Xbox_Debug_Monitor) was the original Xbox
debug monitor, but it only runs on Windows (and really only on XP-era
machines with the XDK installed). xbMacson does the same job on any
platform, with a modern terminal UI.

Named as a nod to the original, with a Mac twist because Milenko is a bougie
bitch.

The Xbox 360 client and its screenshot untiling come from
[OXDK](https://github.com/Horrible-Dev/OXDK) (public domain).

## License

MIT. Copyright (c) 2026 Milenko.
