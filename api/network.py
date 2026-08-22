"""Network traffic monitoring.

Layer 1 (this module): the emulator is launched with `-tcpdump <file>`, which
writes EVERY packet it sees - TCP, UDP, DNS, ICMP, QUIC, ARP, IPv6, the lot - to
a libpcap file. Nothing here talks to the device; it reads that growing file.

The pcap is parsed in pure Python, incrementally, tracking a byte offset so each
poll only decodes records appended since the last one. Records are emitted only
when complete, so a half-written trailing packet is simply picked up next time.

TLS payloads are of course encrypted at this layer. Decrypted HTTP lives in
`mitm.py` (layer 2).
"""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from api.config import settings

# pcap global header magics -> (byte order, timestamps-are-nanoseconds)
_MAGICS: dict[int, tuple[str, bool]] = {
    0xA1B2C3D4: ("<", False),  # microsecond, little-endian host wrote it
    0xD4C3B2A1: (">", False),  # microsecond, opposite endianness
    0xA1B23C4D: ("<", True),  # nanosecond
    0x4D3CB2A1: (">", True),  # nanosecond
}

# libpcap link-layer types we know how to unwrap down to IP.
_DLT_NULL = 0
_DLT_EN10MB = 1
_DLT_RAW = 101
_DLT_LINUX_SLL = 113
_DLT_LINUX_SLL2 = 276

_IP_PROTO = {1: "ICMP", 2: "IGMP", 6: "TCP", 17: "UDP", 58: "ICMPv6", 132: "SCTP"}
_GLOBAL_HEADER_LEN = 24
_RECORD_HEADER_LEN = 16


@dataclass(frozen=True, slots=True)
class Packet:
    """One decoded frame, flattened to the fields a monitor actually wants."""

    index: int
    timestamp: float
    length: int
    l3: str  # IPv4 / IPv6 / ARP / non-IP
    protocol: str  # TCP / UDP / DNS / ICMP / ...
    src: str
    dst: str
    src_port: int | None
    dst_port: int | None
    info: str

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "timestamp": round(self.timestamp, 6),
            "length": self.length,
            "l3": self.l3,
            "protocol": self.protocol,
            "src": self.src,
            "dst": self.dst,
            "src_port": self.src_port,
            "dst_port": self.dst_port,
            "info": self.info,
        }


@dataclass(slots=True)
class PcapCursor:
    """Remembers where we are in the growing capture file."""

    byte_order: str = "<"
    nanoseconds: bool = False
    linktype: int = _DLT_EN10MB
    header_parsed: bool = False
    offset: int = 0  # next unread byte (always at a record boundary)
    index: int = 0  # packets emitted so far


class CaptureError(RuntimeError):
    """Something went wrong reading the capture file."""


def capture_path() -> Path:
    return Path(settings.capture_file)


def capture_available() -> bool:
    return settings.capture_traffic and capture_path().is_file()


def capture_size() -> int:
    path = capture_path()
    return path.stat().st_size if path.is_file() else 0


def _read_global_header(fh: BinaryIO, cursor: PcapCursor) -> None:
    fh.seek(0)
    header = fh.read(_GLOBAL_HEADER_LEN)
    if len(header) < _GLOBAL_HEADER_LEN:
        raise CaptureError("capture file has no complete pcap header yet")

    magic = struct.unpack("<I", header[:4])[0]
    if magic not in _MAGICS:
        magic_be = struct.unpack(">I", header[:4])[0]
        if magic_be not in _MAGICS:
            raise CaptureError(f"not a pcap file (magic {magic:#010x})")
        magic = magic_be

    cursor.byte_order, cursor.nanoseconds = _MAGICS[magic]
    cursor.linktype = struct.unpack(f"{cursor.byte_order}I", header[20:24])[0]
    cursor.header_parsed = True
    if cursor.offset < _GLOBAL_HEADER_LEN:
        cursor.offset = _GLOBAL_HEADER_LEN


def read_new_packets(cursor: PcapCursor, *, limit: int | None = None) -> list[Packet]:
    """Decode every complete record appended since `cursor.offset`.

    Mutates the cursor in place. Safe to call repeatedly on a file that another
    process is still appending to.
    """
    path = capture_path()
    if not path.is_file():
        return []

    packets: list[Packet] = []
    with path.open("rb") as fh:
        if not cursor.header_parsed:
            _read_global_header(fh, cursor)

        fh.seek(cursor.offset)
        endian = cursor.byte_order
        while True:
            if limit is not None and len(packets) >= limit:
                break
            record_header = fh.read(_RECORD_HEADER_LEN)
            if len(record_header) < _RECORD_HEADER_LEN:
                break  # partial trailing record; try again next poll
            ts_sec, ts_frac, incl_len, _orig_len = struct.unpack(f"{endian}IIII", record_header)
            payload = fh.read(incl_len)
            if len(payload) < incl_len:
                break  # record body not fully written yet

            timestamp = ts_sec + ts_frac / (1_000_000_000 if cursor.nanoseconds else 1_000_000)
            packet = _decode(cursor.index, timestamp, incl_len, cursor.linktype, payload)
            packets.append(packet)
            cursor.index += 1
            cursor.offset += _RECORD_HEADER_LEN + incl_len

    return packets


