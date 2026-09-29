"""On-demand NAT check with RFC 5389 STUN binding requests (standard library only).

Used by ``GET /api/v1/webrtc/diagnostics`` and nothing else: it never runs on
a timer, so this device does not contact the internet in the background.

All requests go out from ONE local UDP port. Comparing the public address each
STUN server reports tells how the store router maps that port:

* the same public ip:port for every server -> ``endpoint_independent``
  (hole punching with ephemeral ports works without any router change);
* different public ports -> ``endpoint_dependent`` (symmetric NAT: a direct
  connection usually needs a forwarded UDP port, i.e. ``fixed_port`` mode);
* fewer than two answers from different server addresses -> ``unknown``.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import time
from typing import Any, Optional

MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
ATTR_MAPPED_ADDRESS = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_XOR_MAPPED_ADDRESS_OLD = 0x8020
# Only for the diagnostic, when the operator configured a single STUN server:
# a second, independent server is needed to classify the NAT mapping. The
# first of these that is not already configured is added.
FALLBACK_SERVERS = ("stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478")
FALLBACK_SECOND_SERVER = FALLBACK_SERVERS[0]

ENDPOINT_INDEPENDENT = "endpoint_independent"
ENDPOINT_DEPENDENT = "endpoint_dependent"
UNKNOWN = "unknown"


def parse_stun_url(url: str) -> tuple[str, int]:
    body = url.strip()
    if body.lower().startswith("stun:"):
        body = body[5:]
    if body.startswith("["):
        host, _, rest = body[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else 3478
        return host, port
    host, _, port_txt = body.partition(":")
    return host, int(port_txt) if port_txt else 3478


def build_binding_request(txid: bytes) -> bytes:
    return struct.pack("!HHI", BINDING_REQUEST, 0, MAGIC_COOKIE) + txid


def parse_binding_response(data: bytes, txid: bytes) -> Optional[tuple[str, int]]:
    """(ip, port) from a Binding Success Response for ``txid``; None otherwise."""
    if len(data) < 20:
        return None
    msg_type, length, cookie = struct.unpack("!HHI", data[:8])
    if msg_type != BINDING_SUCCESS or cookie != MAGIC_COOKIE or data[8:20] != txid:
        return None
    body = data[20:20 + length]
    mapped: Optional[tuple[str, int]] = None
    i = 0
    while i + 4 <= len(body):
        atype, alen = struct.unpack("!HH", body[i:i + 4])
        value = body[i + 4:i + 4 + alen]
        i += 4 + alen + ((4 - alen % 4) % 4)
        if len(value) < 8:
            continue
        family = value[1]
        port = struct.unpack("!H", value[2:4])[0]
        if atype in (ATTR_XOR_MAPPED_ADDRESS, ATTR_XOR_MAPPED_ADDRESS_OLD):
            port ^= MAGIC_COOKIE >> 16
            if family == 0x01:
                raw = struct.unpack("!I", value[4:8])[0] ^ MAGIC_COOKIE
                return str(ipaddress.IPv4Address(raw)), port
            if family == 0x02 and len(value) >= 20:
                key = struct.pack("!I", MAGIC_COOKIE) + txid
                raw6 = bytes(a ^ b for a, b in zip(value[4:20], key))
                return str(ipaddress.IPv6Address(raw6)), port
        elif atype == ATTR_MAPPED_ADDRESS and mapped is None:
            if family == 0x01:
                mapped = (str(ipaddress.IPv4Address(value[4:8])), port)
    return mapped


def _query(sock: socket.socket, server_addr: tuple[str, int], timeout: float) -> Optional[tuple[str, int]]:
    """One binding transaction with retransmissions (RFC 5389 7.2.1, shortened)."""
    txid = os.urandom(12)
    request = build_binding_request(txid)
    deadline = time.monotonic() + timeout
    rto = 0.3
    while time.monotonic() < deadline:
        try:
            sock.sendto(request, server_addr)
        except OSError:
            return None
        wait_until = min(deadline, time.monotonic() + rto)
        while True:
            remaining = wait_until - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, peer = sock.recvfrom(2048)
            except socket.timeout:
                break
            except OSError:
                return None
            if peer[0] != server_addr[0]:
                continue
            result = parse_binding_response(data, txid)
            if result:
                return result
        rto *= 2
    return None


def classify(results: list[dict[str, Any]]) -> str:
    answered = [r for r in results if r.get("mapped")]
    by_server_ip: dict[str, str] = {}
    for r in answered:
        by_server_ip.setdefault(r["server_ip"], r["mapped"])
    if len(by_server_ip) < 2:
        return UNKNOWN
    return ENDPOINT_INDEPENDENT if len(set(by_server_ip.values())) == 1 else ENDPOINT_DEPENDENT


def probe(servers: list[str], timeout: float = 2.5, add_fallback: bool = True) -> dict[str, Any]:
    """Query each STUN server from the same local UDP port. Blocking (run in a thread)."""
    urls = list(dict.fromkeys(servers))
    fallback: Optional[str] = None
    if add_fallback and len(urls) < 2:
        fallback = next((u for u in FALLBACK_SERVERS if u not in urls), None)
        if fallback:
            urls.append(fallback)
    out: dict[str, Any] = {"local_port": None, "servers": [], "mapping": UNKNOWN,
                           "public_address": None, "port_preserved": None, "fallback_server": fallback}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", 0))
        local_port = sock.getsockname()[1]
        out["local_port"] = local_port
        for url in urls:
            entry: dict[str, Any] = {"server": url, "server_ip": None, "mapped": None, "error": None,
                                     "configured": url != fallback}
            try:
                host, port = parse_stun_url(url)
                infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
                addr = infos[0][4]
                entry["server_ip"] = addr[0]
            except (OSError, ValueError, IndexError) as exc:
                entry["error"] = f"DNS lookup failed ({exc.__class__.__name__})"
                out["servers"].append(entry)
                continue
            t0 = time.monotonic()
            mapped = _query(sock, (addr[0], addr[1]), timeout)
            if mapped:
                entry["mapped"] = f"{mapped[0]}:{mapped[1]}"
                entry["rtt_ms"] = round((time.monotonic() - t0) * 1000)
            else:
                entry["error"] = "no answer (UDP blocked, or the server is down)"
            out["servers"].append(entry)
    finally:
        sock.close()
    out["mapping"] = classify(out["servers"])
    mapped = [r["mapped"] for r in out["servers"] if r.get("mapped")]
    if mapped:
        out["public_address"] = mapped[0].rsplit(":", 1)[0]
        out["port_preserved"] = all(int(m.rsplit(":", 1)[1]) == out["local_port"] for m in mapped)
    return out
