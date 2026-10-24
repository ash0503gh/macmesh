"""Pairing a new Mac with the mesh using a one-time code, like pairing a Bluetooth device.

On the Mac that holds the mesh's CA:   macmesh invite        (shows a code, waits)
On the new Mac:                        macmesh join CODE

The new Mac opens TLS to the inviter (it can't verify the inviter's
certificate yet) and sends a signing request for its own key. Both directions
carry an HMAC keyed by the code over what that side saw: the inviter's
certificate fingerprint and the payload. A machine in the middle shows the new
Mac a different certificate, so the inviter's check fails without the code,
and it can't forge the reply either. The code is 60 random bits, works once,
expires after 10 minutes and allows 3 wrong tries, so it can't be guessed in
time. The reply carries the signed certificate, the CA and the mesh token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import ssl
import time
from pathlib import Path

from . import pki

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
CODE_LEN = 12
EXPIRY = 600.0
MAX_FAILURES = 3
MAX_MESSAGE = 64 * 1024


class PairingError(RuntimeError):
    pass


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LEN, 4))


def normalize(code: str) -> str:
    c = code.upper().replace("-", "").replace(" ", "")
    c = c.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    if len(c) != CODE_LEN or any(ch not in ALPHABET for ch in c):
        raise PairingError("that doesn't look like a pairing code (12 letters and digits, e.g. 7K2Q-M9XD-4TRA)")
    return c


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _mac(code: str, *parts: str) -> str:
    return hmac.new(code.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def _read_line(sock: ssl.SSLSocket) -> dict:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_MESSAGE:
            raise PairingError("message too large")
    try:
        return json.loads(buf)
    except ValueError:
        raise PairingError("garbled pairing message") from None


def _send(sock: ssl.SSLSocket, obj: dict) -> None:
    sock.sendall(json.dumps(obj).encode() + b"\n")


# ---- inviter -----------------------------------------------------------------
class Invite:
    """Listens for one Mac to join with `code`. `wait()` returns the joined Mac's name."""

    def __init__(self, cfg: dict, code: str, port: int, home: Path | None = None, host: str = "0.0.0.0"):
        self.cfg, self.code, self.home = cfg, normalize(code), home
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        p = pki.paths(home)
        self.ctx.load_cert_chain(p["crt"], p["key"])  # the joiner has no certificate to show yet
        self.fingerprint = pki.own_fingerprint(home)
        self.sock = socket.create_server((host, port))
        self.port = self.sock.getsockname()[1]
        self.failures = 0

    def wait(self, timeout: float = EXPIRY) -> str:
        deadline = time.time() + timeout
        try:
            while self.failures < MAX_FAILURES:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise PairingError("the invite expired")
                self.sock.settimeout(remaining)
                try:
                    conn, _ = self.sock.accept()
                except socket.timeout:
                    raise PairingError("the invite expired") from None
                joined = self._handle(conn)
                if joined:
                    return joined
            raise PairingError(f"{MAX_FAILURES} wrong codes: invite cancelled")
        finally:
            self.sock.close()

    def _handle(self, conn: socket.socket) -> str:
        conn.settimeout(10)
        try:
            with self.ctx.wrap_socket(conn, server_side=True) as s:
                msg = _read_line(s)
                csr, node_id = msg.get("csr", ""), msg.get("node_id", "")
                expected = _mac(self.code, "join", self.fingerprint, node_id, _sha(csr))
                if not hmac.compare_digest(str(msg.get("mac", "")), expected):
                    self.failures += 1
                    _send(s, {"error": "wrong code (or someone is in the middle)"})
                    return ""
                cert = pki.sign(csr, node_id, self.home)
                ca = pki.paths(self.home)["ca_crt"].read_text()
                token = self.cfg["token"]
                _send(s, {"cert": cert, "ca": ca, "token": token,
                          "mac": _mac(self.code, "accept", self.fingerprint, _sha(cert), _sha(ca), _sha(token))})
                return str(msg.get("name") or node_id)
        except (OSError, ssl.SSLError, PairingError, pki.PKIError):
            self.failures += 1
            return ""


# ---- joiner ------------------------------------------------------------------
def join(cfg: dict, code: str, address: str, home: Path | None = None) -> None:
    """Pair with the inviter at host:port; installs the certificate and the mesh token into cfg."""
    code = normalize(code)
    host, _, port = address.rpartition(":")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # verified through the code instead
    csr = pki.new_csr(cfg["node_id"], home)
    try:
        with ctx.wrap_socket(socket.create_connection((host, int(port)), timeout=10)) as s:
            seen = pki.fingerprint_der(s.getpeercert(binary_form=True))
            _send(s, {"v": 1, "node_id": cfg["node_id"], "name": cfg["node_name"], "csr": csr,
                      "mac": _mac(code, "join", seen, cfg["node_id"], _sha(csr))})
            reply = _read_line(s)
    except (OSError, ssl.SSLError) as e:
        raise PairingError(f"couldn't reach the inviting Mac at {address}: {e}") from e
    if "error" in reply:
        raise PairingError(reply["error"])
    cert, ca, token = reply.get("cert", ""), reply.get("ca", ""), reply.get("token", "")
    if not hmac.compare_digest(str(reply.get("mac", "")), _mac(code, "accept", seen, _sha(cert), _sha(ca), _sha(token))):
        raise PairingError("the reply wasn't signed with the code: someone may be in the middle; not joining")
    pki.install(cert, ca, home)
    cfg["token"] = token
