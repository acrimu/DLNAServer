# Simple DLNA Server

A small dependency-free DLNA/UPnP media server for Python 3. It advertises a
folder on your local network and supports folder browsing and byte-range media
streaming.

## Run

```powershell
python .\dlna_server.py "D:\Media"
```

Then open the media-source list on a TV, console, VLC, or another DLNA player
and select `DLNA on <computer-name>`.

Options:

```text
--name NAME          Display name shown to players
--port PORT          HTTP port (default: 8200)
--advertise ADDRESS  This computer's LAN IPv4 address
--host ADDRESS       HTTP bind address (default: all interfaces)
```

If automatic address detection chooses the wrong adapter, find the computer's
IPv4 address with `ipconfig` and pass it with `--advertise`. Windows Firewall
may ask you to allow Python on private networks; discovery and streaming require
that access. Keep the server on a trusted home network: it has no authentication.
