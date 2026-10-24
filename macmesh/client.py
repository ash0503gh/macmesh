"""Signed HTTP client for talking to one worker."""

from __future__ import annotations

import http.client
import json
import os
import shutil
from pathlib import Path

from . import wire


class NodeError(Exception):
    """The node could not be reached or misbehaved (not a task failure)."""


class TaskRejected(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class NodeClient:
    def __init__(self, address: str, token: str, timeout: float = 30):
        host, _, port = address.rpartition(":")
        self.address, self.host, self.port = address, host, int(port)
        self.token, self.timeout = token, timeout
        self.info: dict = {}

    @property
    def name(self) -> str:
        return self.info.get("node_name", self.address)

    def _conn(self, timeout: float | None = None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)

    def _request(self, method: str, path: str, body: bytes = b"") -> tuple[int, bytes]:
        try:
            conn = self._conn()
            headers = wire.auth_headers(self.token, method, path, body)
            headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except (OSError, http.client.HTTPException) as e:
            raise NodeError(f"{self.address}: {e}") from e
        if resp.status == 401:
            raise NodeError(f"{self.address}: authentication failed (token mismatch or clock skew)")
        return resp.status, data

    def _json(self, method: str, path: str, obj=None) -> tuple[int, dict]:
        code, data = self._request(method, path, json.dumps(obj).encode() if obj is not None else b"")
        try:
            return code, json.loads(data or b"{}")
        except ValueError as e:
            raise NodeError(f"{self.address}: bad response") from e

    def fetch_info(self) -> dict:
        code, info = self._json("GET", "/info")
        if code != 200:
            raise NodeError(f"{self.address}: /info returned {code}")
        self.info = info
        return info

    def has_blob(self, sha: str) -> bool:
        code, _ = self._request("HEAD", f"/blobs/{sha}")
        return code == 200

    def put_blob(self, sha: str, path: Path) -> None:
        size = path.stat().st_size
        req_path = f"/blobs/{sha}"
        try:
            conn = self._conn(timeout=max(self.timeout, 600))
            conn.putrequest("PUT", req_path)
            for k, v in wire.auth_headers(self.token, "PUT", req_path, None).items():
                conn.putheader(k, v)
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(wire.CHUNK), b""):
                    conn.send(block)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except (OSError, http.client.HTTPException) as e:
            raise NodeError(f"{self.address}: upload failed: {e}") from e
        if resp.status != 201:
            raise NodeError(f"{self.address}: upload rejected ({resp.status}): {data[:200]!r}")

    def submit(self, spec: dict) -> dict:
        code, body = self._json("POST", "/tasks", spec)
        if code != 202:
            raise TaskRejected(code, body.get("error", f"HTTP {code}"))
        return body

    def status(self, task_id: str) -> dict:
        code, body = self._json("GET", f"/tasks/{task_id}")
        if code == 404:
            raise NodeError(f"{self.address}: lost task {task_id} (worker restarted?)")
        return body

    def download(self, task_id: str, name: str, dest: Path) -> None:
        path = f"/tasks/{task_id}/out/{name}"
        tmp = dest.with_name(dest.name + ".part")
        try:
            conn = self._conn(timeout=max(self.timeout, 600))
            conn.request("GET", path, headers=wire.auth_headers(self.token, "GET", path, b""))
            resp = conn.getresponse()
            if resp.status != 200:
                raise NodeError(f"{self.address}: download {name} returned {resp.status}")
            with open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f, wire.CHUNK)
            conn.close()
            os.replace(tmp, dest)
        except (OSError, http.client.HTTPException) as e:
            raise NodeError(f"{self.address}: download failed: {e}") from e
        finally:
            if tmp.exists():
                tmp.unlink()

    def delete(self, task_id: str) -> None:
        try:
            self._request("DELETE", f"/tasks/{task_id}")
        except NodeError:
            pass
