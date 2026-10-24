"""Adding a Mac to the mesh, as easily as AirDrop and as safely as pairing a Bluetooth device.

The Mac that started the mesh (it holds the mesh's certificate authority) listens for new Macs
on the worker port + 1 and advertises itself over Bonjour (_macmesh-pair._tcp). A new Mac
joins one of two ways:

1. Ask to join (the app's way): the new Mac lists the meshes it finds nearby; you click Join.
   The first Mac shows "Riya's MacBook wants to join" with a 6-digit number, and the new Mac
   shows a number too. You check they match and click Allow. The number is worked out from
   random values both Macs committed to before seeing each other's (commit, then reveal), from
   the first Mac's certificate as the new Mac saw it, and from the new Mac's signing request.
   A machine in the middle changes one of those, so the numbers differ, and it can't search
   for a match because it has to commit before it learns the other side's value.
2. A one-time code (macmesh invite / macmesh join CODE): both directions carry an HMAC keyed by
   the code over the first Mac's certificate fingerprint and the payload. The code is 60 random
   bits, works once, expires after 10 minutes and allows 3 wrong tries.

Either way the new Mac sends a signing request for its own key (which never leaves it) and gets
back its certificate, the CA and the mesh token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path

from . import pki

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
CODE_LEN = 12
EXPIRY = 600.0
MAX_FAILURES = 3
MAX_MESSAGE = 64 * 1024
DECISION_WAIT = 150.0  # how long a join request waits for someone to click Allow


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


def match_number(nj: str, ni: str, server_fp: str, csr: str, node_id: str) -> str:
    """The 6-digit number both Macs show for a join request, as "482 913"."""
    digest = hashlib.sha256("\n".join(["macmesh-pair-v2", nj, ni, server_fp, _sha(csr), node_id]).encode()).digest()
    n = int.from_bytes(digest[:4], "big") % 1_000_000
    return f"{n // 1000:03d} {n % 1000:03d}"


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


# ---- the first Mac -------------------------------------------------------------
class PairingServer:
    """Listens for new Macs: join requests (approved with decide()) and one-time codes (set_code())."""

    def __init__(self, cfg: dict, port: int, home: Path | None = None, host: str = "0.0.0.0", on_change=None):
        self.cfg, self.home, self.on_change = cfg, home, on_change or (lambda: None)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        p = pki.paths(home)
        self.ctx.load_cert_chain(p["crt"], p["key"])  # the new Mac has no certificate to show yet
        self.fingerprint = pki.own_fingerprint(home)
        self.sock = socket.create_server((host, port))
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self.code: str | None = None
        self.code_expires = 0.0
        self.failures = 0
        self.requests: dict[str, dict] = {}   # id -> {name, node_id, number, event, allow}
        self.joined: list[dict] = []          # {name, at}
        self.last_error = ""
        self.closed = threading.Event()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # -- control -----------------------------------------------------------------
    def set_code(self) -> str:
        with self.lock:
            self.code, self.code_expires, self.failures, self.last_error = normalize(new_code()), time.time() + EXPIRY, 0, ""
        return "-".join(self.code[i:i + 4] for i in range(0, CODE_LEN, 4))

    def code_state(self) -> tuple[str | None, float]:
        with self.lock:
            if self.code and time.time() < self.code_expires:
                return "-".join(self.code[i:i + 4] for i in range(0, CODE_LEN, 4)), self.code_expires
            return None, 0.0

    def pending(self) -> list[dict]:
        with self.lock:
            return [{"id": rid, "name": r["name"], "number": r["number"], "age": round(time.time() - r["at"])}
                    for rid, r in self.requests.items() if r["allow"] is None]

    def decide(self, request_id: str, allow: bool) -> bool:
        with self.lock:
            r = self.requests.get(request_id)
            if not r or r["allow"] is not None:
                return False
            r["allow"] = bool(allow)
        r["event"].set()
        return True

    def close(self) -> None:
        self.closed.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # -- connections -------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self.closed.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(15)
        try:
            with self.ctx.wrap_socket(conn, server_side=True) as s:
                msg = _read_line(s)
                if msg.get("kind") == "request":
                    self._request(s, msg)
                else:
                    self._code_join(s, msg)
        except (OSError, ssl.SSLError, PairingError, pki.PKIError):
            pass

    def _issue(self, csr: str, node_id: str) -> dict:
        cert = pki.sign(csr, node_id, self.home)
        return {"cert": cert, "ca": pki.paths(self.home)["ca_crt"].read_text(), "token": self.cfg["token"]}

    def _joined(self, name: str) -> None:
        with self.lock:
            self.joined.append({"name": name, "at": time.time()})
        self.on_change()

    def _code_join(self, s: ssl.SSLSocket, msg: dict) -> None:
        csr, node_id = msg.get("csr", ""), msg.get("node_id", "")
        with self.lock:
            code = self.code if self.code and time.time() < self.code_expires else None
        expected = _mac(code, "join", self.fingerprint, node_id, _sha(csr)) if code else ""
        if not code or not hmac.compare_digest(str(msg.get("mac", "")), expected):
            with self.lock:
                if code:
                    self.failures += 1
                    if self.failures >= MAX_FAILURES:
                        self.code, self.last_error = None, f"{MAX_FAILURES} wrong codes: the code was cancelled"
            _send(s, {"error": "wrong or expired code (or someone is in the middle)"})
            return
        issued = self._issue(csr, node_id)
        with self.lock:
            self.code = None  # one use
        _send(s, {**issued, "mac": _mac(code, "accept", self.fingerprint, _sha(issued["cert"]), _sha(issued["ca"]),
                                        _sha(issued["token"]))})
        self._joined(str(msg.get("name") or node_id))

    def _request(self, s: ssl.SSLSocket, msg: dict) -> None:
        csr, node_id, name = msg.get("csr", ""), str(msg.get("node_id", "")), str(msg.get("name") or "A Mac")[:64]
        commit = str(msg.get("commit", ""))
        ni = secrets.token_hex(16)
        _send(s, {"ni": ni})
        nj = str(_read_line(s).get("nj", ""))
        if hashlib.sha256(nj.encode()).hexdigest() != commit or len(nj) != 32:
            _send(s, {"error": "the request didn't keep its commitment"})
            return
        rid = uuid.uuid4().hex[:8]
        request = {"name": name, "node_id": node_id, "at": time.time(), "allow": None, "event": threading.Event(),
                   "number": match_number(nj, ni, self.fingerprint, csr, node_id)}
        with self.lock:
            self.requests[rid] = request
        self.on_change()
        s.settimeout(DECISION_WAIT + 10)
        try:
            request["event"].wait(DECISION_WAIT)
            if request["allow"]:
                _send(s, self._issue(csr, node_id))
                self._joined(name)
            else:
                _send(s, {"error": "the other Mac didn't let this one in" if request["allow"] is False
                          else "nobody answered on the other Mac in time"})
        finally:
            with self.lock:
                self.requests.pop(rid, None)
            self.on_change()


class Invite:
    """A one-time code on its own (the `macmesh invite` command when no daemon is running)."""

    def __init__(self, cfg: dict, code: str, port: int, home: Path | None = None, host: str = "0.0.0.0"):
        self.server = PairingServer(cfg, port, home, host)
        with self.server.lock:
            self.server.code, self.server.code_expires = normalize(code), time.time() + EXPIRY
        self.port, self.sock = self.server.port, self.server.sock

    @property
    def failures(self) -> int:
        return self.server.failures

    def wait(self, timeout: float = EXPIRY) -> str:
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                if self.server.joined:
                    return self.server.joined[0]["name"]
                if self.server.code is None and self.server.last_error:
                    raise PairingError(self.server.last_error)
                time.sleep(0.1)
            raise PairingError("the invite expired")
        finally:
            self.server.close()


# ---- the new Mac -----------------------------------------------------------------
def _client_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # verified through the code or the number
    return ctx


def _connect(addresses: list[str] | str, timeout: float = 8) -> tuple[ssl.SSLSocket, str]:
    """TLS to the first address that answers (a Mac can have several: Wi-Fi, Ethernet, VPN)."""
    if isinstance(addresses, str):
        addresses = [a for a in addresses.split(",") if a]
    errors = []
    for address in addresses:
        host, _, port = address.rpartition(":")
        try:
            raw = socket.create_connection((host.strip("[]"), int(port)), timeout=timeout)
            return _client_ctx().wrap_socket(raw), address
        except (OSError, ssl.SSLError, ValueError) as e:
            errors.append(f"{address}: {e}")
    raise PairingError("couldn't reach the other Mac (" + "; ".join(errors) + "). Check both Macs are on the same "
                       "network, that macmesh is allowed through the firewall, and that it may use the local network.")


def _install(cfg: dict, reply: dict, home: Path | None) -> None:
    pki.install(reply["cert"], reply["ca"], home)
    cfg["token"] = reply["token"]


def join(cfg: dict, code: str, address: list[str] | str, home: Path | None = None) -> None:
    """Join with a one-time code; installs the certificate and the mesh token into cfg."""
    code = normalize(code)
    csr = pki.new_csr(cfg["node_id"], home)
    s, _ = _connect(address)
    with s:
        s.settimeout(15)
        seen = pki.fingerprint_der(s.getpeercert(binary_form=True))
        _send(s, {"v": 1, "node_id": cfg["node_id"], "name": cfg["node_name"], "csr": csr,
                  "mac": _mac(code, "join", seen, cfg["node_id"], _sha(csr))})
        reply = _read_line(s)
    if "error" in reply:
        raise PairingError(reply["error"])
    cert, ca, token = reply.get("cert", ""), reply.get("ca", ""), reply.get("token", "")
    if not hmac.compare_digest(str(reply.get("mac", "")), _mac(code, "accept", seen, _sha(cert), _sha(ca), _sha(token))):
        raise PairingError("the reply wasn't signed with the code: someone may be in the middle; not joining")
    _install(cfg, reply, home)


def request_join(cfg: dict, address: list[str] | str, home: Path | None = None, on_number=lambda number: None) -> None:
    """Ask the first Mac to let this one in. `on_number` gets the 6-digit number to compare; this
    returns once someone clicks Allow there (or raises if they don't)."""
    csr = pki.new_csr(cfg["node_id"], home)
    nj = secrets.token_hex(16)
    s, _ = _connect(address)
    with s:
        s.settimeout(15)
        seen = pki.fingerprint_der(s.getpeercert(binary_form=True))
        _send(s, {"v": 2, "kind": "request", "node_id": cfg["node_id"], "name": cfg["node_name"], "csr": csr,
                  "commit": hashlib.sha256(nj.encode()).hexdigest()})
        first = _read_line(s)
        if "error" in first:
            raise PairingError(first["error"])
        _send(s, {"nj": nj})
        on_number(match_number(nj, str(first.get("ni", "")), seen, csr, cfg["node_id"]))
        s.settimeout(DECISION_WAIT + 20)
        reply = _read_line(s)
    if "error" in reply:
        raise PairingError(reply["error"])
    if not all(reply.get(k) for k in ("cert", "ca", "token")):
        raise PairingError("the other Mac's reply was incomplete")
    _install(cfg, reply, home)
