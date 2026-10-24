"""The daemon every Mac runs: accepts signed tasks, runs them, serves results.

Endpoints (all signed, see wire.py):
  GET    /info                    node capabilities, load and power state
  HEAD   /blobs/<sha256>          200 if the blob is already cached here
  PUT    /blobs/<sha256>          upload a blob (re-hashed on arrival)
  POST   /tasks                   queue a task (JSON, see jobspec.py)
  GET    /tasks/<id>              status, exit code, log tail, output manifest
  GET    /tasks/<id>/out/<name>   download an output (dirs arrive as .tar)
  DELETE /tasks/<id>              cancel and/or clean up
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from . import __version__, config, sysinfo, wire

PLACEHOLDER = re.compile(r"\{(in|out):([A-Za-z0-9_.-]+)\}")
SHA = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
LOG_TAIL = 4000
MAX_JSON = 1 << 20


class Task:
    def __init__(self, spec: dict, root: Path):
        self.spec = spec
        self.id = spec["task_id"]
        self.dir = root / self.id
        self.state = "queued"  # queued -> running -> done | failed | cancelled
        self.returncode: int | None = None
        self.error = ""
        self.proc: subprocess.Popen | None = None
        self.outputs: dict = {}
        self.started = self.finished = 0.0

    def status(self) -> dict:
        log = self.dir / "task.log"
        tail = ""
        if log.exists():
            with open(log, "rb") as f:
                f.seek(max(0, log.stat().st_size - LOG_TAIL))
                tail = f.read().decode(errors="replace")
        return {
            "task_id": self.id, "state": self.state, "returncode": self.returncode,
            "error": self.error, "log_tail": tail, "outputs": self.outputs,
            "elapsed": (self.finished or time.time()) - self.started if self.started else 0,
        }


class Worker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = config.home()
        self.blobs = self.root / "blobs"
        self.work = self.root / "work"
        self.blobs.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.work, ignore_errors=True)  # tasks don't survive restarts
        self.work.mkdir(parents=True)
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(int(cfg["slots"]))
        self.extract_lock = threading.Lock()

    # ---- capabilities -------------------------------------------------
    def programs(self) -> dict[str, str]:
        """Allowed program name -> resolved executable, for those present here."""
        found = {}
        for name in self.cfg["allowed_programs"]:
            path = shutil.which(name)
            if path:
                found[name] = path
        for name, tool in self.cfg["tools"].items():
            path = tool.get("path", "")
            if path and os.access(path, os.X_OK):
                found[name] = path
            elif shutil.which(name):
                found[name] = shutil.which(name)
        return found

    def info(self) -> dict:
        with self.lock:
            active = [t for t in self.tasks.values() if t.state in ("queued", "running")]
        pw = sysinfo.power()
        return {
            "version": __version__,
            "node_id": self.cfg["node_id"],
            "node_name": self.cfg["node_name"],
            "slots": int(self.cfg["slots"]),
            "running": sum(t.state == "running" for t in active),
            "queued": sum(t.state == "queued" for t in active),
            "hardware": sysinfo.hardware(),
            "power": pw,
            "accepting": self.accepting(pw),
            "programs": sorted(self.programs()),
        }

    def accepting(self, pw: dict) -> bool:
        if not pw["on_battery"]:
            return True
        if not self.cfg["accept_on_battery"]:
            return False
        return pw["battery_pct"] is None or pw["battery_pct"] >= self.cfg["min_battery"]

    # ---- blobs --------------------------------------------------------
    def put_blob(self, sha: str, stream, length: int) -> None:
        dest = self.blobs / sha
        if dest.exists():
            _drain(stream, length)
            return
        h = hashlib.sha256()
        fd, tmp = tempfile.mkstemp(dir=self.blobs, prefix=".up-")
        try:
            with os.fdopen(fd, "wb") as f:
                remaining = length
                while remaining:
                    block = stream.read(min(wire.CHUNK, remaining))
                    if not block:
                        raise IOError("upload truncated")
                    h.update(block)
                    f.write(block)
                    remaining -= len(block)
            if h.hexdigest() != sha:
                raise ValueError("blob hash mismatch")
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def extracted(self, sha: str) -> Path:
        """Directory inputs are tars; unpack each one once and clone per task."""
        target = self.blobs / f"{sha}.d"
        with self.extract_lock:
            if not target.exists():
                tmp = Path(tempfile.mkdtemp(dir=self.blobs, prefix=".x-"))
                wire.safe_extract(self.blobs / sha, tmp)
                tmp.rename(target)
        return target

    # ---- tasks --------------------------------------------------------
    def submit(self, spec: dict) -> Task:
        tid = spec.get("task_id", "")
        if not TASK_ID.match(tid):
            raise ValueError("bad task_id")
        argv = spec.get("argv")
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        programs = self.programs()
        if argv[0] not in programs:
            raise PermissionError(f"program {argv[0]!r} is not allowed or not installed on this node")
        for name, inp in spec.get("inputs", {}).items():
            if not SHA.match(inp.get("blob", "")):
                raise ValueError(f"input {name}: bad blob hash")
            if not (self.blobs / inp["blob"]).exists():
                raise FileNotFoundError(f"input {name}: blob not uploaded")
            _check_filename(inp.get("filename", ""))
        for fname in spec.get("outputs", {}).values():
            _check_filename(fname)
        with self.lock:
            if tid in self.tasks:
                raise FileExistsError("duplicate task_id")
            task = Task(spec, self.work)
            self.tasks[tid] = task
        threading.Thread(target=self._run, args=(task, programs[argv[0]]), daemon=True).start()
        return task

    def _run(self, task: Task, exe: str) -> None:
        with self.slots:
            if task.state == "cancelled":
                return
            task.state = "running"
            task.started = time.time()
            try:
                argv = self._prepare(task, exe)
                rc = self._execute(task, argv)
                task.returncode = rc
                if task.state == "cancelled":
                    return
                if rc != 0:
                    task.state = "failed"
                    task.error = f"exit code {rc}"
                else:
                    self._collect(task)
                    task.state = "done"
            except Exception as e:  # report anything to the coordinator
                if task.state != "cancelled":
                    task.state, task.error = "failed", f"{type(e).__name__}: {e}"
            finally:
                task.finished = time.time()

    def _prepare(self, task: Task, exe: str) -> list[str]:
        (task.dir / "in").mkdir(parents=True)
        (task.dir / "out").mkdir()
        paths = {}
        for name, inp in task.spec.get("inputs", {}).items():
            dest = task.dir / "in" / inp["filename"]
            src = self.extracted(inp["blob"]) if inp.get("kind") == "dir" else self.blobs / inp["blob"]
            if not dest.exists():  # several placeholders may share one file
                wire.clone(src, dest)
            paths[("in", name)] = str(dest)
        for name, fname in task.spec.get("outputs", {}).items():
            paths[("out", name)] = str(task.dir / "out" / fname)

        def expand(arg: str) -> str:
            def sub(m):
                key = (m.group(1), m.group(2))
                if key not in paths:
                    raise ValueError(f"unknown placeholder {m.group(0)}")
                return paths[key]
            return PLACEHOLDER.sub(sub, arg)

        argv = [exe] + [expand(a) for a in task.spec["argv"][1:]]
        (task.dir / "argv.json").write_text(json.dumps(argv, indent=1))
        return argv

    def _execute(self, task: Task, argv: list[str]) -> int:
        nice = int(self.cfg.get("nice", 0))
        with open(task.dir / "task.log", "wb") as log:
            task.proc = subprocess.Popen(
                argv, cwd=task.dir, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                preexec_fn=(lambda: os.nice(nice)) if nice else None, start_new_session=True,
            )
            awake = None
            if sysinfo.IS_MAC:
                # Keep the Mac from idle-sleeping while this task runs.
                awake = subprocess.Popen(["caffeinate", "-i", "-w", str(task.proc.pid)])
            try:
                timeout = task.spec.get("timeout")
                return task.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill(task.proc)
                task.error = "timed out"
                return -1
            finally:
                if awake:
                    awake.wait()

    def _collect(self, task: Task) -> None:
        for name, fname in task.spec.get("outputs", {}).items():
            path = task.dir / "out" / fname
            if path.is_dir():
                packed = task.dir / "out" / f".{name}.tar"
                wire.tar_dir(path, packed)
                kind, served = "dir", packed
            elif path.is_file():
                kind, served = "file", path
            else:
                raise FileNotFoundError(f"task did not produce output {name!r} ({fname})")
            task.outputs[name] = {"kind": kind, "size": served.stat().st_size,
                                  "sha256": wire.sha256_file(served), "served": str(served)}

    def cancel(self, tid: str) -> bool:
        with self.lock:
            task = self.tasks.pop(tid, None)
        if not task:
            return False
        if task.state in ("queued", "running"):
            task.state = "cancelled"
            if task.proc and task.proc.poll() is None:
                _kill(task.proc)
        shutil.rmtree(task.dir, ignore_errors=True)
        return True


def _check_filename(name: str) -> None:
    if not name or "/" in name or name in (".", "..") or name.startswith("."):
        raise ValueError(f"bad filename {name!r}")


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def _drain(stream, length: int) -> None:
    while length:
        block = stream.read(min(wire.CHUNK, length))
        if not block:
            return
        length -= len(block)


class Handler(BaseHTTPRequestHandler):
    worker: Worker
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; the CLI prints its own progress
        if os.environ.get("MACMESH_DEBUG"):
            super().log_message(fmt, *args)

    def _reply(self, code: int, obj=None) -> None:
        body = json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _auth(self, body: bytes | None) -> bool:
        if wire.verify(self.worker.cfg["token"], self.command, self.path, self.headers, body):
            return True
        self.close_connection = True
        self._reply(401, {"error": "bad signature (wrong token or clock skew)"})
        return False

    def _parts(self) -> list[str]:
        return [unquote(p) for p in self.path.split("?")[0].strip("/").split("/")]

    def do_GET(self):
        if not self._auth(b""):
            return
        p = self._parts()
        if p == ["info"]:
            return self._reply(200, self.worker.info())
        if len(p) >= 2 and p[0] == "tasks":
            task = self.worker.tasks.get(p[1])
            if not task:
                return self._reply(404, {"error": "no such task"})
            if len(p) == 2:
                return self._reply(200, task.status())
            if len(p) == 4 and p[2] == "out" and p[3] in task.outputs:
                return self._send_file(Path(task.outputs[p[3]]["served"]))
        self._reply(404, {"error": "not found"})

    def do_HEAD(self):
        if not self._auth(b""):
            return
        p = self._parts()
        ok = len(p) == 2 and p[0] == "blobs" and SHA.match(p[1]) and (self.worker.blobs / p[1]).exists()
        self._reply(200 if ok else 404)

    def do_PUT(self):
        if not self._auth(None):
            return
        p = self._parts()
        if len(p) != 2 or p[0] != "blobs" or not SHA.match(p[1]):
            return self._reply(404, {"error": "not found"})
        try:
            self.worker.put_blob(p[1], self.rfile, int(self.headers.get("Content-Length", 0)))
        except (IOError, ValueError) as e:
            self.close_connection = True
            return self._reply(400, {"error": str(e)})
        self._reply(201, {"blob": p[1]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_JSON:
            self.close_connection = True
            return self._reply(413, {"error": "request too large"})
        body = self.rfile.read(length)
        if not self._auth(body):
            return
        if self._parts() != ["tasks"]:
            return self._reply(404, {"error": "not found"})
        if not self.worker.accepting(sysinfo.power()):
            return self._reply(503, {"error": "not accepting work (battery policy)"})
        try:
            task = self.worker.submit(json.loads(body))
        except PermissionError as e:
            return self._reply(403, {"error": str(e)})
        except FileExistsError as e:
            return self._reply(409, {"error": str(e)})
        except (ValueError, KeyError, FileNotFoundError) as e:
            return self._reply(400, {"error": str(e)})
        self._reply(202, task.status())

    def do_DELETE(self):
        if not self._auth(b""):
            return
        p = self._parts()
        if len(p) == 2 and p[0] == "tasks" and self.worker.cancel(p[1]):
            return self._reply(200, {"deleted": p[1]})
        self._reply(404, {"error": "no such task"})

    def _send_file(self, path: Path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, wire.CHUNK)


def serve(cfg: dict, host: str = "0.0.0.0", advertise: bool = True) -> None:
    worker = Worker(cfg)
    handler = type("BoundHandler", (Handler,), {"worker": worker})
    server = ThreadingHTTPServer((host, int(cfg["port"])), handler)
    server.daemon_threads = True
    ad = None
    if advertise:
        from . import discovery
        ad = discovery.advertise(cfg)
    hw = sysinfo.hardware()
    print(f"macmesh worker '{cfg['node_name']}' on :{cfg['port']}  "
          f"[{hw['model']} · {hw['chip']} · score {hw['score']} · {cfg['slots']} slot(s)]")
    print(f"programs: {', '.join(sorted(worker.programs())) or '(none found)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if ad:
            ad.close()
        server.server_close()
