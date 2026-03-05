# xbMacson

Cross-platform Xbox Debug Monitor — a modern replacement for xbWatson.

Connects to XBDM (Xbox Debug Monitor) on port 731 and streams `OutputDebugString`/`DbgPrint` output in a terminal UI. Works on macOS, Linux, and Windows — anywhere Python 3 runs.

![Python 3.6+](https://img.shields.io/badge/python-3.6%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)

## Features

- **Interactive TUI** — no flags to remember, everything is keyboard-driven
- **Network scanner** — auto-discovers Xbox consoles on your local subnet
- **Live filtering** — type to filter debug output in real-time
- **Color-coded output** — errors (red), warnings (yellow), init messages (green), tags (cyan)
- **Thread tracking** — shows which thread each message came from
- **Config file** — remembers your default Xbox, recent consoles, log directory
- **File logging** — toggle logging on/off during a session
- **Probe mode** — query XBDM version, debug name, running XBE, loaded modules
- **Auto-reconnect** — reconnects automatically when the Xbox disconnects
- **Zero dependencies** — pure Python stdlib (socket, curses, threading)

## Requirements

- Python 3.6+
- An original Xbox running a debug BIOS with XBDM enabled
- Network connectivity to the Xbox (port 731/TCP)

> **Note:** The debug BIOS may assign a secondary IP address to the Xbox that differs from the dashboard IP. Use the network scanner to find it.

## Usage

```bash
# Launch the TUI
python3 xbmacson.py

# Quick-connect to a known IP
python3 xbmacson.py 192.168.0.121
```

That's it. No flags. The TUI handles everything.

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
| `F` | Filter — live search as you type |
| `R` | Toggle raw mode (show unparsed XBDM output) |
| `L` | Toggle file logging |
| `P` | Pause/resume output |
| `C` | Clear screen |
| `I` | Probe Xbox info (version, modules, running XBE) |
| `D` | Set current Xbox as default |
| `Up/Down` | Scroll through history |
| `PgUp/PgDn` | Scroll by page |
| `Home/End` | Jump to top/bottom |
| `Esc` | Back to menu |
| `Q` | Quit |

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
  "log_dir": "",
  "last_connected": [
    ["192.168.0.121", "milenko"]
  ]
}
```

A sample config is included as `config.sample.json`.

| Setting | Description |
|---------|-------------|
| `default_ip` | Xbox IP shown as first menu option for quick-connect |
| `auto_reconnect` | Automatically reconnect on disconnect (default: true) |
| `log_dir` | Directory for log files (default: current directory) |
| `last_connected` | Recent consoles — managed automatically |

## How It Works

xbMacson speaks the XBDM text protocol over TCP:

1. Connects to port 731, receives `201- connected` greeting
2. Queries `DBGNAME` and `DMVERSION` for console info
3. Sends `NOTIFY debugstr` to subscribe to debug output
4. Parses incoming `debugstr thread=NN cr|lf string=<message>` lines
5. Displays parsed messages with timestamps, thread IDs, and color coding

## Background

[xbWatson](https://xboxdevwiki.net/XBDM) was the original Xbox Debug Monitor client, but it only runs on Windows (and really only on XP-era machines with the XDK installed). xbMacson provides the same core functionality — streaming debug output — on any platform, with a modern terminal UI.

Named as a nod to the original, with a Mac twist because Milenko is a bougie bitch.

## License

MIT
