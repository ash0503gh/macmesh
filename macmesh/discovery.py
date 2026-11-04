"""Bonjour (mDNS) advertisement and browsing: zero-config, like AirDrop.

A Mac can have several addresses (Wi-Fi, Ethernet, a VPN, a virtual machine's bridge), and only
some are reachable from another Mac. So every address is advertised, the one this Mac uses to
reach the local network first, and browsing returns them all for the caller to try in turn.

Each address is also labelled with its kind of link ("links": wired or wifi). A cable (Ethernet,
or a Thunderbolt cable between two Macs) is many times faster than Wi-Fi through a router, so
when this Mac has a wired link too, another Mac's wired addresses are tried first.
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import time

from . import SERVICE_TYPE, __version__


def _primary_ip() -> str | None:
    """The address this Mac uses to reach the local network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("224.0.0.251", 5353))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def link_kinds() -> dict[str, str]:
    """This Mac's IPv4 addresses by kind of link: "wired" (Ethernet, Thunderbolt) or "wifi"."""
    try:
        ports = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True,
                               timeout=5).stdout
        ifconfig = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    kind, port = {}, ""
    for line in ports.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:"):
            name = port.lower()
            kind[line.split(":", 1)[1].strip()] = ("wifi" if "wi-fi" in name else
                                                    "wired" if any(w in name for w in ("ethernet", "thunderbolt", "lan"))
                                                    else "other")
    found, device = {}, ""
    for line in ifconfig.splitlines():
        if line and not line[0].isspace():
            device = line.split(":", 1)[0]
        elif line.strip().startswith("inet ") and device in kind:
            found[line.split()[1]] = kind[device]
    return found


def _local_ips() -> list[bytes]:
    ips = set()
    try:
        for fam, _, _, _, addr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(addr[0])
    except socket.gaierror:
        pass
    primary = _primary_ip()
    if primary:
        ips.add(primary)
    ips.discard("127.0.0.1")
    ordered = sorted(ips, key=lambda ip: (ip != primary, ip))
    return [socket.inet_aton(ip) for ip in ordered] or [socket.inet_aton("127.0.0.1")]


class Advertisement:
    def __init__(self, zc, info):
        self.zc, self.info = zc, info

    def close(self) -> None:
        self.zc.unregister_service(self.info)
        self.zc.close()


def advertise(cfg: dict, service_type: str = SERVICE_TYPE, port: int | None = None,
              properties: dict | None = None) -> Advertisement | None:
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        print("zeroconf not installed: not advertising (peers must be added by address)")
        return None
    name = f"{cfg['node_name']}-{cfg['node_id'][:6]}.{service_type}"
    info = ServiceInfo(
        service_type, name, addresses=_local_ips(), port=int(port or cfg["port"]),
        properties={"node_id": cfg["node_id"], "name": cfg["node_name"], "v": __version__,
                    "links": ",".join(f"{ip}={k}" for ip, k in link_kinds().items() if k != "other")[:240],
                    **(properties or {})},
        server=f"{cfg['node_id']}.local.",
    )
    zc = Zeroconf()
    zc.register_service(info)
    return Advertisement(zc, info)


def _rank(ip: str, primary: str | None) -> tuple:
    """Addresses on this Mac's own network first, then other private ones, link-local last."""
    try:
        addr = ipaddress.ip_address(ip)
        same = primary is not None and ipaddress.ip_network(f"{primary}/24", strict=False).network_address == \
            ipaddress.ip_network(f"{ip}/24", strict=False).network_address
    except ValueError:
        return (3, ip)
    return (0 if same else 1 if addr.is_private and not addr.is_link_local else 2, ip)


def order_addresses(ips, links: dict[str, str], primary: str | None, wired_here: bool) -> list[str]:
    """Which of another Mac's addresses to try first: its wired ones when this Mac has a cable too,
    then ones on this Mac's own network, other private ones, link-local last."""
    return sorted(ips, key=lambda ip: (0 if wired_here and links.get(ip) == "wired" else 1, _rank(ip, primary)))


def browse_services(seconds: float = 2.0, service_type: str = SERVICE_TYPE) -> list[dict]:
    """Every advertiser visible on the network: {"name", "node_id", "addresses": ["ip:port", ...], "props"}."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    found: dict[str, dict] = {}
    primary = _primary_ip()
    wired_here = "wired" in link_kinds().values()
    zc = Zeroconf()

    class Listener:
        def add_service(self, zc_, type_, name):
            info = zc_.get_service_info(type_, name, timeout=1500)
            if not info or not info.addresses:
                return
            props = {k.decode(errors="replace"): (v or b"").decode(errors="replace") for k, v in (info.properties or {}).items()}
            links = dict(pair.split("=", 1) for pair in props.get("links", "").split(",") if "=" in pair)
            ips = order_addresses({socket.inet_ntoa(a) for a in info.addresses if len(a) == 4}, links, primary,
                                  wired_here)
            found[name] = {"name": props.get("name") or name.split(".")[0], "node_id": props.get("node_id", ""),
                           "addresses": [f"{ip}:{info.port}" for ip in ips], "props": props}

        update_service = add_service

        def remove_service(self, *_):
            pass

    ServiceBrowser(zc, service_type, Listener())
    time.sleep(seconds)
    zc.close()
    return sorted(found.values(), key=lambda s: s["name"].lower())


def browse(seconds: float = 2.0, service_type: str = SERVICE_TYPE) -> list[str]:
    """Each advertiser's best "host:port" (use browse_services to try every address)."""
    return [s["addresses"][0] for s in browse_services(seconds, service_type) if s["addresses"]]