def _decode(index: int, ts: float, length: int, linktype: int, data: bytes) -> Packet:
    l3, ip_proto, src, dst, rest = _strip_link_layer(linktype, data)

    if ip_proto is None:
        return Packet(index, ts, length, l3, l3, src, dst, None, None, l3)

    proto = _IP_PROTO.get(ip_proto, f"IP/{ip_proto}")
    src_port = dst_port = None
    info = proto

    if ip_proto in (6, 17) and len(rest) >= 4:  # TCP or UDP
        src_port, dst_port = struct.unpack(">HH", rest[:4])
        if ip_proto == 6:
            proto, info = _tcp(rest, src_port, dst_port)
        else:
            proto, info = _udp(rest, src_port, dst_port)

    return Packet(index, ts, length, l3, proto, src, dst, src_port, dst_port, info)


def _strip_link_layer(linktype: int, data: bytes) -> tuple[str, int | None, str, str, bytes]:
    """Return (l3_name, ip_protocol|None, src, dst, l4_bytes)."""
    if linktype == _DLT_EN10MB:
        if len(data) < 14:
            return "non-IP", None, "", "", b""
        ethertype = struct.unpack(">H", data[12:14])[0]
        return _strip_l3(ethertype, data[14:])
    if linktype == _DLT_RAW:
        return _strip_l3(_ethertype_from_version(data), data)
    if linktype == _DLT_NULL:
        return _strip_l3(0x0800 if data[:4] in (b"\x02\x00\x00\x00",) else 0x86DD, data[4:])
    if linktype == _DLT_LINUX_SLL:
        if len(data) < 16:
            return "non-IP", None, "", "", b""
        return _strip_l3(struct.unpack(">H", data[14:16])[0], data[16:])
    if linktype == _DLT_LINUX_SLL2:
        if len(data) < 20:
            return "non-IP", None, "", "", b""
        return _strip_l3(struct.unpack(">H", data[0:2])[0], data[20:])
    # Unknown link type: best-effort guess it is raw IP.
    return _strip_l3(_ethertype_from_version(data), data)


def _ethertype_from_version(data: bytes) -> int:
    if not data:
        return 0
    version = data[0] >> 4
    return 0x0800 if version == 4 else 0x86DD if version == 6 else 0


def _strip_l3(ethertype: int, data: bytes) -> tuple[str, int | None, str, str, bytes]:
    if ethertype == 0x0800:  # IPv4
        if len(data) < 20:
            return "IPv4", None, "", "", b""
        ihl = (data[0] & 0x0F) * 4
        proto = data[9]
        src = str(ipaddress.IPv4Address(data[12:16]))
        dst = str(ipaddress.IPv4Address(data[16:20]))
        return "IPv4", proto, src, dst, data[ihl:]
    if ethertype == 0x86DD:  # IPv6
        if len(data) < 40:
            return "IPv6", None, "", "", b""
        proto = data[6]  # next header (skips extension headers, good enough)
        src = str(ipaddress.IPv6Address(data[8:24]))
        dst = str(ipaddress.IPv6Address(data[24:40]))
        return "IPv6", proto, src, dst, data[40:]
    if ethertype == 0x0806:  # ARP
        return "ARP", None, "", "", b""
    return "non-IP", None, "", "", b""


def _tcp(seg: bytes, src_port: int, dst_port: int) -> tuple[str, str]:
    flags = seg[13] if len(seg) >= 14 else 0
    names = [
        n
        for bit, n in ((0x02, "SYN"), (0x10, "ACK"), (0x01, "FIN"), (0x04, "RST"), (0x08, "PSH"))
        if flags & bit
    ]
    data_offset = (seg[12] >> 4) * 4 if len(seg) >= 13 else 20
    payload = seg[data_offset:] if len(seg) > data_offset else b""
    app = _classify_port(src_port, dst_port)
    if (
        app == "HTTP"
        and payload[:8].isascii()
        and payload[:4]
        in (
            b"GET ",
            b"POST",
            b"PUT ",
            b"HEAD",
            b"DELE",
            b"HTTP",
            b"OPTI",
            b"PATC",
        )
    ):
        line = payload.split(b"\r\n", 1)[0].decode("latin-1", "replace")[:120]
        return "HTTP", line
    label = app or "TCP"
    return label, f"{label} [{','.join(names) or 'data'}]"


def _udp(seg: bytes, src_port: int, dst_port: int) -> tuple[str, str]:
    if src_port == 53 or dst_port == 53:
        return "DNS", _dns_summary(seg[8:])
    if src_port in (443, 80) or dst_port in (443, 80):
        return "QUIC", "QUIC/UDP"
    app = _classify_port(src_port, dst_port)
    label = app or "UDP"
    return label, label


def _classify_port(src_port: int, dst_port: int) -> str | None:
    ports = {src_port, dst_port}
    if 80 in ports or 8080 in ports:
        return "HTTP"
    if 443 in ports:
        return "TLS"
    if 53 in ports:
        return "DNS"
    return None


def _dns_summary(payload: bytes) -> str:
    if len(payload) < 12:
        return "DNS"
    qd = struct.unpack(">H", payload[4:6])[0]
    if qd == 0:
        return "DNS"
    labels, i = [], 12
    for _ in range(64):
        if i >= len(payload):
            break
        n = payload[i]
        if n == 0:
            break
        labels.append(payload[i + 1 : i + 1 + n].decode("latin-1", "replace"))
        i += n + 1
    name = ".".join(labels)
    return f"DNS {name}" if name else "DNS"


@dataclass(slots=True)
class CaptureState:
    """Process-wide capture bookkeeping shared across requests."""

    baseline_offset: int = _GLOBAL_HEADER_LEN
    baseline_index: int = 0
    stats_cursor: PcapCursor = field(default_factory=PcapCursor)
    totals: dict[str, int] = field(default_factory=dict)
    by_protocol: dict[str, int] = field(default_factory=dict)
    by_host: dict[str, int] = field(default_factory=dict)


state = CaptureState()
