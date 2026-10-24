"""Finding the Macs on the mesh: static peers plus Bonjour, each asked for its /info."""

from __future__ import annotations

from . import discovery, pki
from .client import NodeClient, NodeError


def find_nodes(cfg: dict, peers: list[str] = (), discover: bool = True, seconds: float = 2.0,
               log=lambda msg: None) -> list[NodeClient]:
    """Every reachable worker, once each (a Mac seen at two addresses counts once).

    A Mac found over Bonjour may have several addresses; each is tried until one answers."""
    candidates = [[a] for a in list(cfg.get("peers", [])) + list(peers)]
    if discover:
        candidates += [s["addresses"] for s in discovery.browse_services(seconds) if s["addresses"]]
    if not candidates:
        candidates = [[f"127.0.0.1:{cfg['port']}"]]  # at least this Mac, if its daemon runs
    tls = pki.context(server=False) if cfg.get("tls", True) else None
    clients, seen = [], set()
    for addresses in candidates:
        for addr in addresses:
            c = NodeClient(addr, cfg["token"], tls=tls, revoked=cfg.get("revoked", []))
            try:
                info = c.fetch_info()
            except NodeError as e:
                log(f"skip {addr}: {e}")
                continue
            if info["node_id"] not in seen:
                seen.add(info["node_id"])
                clients.append(c)
            break  # this Mac answered; don't try its other addresses
    return clients
