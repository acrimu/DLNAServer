# Python DLNA Media Server

A lightweight, dependency-free UPnP/DLNA media server for sharing a local
folder with smart TVs, VLC, computers, consoles, and other media players on a
home network.

It is implemented with the Python standard library and requires no database,
web framework, or background service.

## Features

- SSDP discovery and UPnP `MediaServer:1` advertisement
- DLNA 1.5 Digital Media Server identification
- ContentDirectory folder and file browsing
- HTTP byte-range streaming
- DLNA normal-play-time seeking for compatible players
- Matroska (`.mkv`) and WebM duration detection without external tools
- Concurrent streaming with clean handling of player probe connections
- Stable server identity across restarts
- Path traversal protection
- No required third-party Python packages

## Requirements

- Python 3.9 or newer
- A trusted local network
- A DLNA/UPnP-compatible media player

The server has been developed and tested on Windows. It should also run on
other platforms that support IPv4 multicast sockets.

## Quick start

Clone the repository or download its files, then open a terminal in the project
directory.

On Windows PowerShell:

```powershell
python .\dlna_server.py "D:\Media"
```

On macOS or Linux:

```bash
python3 ./dlna_server.py "/path/to/media"
```

Leave the process running while using the media server. Open the media-source
or connected-device screen on your TV or player and select
`DLNA on <computer-name>`.

Press `Ctrl+C` to stop the server.

## Command-line options

```text
usage: dlna_server.py [-h] [--name NAME] [--host HOST]
                      [--advertise ADDRESS] [--port PORT]
                      [--debug-discovery]
                      folder

positional arguments:
  folder               Folder to share

options:
  --name NAME          Display name shown to media players
  --host HOST          HTTP bind address (default: 0.0.0.0)
  --advertise ADDRESS  LAN IPv4 address advertised to players
  --port PORT          HTTP streaming port (default: 8200)
  --debug-discovery    Log incoming SSDP discovery searches
```

For example:

```powershell
python .\dlna_server.py "D:\Filme" --name "Living Room Media" --port 8200
```

If the wrong network adapter is detected, specify the computer's LAN address:

```powershell
python .\dlna_server.py "D:\Filme" --advertise 192.168.1.25
```

## Windows network configuration

DLNA discovery uses multicast UDP port `1900`. Media browsing and streaming use
TCP port `8200` by default.

Windows may prompt for permission when Python first starts the server. Allow it
on **Private networks**. The active home-network connection must also be marked
as **Private**, not Public:

```powershell
Get-NetConnectionProfile
```

To change a trusted Ethernet home connection, run PowerShell as Administrator:

```powershell
Set-NetConnectionProfile -InterfaceAlias "Ethernet" -NetworkCategory Private
```

Only do this for a network you trust. Do not mark hotel, airport, café, or other
public networks as Private.

Third-party security suites may have their own network profile and application
rules. Configure the home adapter as trusted or Home/Office and allow the
Python executable on the local network.

## Seeking support

The server supports standard HTTP byte-range requests. It also advertises DLNA
time-seek support and translates normal-play-time requests into file offsets.

For `.mkv` and `.webm` files, duration is read directly from Matroska metadata.
This enables timeline seeking on players that require a duration before showing
or activating the seek bar. Other containers can still use byte-range seeking,
but time-based seeking depends on the player's behavior.

## Troubleshooting

### The media server is not visible

Run with discovery logging enabled:

```powershell
python .\dlna_server.py "D:\Media" --debug-discovery
```

Then refresh the TV's media-source screen. A message such as the following
means the device can reach the server over SSDP:

```text
[SSDP] Search from 192.168.1.40:54321 for urn:schemas-upnp-org:device:MediaServer:1
```

If no discovery message appears:

- Confirm the server process is still running.
- Confirm the computer and player are on the same IPv4 subnet.
- Avoid guest Wi-Fi networks.
- Disable router AP/client isolation for the trusted home network.
- Check Windows and third-party firewall settings.
- If multiple adapters are active, use `--advertise` with the correct address.
- Power-cycle the TV after changing network or server settings.

### A file appears but does not play

The server streams original files and does not transcode them. Playback depends
on the codecs and container formats supported by the receiving device. A file
that works in VLC may still use a video, audio, or subtitle codec unsupported by
a particular TV.

### The console shows `206 Partial Content`

This is normal. It means the player requested and received part of a file using
HTTP range streaming.

## Tests

Run the test suite with:

```powershell
python -m unittest discover -s . -v
```

## Security

The server has no authentication or encryption. Anyone able to access it on the
local network can browse and stream files inside the shared folder. Run it only
on a trusted network and share only the directory you intend to expose.

## Limitations

- No transcoding or codec conversion
- No cover-art or thumbnail generation
- No persistent media index or search database
- Time-based duration detection is currently limited to Matroska and WebM
- Intended for trusted LAN use, not exposure to the internet

## License

This project is licensed under the [MIT License](LICENSE).

Copyright © 2026 Adrian Crimu.
