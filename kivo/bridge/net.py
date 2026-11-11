"""Network interface discovery and binding validation for Phone Bridge."""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Optional

import psutil

_EXCLUDED_IFACE_PATTERNS = re.compile(
    r"^(?:lo|docker\d*|br-[0-9a-f]+|veth[0-9a-f]+|virbr\d*|tun\d*|tap\d*|wg\d*)$",
    re.IGNORECASE,
)


def discover_lan_interface() -> tuple[str, str] | None:
    """Discover the active physical LAN interface and its IPv4 address.

    Explicitly excludes loopback, docker bridges, and virtual container interfaces.

    Returns:
        (interface_name, ipv4_address) or None if no physical LAN interface is active.
    """
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()

    # Step 1: Probe default route interface via routing socket
    default_iface_ip: str | None = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Does not actually transmit packets; resolves routing table
        s.connect(("8.8.8.8", 80))
        default_iface_ip = s.getsockname()[0]
        s.close()
    except OSError:
        default_iface_ip = None

    candidates: list[tuple[str, str, int]] = []

    for iface, addr_list in addrs.items():
        if _EXCLUDED_IFACE_PATTERNS.match(iface):
            continue

        stat = stats.get(iface)
        if not stat or not stat.isup:
            continue

        for addr in addr_list:
            if addr.family != socket.AF_INET:
                continue
            ip_str = addr.address
            try:
                ip_obj = ipaddress.IPv4Address(ip_str)
            except ValueError:
                continue

            if ip_obj.is_loopback or ip_obj.is_link_local:
                continue
            if not ip_obj.is_private:
                continue

            # Priority scoring: default route match = 100, wireless = 50, ethernet = 40
            score = 10
            if ip_str == default_iface_ip:
                score = 100
            elif iface.startswith("wl"):
                score = 50
            elif iface.startswith("en") or iface.startswith("eth"):
                score = 40

            candidates.append((iface, ip_str, score))

    if not candidates:
        return None

    # Sort descending by priority score
    candidates.sort(key=lambda c: c[2], reverse=True)
    best = candidates[0]
    return best[0], best[1]


def is_port_available(ip: str, port: int) -> bool:
    """Check if a TCP port can be bound on the specified IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((ip, port))
        s.close()
        return True
    except OSError:
        return False


def find_available_port(ip: str, preferred: int = 8443) -> int:
    """Return preferred port if free, otherwise let OS assign an ephemeral port."""
    if is_port_available(ip, preferred):
        return preferred

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((ip, 0))
    port = s.getsockname()[1]
    s.close()
    return port


__all__ = [
    "discover_lan_interface",
    "is_port_available",
    "find_available_port",
]
