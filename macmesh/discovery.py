"""Bonjour (mDNS) advertisement and browsing: zero-config, like AirDrop."""

from __future__ import annotations

import socket
import time

from . import SERVICE_TYPE, __version__


def _local_ips() -> list[bytes]:
    ips = set()
    try:
        for fam, _, _, _, addr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(addr[0])
    except socket.gaierror:
        pass
    try:  # the address used to reach the LAN, if hostname lookup is unhelpful
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("224.0.0.251", 5353))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return [socket.inet_aton(ip) for ip in sorted(ips)] or [socket.inet_aton("127.0.0.1")]


class Advertisement:
    def __init__(self, zc, info):
        self.zc, self.info = zc, info

    def close(self) -> None:
        self.zc.unregister_service(self.info)
        self.zc.close()


def advertise(cfg: dict, service_type: str = SERVICE_TYPE, port: int | None = None) -> Advertisement | None:
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        print("zeroconf not installed: not advertising (peers must be added by address)")
        return None
    name = f"{cfg['node_name']}-{cfg['node_id'][:6]}.{service_type}"
    info = ServiceInfo(
        service_type, name, addresses=_local_ips(), port=int(port or cfg["port"]),
        properties={"node_id": cfg["node_id"], "name": cfg["node_name"], "v": __version__},
        server=f"{cfg['node_name']}.local.",
    )
    zc = Zeroconf()
    zc.register_service(info)
    return Advertisement(zc, info)


def browse(seconds: float = 2.0, service_type: str = SERVICE_TYPE) -> list[str]:
    """Return "host:port" for every macmesh worker (or open invite) visible on the network."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    found: dict[str, str] = {}
    zc = Zeroconf()

    class Listener:
        def add_service(self, zc_, type_, name):
            info = zc_.get_service_info(type_, name, timeout=1500)
            if info and info.addresses:
                found[name] = f"{socket.inet_ntoa(info.addresses[0])}:{info.port}"

        update_service = add_service

        def remove_service(self, *_):
            pass

    ServiceBrowser(zc, service_type, Listener())
    time.sleep(seconds)
    zc.close()
    return sorted(set(found.values()))
