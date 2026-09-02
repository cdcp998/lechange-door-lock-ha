"""Pure helpers for camera stream URLs (no Home Assistant imports).

Dahua/Imou RTSP URI convention:
    rtsp://<user>:<pass>@<host>:<port>/cam/realmonitor?channel=<N>&subtype=<0|1>
"""

from __future__ import annotations

import re

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def split_host_port(host: str, default_port: int = 554) -> tuple[str, int]:
    """Split 'host' into (host, port); handles 'host:port' and '<ipv4>'."""
    host = (host or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    if ":" in host:
        name, _, port = host.rpartition(":")
        if port.isdigit():
            return name, int(port)
    return host, default_port


def is_lan_host(host: str) -> bool:
    """True when the host is a plain IPv4 LAN address (snapshot CGI applies)."""
    host = (host or "").strip()
    if ":" in host and not _IP_RE.match(host.split(":")[0]):
        return False
    return bool(_IP_RE.match(host.split(":")[0]))


def build_rtsp_url(
    host: str,
    port: int,
    username: str,
    password: str,
    channel_id: int | str,
    subtype: int = 0,
) -> str:
    """Build a Dahua-style RTSP URL for one camera channel.

    RTSP channel numbers are one-based (channel 0 -> channel=1).
    """
    host = (host or "").strip()
    if not host:
        return ""
    cred = ""
    if username:
        user = username.strip()
        pw = str(password or "").strip()
        import urllib.parse

        cred = f"{urllib.parse.quote(user)}:{urllib.parse.quote(pw)}@"
    ch = int(channel_id) + 1
    return (
        f"rtsp://{cred}{host}:{port}/cam/realmonitor"
        f"?channel={ch}&subtype={int(subtype)}"
    )
