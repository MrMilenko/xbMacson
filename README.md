# xbMacson

Cross-platform Xbox Debug Monitor for the original Xbox.

Connects to XBDM on port 731 and streams `OutputDebugString` / `DbgPrint` output in a terminal UI. Works on macOS, Linux, and Windows; pure Python 3 stdlib.

![Python 3.6+](https://img.shields.io/badge/python-3.6%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)

## Features

- **Interactive TUI**: no flags to remember, everything is keyboard driven.
- **Faithful xbWatson output**: debug strings, asserts, RIPs, exceptions and module loads, formatted the way xbWatson shows them. No on-screen timestamp or thread chrome by default.
- **Survives resets and reboots**: clones xbWatson's `NOTIFYAT` reverse-connect model, so XBDM dials back automatically across in-game resets, hard reboots and XBE launches.
- **Network scanner**: auto-discovers Xbox consoles on your local subnet.
- **Live filtering**: type to filter debug output in real time.
- **Color-coded output**: errors (red), warnings (yellow), init messages (green), tags (cyan).
- **Raw mode**: toggle to see the wire output with timestamps.
- **File logging**: timestamps and thread IDs preserved in the log file.
- **Config file**: remembers your default Xbox, recent consoles, log directory.
- **Screenshot capture**: dumps the Xbox framebuffer to PNG (pure stdlib, no PIL).
- **File browser**: browse Xbox drives and directories, launch XBE files via `MAGICBOOT`.
- **Probe mode**: query XBDM version, debug name, running XBE, loaded modules.
- **Remote reboot**: reboot the Xbox from the monitor with confirmation.
- **Zero dependencies**: pure Python stdlib (socket, curses, threading).

## Requirements

- Python 3.6+
- An original Xbox running a debug BIOS with XBDM enabled
- Network connectivity to the Xbox (port 731/TCP)
- On macOS: allow Python to accept incoming connections when prompted, so XBDM can dial back

> **Note:** The debug BIOS may assign a secondary IP address to the Xbox that differs from the dashboard IP. Use the network scanner to find it.

## Usage

```bash
# Launch the TUI
python3 xbmacson.py

# Quick-connect to a known IP
python3 xbmacson.py 192.168.0.121
```

No flags. The TUI handles everything.

## TUI Controls

### Main Menu

| Key | Action |
|-----|--------|
| `Up/Down` | Navigate |
| `Enter` | Select |
| `S` | Scan network |
| `Q` | Quit |

### Monitor View

| Key | Action |
|-----|--------|
| `B` | Browse filesystem (list drives, navigate directories, launch XBEs) |
| `S` | Screenshot (capture framebuffer to PNG) |
| `F` | Filter (live search as you type) |
| `R` | Toggle raw mode (show unparsed XBDM output with timestamps) |
| `L` | Toggle file logging |
| `P` | Pause / resume output |
| `C` | Clear screen |
| `I` | Probe Xbox info (version, modules, running XBE) |
| `X` | Reboot Xbox (with confirmation) |
| `D` | Set current Xbox as default |
| `Up/Down` | Scroll through history |
| `PgUp/PgDn` | Scroll by page |
| `Home/End` | Jump to top / bottom |
| `Esc` | Back to menu (connection stays alive) |
| `Q` | Quit |

### File Browser

| Key | Action |
|-----|--------|
| `Up/Down` | Navigate entries |
| `Enter` | Open directory or launch XBE (with confirmation) |
| `Backspace` | Go up one directory |
| `R` | Refresh current directory |
| `Esc` | Back to drives / monitor |

### Filter Mode

| Key | Action |
|-----|--------|
| Type | Live filter as you type |
| `Enter` | Confirm filter |
| `Esc` | Cancel and clear filter |

## Configuration

Config is stored at `~/.xbmacson.json` and managed through the Settings menu. You can also edit it directly:

```json
{
  "default_ip": "192.168.0.121",
  "auto_reconnect": true,
  "default_logging": false,
  "log_dir": "",
  "screenshot_dir": "~/Desktop/xbox_screenshots",
  "last_connected": [
    ["192.168.0.121", "milenko"]
  ]
}
```

A sample config is included as `config.sample.json`.

| Setting | Description |
|---------|-------------|
| `default_ip` | Xbox IP shown as the first menu option for quick-connect |
| `auto_reconnect` | Automatically reconnect on disconnect (default: true) |
| `default_logging` | Start logging automatically on connect (default: false) |
| `log_dir` | Directory for log files (default: current directory) |
| `screenshot_dir` | Directory for screenshot PNGs (default: `~/Desktop/xbox_screenshots`) |
| `last_connected` | Recent consoles (managed automatically) |

## How It Works

xbMacson clones xbWatson's notification model rather than running its own polling loop:

1. Binds a local TCP listen socket on a free port.
2. Connects to the Xbox on port 731, sends `DEBUGGER connect` and `NOTIFYAT port=<local-port>` to register.
3. XBDM stores the registration in its `.ini` and dials back to the listen socket. That accepted connection is the notification channel.
4. xbMacson reads `debugstr`, `assert`, `rip`, `exception`, `modload` and the other notifications off the channel and renders them.
5. When the connection drops (in-game reset, reboot, XBE launch), the listen socket waits for the next dial-in. XBDM reconnects on its own once it is back up, because the registration is persistent.
6. On clean exit, xbMacson sends `notifyat port=<port> drop` so the Xbox stops calling a port we are no longer listening on.

Screenshots, file browsing, reboot and XBE launch all use separate one-shot connections so they do not interfere with the persistent notification channel. The file browser uses `DRIVELIST` and `DIRLIST` to enumerate, and `MAGICBOOT` to cold-boot into an XBE.

TCP keepalive is enabled on the notification socket so a rebooting Xbox that drops the link without a clean close is detected in seconds rather than the OS default.

## Background

[xbWatson](https://xboxdevwiki.net/XBDM) was the original Xbox Debug Monitor client, but it only runs on Windows (and really only on XP-era machines with the XDK installed). xbMacson does the same job on any platform, with a modern terminal UI.

Named as a nod to the original, with a Mac twist because Milenko is a bougie bitch.

## License

MIT
