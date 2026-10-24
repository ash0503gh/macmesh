"""Certificates for mutual TLS between Macs on the mesh.

The first Mac (`macmesh init`) holds the mesh's certificate authority. Every
Mac has its own key and a certificate signed by that CA, given out during
pairing (`macmesh invite` / `macmesh join`). Workers and coordinators accept
only peers with a certificate from the mesh's CA that isn't revoked, so a Mac
can be cut off on its own without re-keying the others.

Keys are P-256 and never leave the Mac that made them. Uses the system
`openssl` (LibreSSL on macOS) so there's no Python dependency to install.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

from . import config

CA_DAYS = NODE_DAYS = 3650
LEAF_EXT = ("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth,clientAuth\n")


class PKIError(RuntimeError):
    pass


def paths(home: Path | None = None) -> dict[str, Path]:
    d = (home or config.home()) / "tls"
    return {"dir": d, "ca_crt": d / "ca.crt", "ca_key": d / "ca.key", "key": d / "node.key", "crt": d / "node.crt"}


def _openssl() -> str:
    for candidate in ("/usr/bin/openssl", shutil.which("openssl")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise PKIError("openssl not found")


def _run(*args: str | Path) -> str:
    proc = subprocess.run([_openssl(), *map(str, args)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise PKIError(f"openssl {args[0]} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def _new_key(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", path)
    os.chmod(path, 0o600)


def create_ca(node_id: str, home: Path | None = None) -> None:
    p = paths(home)
    _new_key(p["ca_key"])
    _run("req", "-new", "-x509", "-key", p["ca_key"], "-subj", f"/CN=macmesh CA {node_id}", "-days", CA_DAYS,
         "-sha256", "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", p["ca_crt"])


def new_csr(node_id: str, home: Path | None = None) -> str:
    """Make this Mac's key (if needed) and return a signing request for it."""
    p = paths(home)
    if not p["key"].exists():
        _new_key(p["key"])
    return _run("req", "-new", "-key", p["key"], "-subj", f"/CN={node_id}")


def sign(csr_pem: str, node_id: str, home: Path | None = None) -> str:
    """CA side: sign a request, which must name `node_id` as its subject."""
    p = paths(home)
    if not p["ca_key"].exists():
        raise PKIError("this Mac doesn't hold the mesh's CA; invite from the Mac you ran `macmesh init` on")
    if not all(c in "0123456789abcdef" for c in node_id) or not 1 <= len(node_id) <= 32:
        raise PKIError(f"bad node id {node_id!r}")
    with tempfile.TemporaryDirectory() as d:
        csr, ext, crt = Path(d, "req.csr"), Path(d, "ext"), Path(d, "out.crt")
        csr.write_text(csr_pem)
        ext.write_text(LEAF_EXT)
        _run("req", "-in", csr, "-noout", "-verify")  # proves the requester holds the key
        subject = _run("req", "-in", csr, "-noout", "-subject").replace(" ", "")
        if not subject.strip().endswith(f"CN={node_id}"):
            raise PKIError(f"signing request names {subject.strip()!r}, expected CN={node_id}")
        _run("x509", "-req", "-in", csr, "-CA", p["ca_crt"], "-CAkey", p["ca_key"], "-set_serial",
             "0x" + secrets.token_hex(8), "-days", NODE_DAYS, "-sha256", "-extfile", ext, "-out", crt)
        return crt.read_text()


def install(cert_pem: str, ca_pem: str, home: Path | None = None) -> None:
    p = paths(home)
    p["dir"].mkdir(parents=True, exist_ok=True)
    p["crt"].write_text(cert_pem)
    p["ca_crt"].write_text(ca_pem)


def self_issue(node_id: str, home: Path | None = None) -> None:
    """On the CA Mac: give this Mac its own node certificate."""
    install(sign(new_csr(node_id, home), node_id, home), paths(home)["ca_crt"].read_text(), home)


def ready(home: Path | None = None) -> bool:
    p = paths(home)
    return all(p[k].exists() for k in ("ca_crt", "key", "crt"))


def fingerprint_pem(pem: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def fingerprint_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def own_fingerprint(home: Path | None = None) -> str:
    return fingerprint_pem(paths(home)["crt"].read_text())


def context(server: bool, home: Path | None = None) -> ssl.SSLContext:
    """Mutual TLS: present our certificate and require one from the mesh's CA."""
    if not ready(home):
        raise PKIError("no TLS identity on this Mac: run `macmesh init` (first Mac) or `macmesh join`")
    p = paths(home)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False  # Macs move between addresses; identity is the CA-signed certificate
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(p["ca_crt"])
    ctx.load_cert_chain(p["crt"], p["key"])
    return ctx
