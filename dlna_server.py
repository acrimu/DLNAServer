"""Small, dependency-free UPnP/DLNA media server."""

from __future__ import annotations

import argparse
import email.utils
import functools
import html
import mimetypes
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
import uuid as uuidlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET


SSDP_ADDR = ("239.255.255.250", 1900)
CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
DLNA_NS = "urn:schemas-dlna-org:device-1-0"


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def mime_for(path: Path) -> str:
    overrides = {".mkv": "video/x-matroska", ".m4v": "video/mp4", ".flac": "audio/flac"}
    return overrides.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _ebml_value(data: bytes, marker: bytes):
    """Return the payload following the first matching EBML element ID."""
    pos = data.find(marker)
    if pos < 0:
        return None
    pos += len(marker)
    if pos >= len(data):
        return None
    first = data[pos]
    mask, width = 0x80, 1
    while width <= 8 and not first & mask:
        mask >>= 1
        width += 1
    if width > 8 or pos + width > len(data):
        return None
    size = first & (mask - 1)
    for byte in data[pos + 1:pos + width]:
        size = (size << 8) | byte
    start = pos + width
    return data[start:start + size] if start + size <= len(data) else None


@functools.lru_cache(maxsize=1024)
def _duration_cached(filename: str, size: int, mtime_ns: int):
    path = Path(filename)
    if path.suffix.lower() not in (".mkv", ".webm"):
        return None
    try:
        with path.open("rb") as file:
            data = file.read(16 * 1024 * 1024)
        raw_duration = _ebml_value(data, b"\x44\x89")
        raw_scale = _ebml_value(data, b"\x2a\xd7\xb1")
        if not raw_duration or len(raw_duration) not in (4, 8):
            return None
        duration_units = struct.unpack(">f" if len(raw_duration) == 4 else ">d", raw_duration)[0]
        scale = int.from_bytes(raw_scale, "big") if raw_scale else 1_000_000
        seconds = duration_units * scale / 1_000_000_000
        return seconds if 0 < seconds < 30 * 24 * 3600 else None
    except (OSError, ValueError, struct.error):
        return None


def media_duration(path: Path):
    stat = path.stat()
    return _duration_cached(str(path), stat.st_size, stat.st_mtime_ns)


def format_npt(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"


def parse_npt(value: str) -> float:
    value = value.strip()
    if ":" not in value:
        return float(value)
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError("invalid NPT")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


DLNA_FEATURES = "DLNA.ORG_OP=11;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


class Library:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"Not a directory: {root}")

    def encode(self, path: Path) -> str:
        if path == self.root:
            return "0"
        return urllib.parse.quote(path.relative_to(self.root).as_posix(), safe="")

    def decode(self, object_id: str) -> Path:
        rel = "" if object_id == "0" else urllib.parse.unquote(object_id)
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Invalid object id")
        return candidate

    def children(self, path: Path) -> list[Path]:
        try:
            return sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except (OSError, PermissionError):
            return []


class DLNAServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, library: Library, name: str, uuid: str, advertised_ip: str):
        super().__init__(address, Handler)
        self.library, self.friendly_name, self.uuid = library, name, uuid
        self.advertised_ip = advertised_ip

    @property
    def base_url(self):
        return f"http://{self.advertised_ip}:{self.server_port}"

    def handle_error(self, request, client_address):
        """Ignore normal DLNA client disconnects, report everything else."""
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server: DLNAServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_bytes(self, data: bytes, content_type: str, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/description.xml":
            return self.send_bytes(device_description(self.server).encode(), "text/xml; charset=utf-8")
        if path == "/ContentDirectory.xml":
            return self.send_bytes(scpd_content().encode(), "text/xml; charset=utf-8")
        if path == "/ConnectionManager.xml":
            return self.send_bytes(scpd_connection().encode(), "text/xml; charset=utf-8")
        if path.startswith("/media/"):
            try:
                target = self.server.library.decode(urllib.parse.unquote(path[7:]))
                if not target.is_file():
                    raise FileNotFoundError
            except (ValueError, FileNotFoundError, PermissionError):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            return self.stream_file(target)
        self.send_error(HTTPStatus.NOT_FOUND)

    do_HEAD = do_GET

    def stream_file(self, path: Path):
        size, start, end = path.stat().st_size, 0, path.stat().st_size - 1
        status = HTTPStatus.OK
        requested = self.headers.get("Range")
        time_seek = self.headers.get("TimeSeekRange.dlna.org")
        duration = media_duration(path)
        seek_start_seconds = None
        if time_seek and duration:
            try:
                spec = time_seek.split("=", 1)[1].split("/", 1)[0]
                seek_start_seconds = parse_npt(spec.split("-", 1)[0])
                if seek_start_seconds < 0 or seek_start_seconds >= duration:
                    raise ValueError
                start = min(size - 1, int(size * seek_start_seconds / duration))
                status = HTTPStatus.PARTIAL_CONTENT
                requested = None
            except (ValueError, IndexError):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if requested:
            try:
                unit, spec = requested.split("=", 1)
                if unit != "bytes" or "," in spec:
                    raise ValueError
                left, right = spec.split("-", 1)
                if left:
                    start = int(left)
                    end = int(right) if right else end
                else:
                    length = int(right)
                    start = max(0, size - length)
                if start < 0 or end < start or start >= size:
                    raise ValueError
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(status)
        self.send_header("Content-Type", mime_for(path))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", DLNA_FEATURES)
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if duration:
            actual_start = seek_start_seconds if seek_start_seconds is not None else duration * start / size
            actual_end = duration * (end + 1) / size
            self.send_header("TimeSeekRange.dlna.org", f"npt={format_npt(actual_start)}-{format_npt(actual_end)}/{format_npt(duration)}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # DLNA players commonly open overlapping byte-range probes
                    # and close the ones they no longer need. This is normal.
                    break
                remaining -= len(chunk)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        action = self.headers.get("SOAPACTION", "").strip('"').split("#")[-1]
        try:
            if self.path == "/control/content" and action == "Browse":
                args = soap_args(body)
                response = browse_response(self.server, args)
            elif self.path == "/control/content" and action == "GetSearchCapabilities":
                response = soap_response(CDS, action, {"SearchCaps": ""})
            elif self.path == "/control/content" and action == "GetSortCapabilities":
                response = soap_response(CDS, action, {"SortCaps": "dc:title"})
            elif self.path == "/control/content" and action == "GetSystemUpdateID":
                response = soap_response(CDS, action, {"Id": "1"})
            elif self.path == "/control/connection" and action == "GetProtocolInfo":
                response = soap_response(CMS, action, {"Source": "", "Sink": "http-get:*:*:*"})
            elif self.path == "/control/connection" and action == "GetCurrentConnectionIDs":
                response = soap_response(CMS, action, {"ConnectionIDs": "0"})
            elif self.path == "/control/connection" and action == "GetCurrentConnectionInfo":
                response = soap_response(CMS, action, {
                    "RcsID": "-1", "AVTransportID": "-1", "ProtocolInfo": "",
                    "PeerConnectionManager": "", "PeerConnectionID": "-1",
                    "Direction": "Output", "Status": "OK",
                })
            else:
                return self.soap_fault(401, "Invalid Action")
            self.send_bytes(response.encode(), 'text/xml; charset="utf-8"', extra={"EXT": ""})
        except (ET.ParseError, ValueError, FileNotFoundError):
            self.soap_fault(701, "No Such Object")

    def soap_fault(self, code, description):
        xml = f'''<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>{code}</errorCode><errorDescription>{description}</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>'''
        self.send_bytes(xml.encode(), "text/xml; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_SUBSCRIBE(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("SID", "uuid:dlna-event-subscription")
        self.send_header("TIMEOUT", "Second-1800")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_UNSUBSCRIBE = do_SUBSCRIBE


def device_description(server: DLNAServer) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="{DLNA_NS}"><specVersion><major>1</major><minor>0</minor></specVersion><URLBase>{server.base_url}/</URLBase><device><deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType><friendlyName>{html.escape(server.friendly_name)}</friendlyName><manufacturer>Codex</manufacturer><manufacturerURL>https://openai.com/</manufacturerURL><modelDescription>UPnP AV Media Server</modelDescription><modelName>Python DLNA Server</modelName><modelNumber>1.0</modelNumber><serialNumber>{server.uuid[:12]}</serialNumber><UDN>uuid:{server.uuid}</UDN><dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC><serviceList>
<service><serviceType>{CDS}</serviceType><serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId><SCPDURL>/ContentDirectory.xml</SCPDURL><controlURL>/control/content</controlURL><eventSubURL>/event/content</eventSubURL></service>
<service><serviceType>{CMS}</serviceType><serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId><SCPDURL>/ConnectionManager.xml</SCPDURL><controlURL>/control/connection</controlURL><eventSubURL>/event/connection</eventSubURL></service>
</serviceList></device></root>'''


def scpd_content():
    browse_args = ''.join(f'<argument><name>{n}</name><direction>{d}</direction><relatedStateVariable>{var}</relatedStateVariable></argument>' for n,d,var in [
        ("ObjectID","in","A_ARG_TYPE_ObjectID"),("BrowseFlag","in","A_ARG_TYPE_BrowseFlag"),("Filter","in","A_ARG_TYPE_Filter"),("StartingIndex","in","A_ARG_TYPE_Index"),("RequestedCount","in","A_ARG_TYPE_Count"),("SortCriteria","in","A_ARG_TYPE_SortCriteria"),("Result","out","A_ARG_TYPE_Result"),("NumberReturned","out","A_ARG_TYPE_Count"),("TotalMatches","out","A_ARG_TYPE_Count"),("UpdateID","out","SystemUpdateID")])
    actions = f'''<action><name>Browse</name><argumentList>{browse_args}</argumentList></action>
<action><name>GetSearchCapabilities</name><argumentList><argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument></argumentList></action>
<action><name>GetSortCapabilities</name><argumentList><argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument></argumentList></action>
<action><name>GetSystemUpdateID</name><argumentList><argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument></argumentList></action>'''
    variables = ''.join(f'<stateVariable sendEvents="no"><name>{name}</name><dataType>{dtype}</dataType></stateVariable>' for name,dtype in [
        ("A_ARG_TYPE_ObjectID","string"),("A_ARG_TYPE_BrowseFlag","string"),("A_ARG_TYPE_Filter","string"),("A_ARG_TYPE_Index","ui4"),("A_ARG_TYPE_Count","ui4"),("A_ARG_TYPE_SortCriteria","string"),("A_ARG_TYPE_Result","string"),("SearchCapabilities","string"),("SortCapabilities","string")])
    return f'''<?xml version="1.0"?><scpd xmlns="urn:schemas-upnp-org:service-1-0"><specVersion><major>1</major><minor>0</minor></specVersion><actionList>{actions}</actionList><serviceStateTable>{variables}<stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable></serviceStateTable></scpd>'''


def scpd_connection():
    return '''<?xml version="1.0"?><scpd xmlns="urn:schemas-upnp-org:service-1-0"><specVersion><major>1</major><minor>0</minor></specVersion><actionList><action><name>GetProtocolInfo</name></action><action><name>GetCurrentConnectionIDs</name></action><action><name>GetCurrentConnectionInfo</name></action></actionList><serviceStateTable/></scpd>'''


def soap_args(body: bytes) -> dict[str, str]:
    root = ET.fromstring(body)
    action = next(iter(next(e for e in root if e.tag.endswith("Body"))))
    return {node.tag.split("}")[-1]: node.text or "" for node in action}


def didl_entry(server: DLNAServer, path: Path) -> str:
    lib, oid = server.library, server.library.encode(path)
    parent = "-1" if path == lib.root else "0" if path.parent == lib.root else lib.encode(path.parent)
    title = html.escape(path.name)
    if path.is_dir():
        return f'<container id="{oid}" parentID="{parent}" restricted="1" childCount="{len(lib.children(path))}"><dc:title>{title}</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>'
    mime = mime_for(path)
    kind = "videoItem" if mime.startswith("video/") else "audioItem.musicTrack" if mime.startswith("audio/") else "imageItem.photo" if mime.startswith("image/") else "item"
    url = f"{server.base_url}/media/{urllib.parse.quote(oid, safe='')}"
    duration = media_duration(path)
    duration_attr = f' duration="{format_npt(duration)}"' if duration else ""
    protocol = f"http-get:*:{mime}:{DLNA_FEATURES}"
    return f'<item id="{oid}" parentID="{parent}" restricted="1"><dc:title>{title}</dc:title><upnp:class>object.item.{kind}</upnp:class><res protocolInfo="{protocol}" size="{path.stat().st_size}"{duration_attr}>{html.escape(url)}</res></item>'


def browse_response(server: DLNAServer, args: dict[str, str]) -> str:
    target = server.library.decode(args.get("ObjectID", "0"))
    if not target.exists():
        raise FileNotFoundError
    metadata = args.get("BrowseFlag") == "BrowseMetadata"
    entries = [target] if metadata else server.library.children(target)
    total = len(entries)
    if not metadata:
        start = max(0, int(args.get("StartingIndex", "0")))
        count = max(0, int(args.get("RequestedCount", "0")))
        entries = entries[start:] if count == 0 else entries[start:start + count]
    didl = '<?xml version="1.0"?><DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">' + ''.join(didl_entry(server, p) for p in entries) + '</DIDL-Lite>'
    return soap_response(CDS, "Browse", {"Result": didl, "NumberReturned": str(len(entries)), "TotalMatches": str(total), "UpdateID": "1"})


def soap_response(service: str, action: str, values: dict[str, str]) -> str:
    fields = "".join(f"<{k}>{html.escape(v)}</{k}>" for k, v in values.items())
    return f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action}Response xmlns:u="{service}">{fields}</u:{action}Response></s:Body></s:Envelope>'


class SSDP(threading.Thread):
    def __init__(self, server: DLNAServer, debug=False):
        super().__init__(daemon=True)
        self.server, self.running, self.debug = server, True, debug
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", 1900))
        interface = socket.inet_aton(server.advertised_ip)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(SSDP_ADDR[0]), interface))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, interface)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self.sock.settimeout(1)

    def run(self):
        self.notify("ssdp:alive")
        last = time.monotonic()
        while self.running:
            try:
                data, address = self.sock.recvfrom(65535)
                text = data.decode(errors="ignore")
                if text.startswith("M-SEARCH") and "ssdp:discover" in text.lower():
                    if self.debug:
                        st = next((line.split(":", 1)[1].strip() for line in text.splitlines() if line.upper().startswith("ST:")), "unknown")
                        print(f"[SSDP] Search from {address[0]}:{address[1]} for {st}")
                    self.reply(address, text)
            except socket.timeout:
                pass
            if time.monotonic() - last > 300:
                self.notify("ssdp:alive")
                last = time.monotonic()

    def targets(self):
        uid = f"uuid:{self.server.uuid}"
        return [("upnp:rootdevice", f"{uid}::upnp:rootdevice"), (uid, uid), ("urn:schemas-upnp-org:device:MediaServer:1", f"{uid}::urn:schemas-upnp-org:device:MediaServer:1"), (CDS, f"{uid}::{CDS}"), (CMS, f"{uid}::{CMS}")]

    def reply(self, address, request):
        st_line = next((x for x in request.splitlines() if x.upper().startswith("ST:")), "ST:ssdp:all")
        wanted = st_line.split(":", 1)[1].strip()
        for st, usn in self.targets():
            if wanted not in ("ssdp:all", st):
                continue
            msg = f"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=900\r\nDATE: {email.utils.formatdate(usegmt=True)}\r\nEXT:\r\nLOCATION: {self.server.base_url}/description.xml\r\nSERVER: Windows/11 UPnP/1.0 DLNADOC/1.50 PythonDLNA/1.0\r\nST: {st}\r\nUSN: {usn}\r\nCONTENT-LENGTH: 0\r\n\r\n"
            self.sock.sendto(msg.encode(), address)

    def notify(self, nts):
        for nt, usn in self.targets():
            msg = f"NOTIFY * HTTP/1.1\r\nHOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\nCACHE-CONTROL: max-age=900\r\nLOCATION: {self.server.base_url}/description.xml\r\nNT: {nt}\r\nNTS: {nts}\r\nSERVER: Windows/11 UPnP/1.0 DLNADOC/1.50 PythonDLNA/1.0\r\nUSN: {usn}\r\nCONTENT-LENGTH: 0\r\n\r\n"
            self.sock.sendto(msg.encode(), SSDP_ADDR)

    def close(self):
        self.notify("ssdp:byebye")
        self.running = False
        self.sock.close()


def main():
    parser = argparse.ArgumentParser(description="Share a folder with DLNA/UPnP players")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--name", default=f"DLNA on {socket.gethostname()}")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    parser.add_argument("--advertise", default=local_ip(), help="LAN IPv4 address advertised to players")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--debug-discovery", action="store_true", help="log incoming SSDP searches")
    args = parser.parse_args()
    identity = f"python-dlna-v2:{socket.gethostname()}:{args.folder.resolve()}"
    uid = str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, identity))
    server = DLNAServer((args.host, args.port), Library(args.folder), args.name, uid, args.advertise)
    try:
        discovery = SSDP(server, args.debug_discovery)
    except OSError as error:
        server.server_close()
        raise SystemExit(f"Could not start SSDP discovery on {args.advertise}: {error}")
    discovery.start()
    print(f'Sharing "{server.library.root}" as "{args.name}" at {server.base_url}')
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        discovery.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
