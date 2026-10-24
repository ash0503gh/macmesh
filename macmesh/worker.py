"""The daemon every Mac runs: accepts signed tasks, runs them, serves results.

Endpoints (all signed, see wire.py; over mutual TLS unless `tls` is off):
  GET    /info                    node capabilities, load, power and owner state
  HEAD   /blobs/<sha256>          200 if the blob is already cached here
  PUT    /blobs/<sha256>          upload a blob (re-hashed on arrival)
  DELETE /blobs/<sha256>          forget a blob (409 while a task still uses it)
  POST   /tasks                   queue a task (JSON, see jobspec.py)
  GET    /tasks/<id>              status, exit code, log tail, output manifest
  GET    /tasks/<id>/out/<name>   download an output (dirs arrive as .tar)
  DELETE /tasks/<id>              cancel and/or clean up

The owner comes first: while they need the Mac, running tasks are paused
(SIGSTOP) and no new ones are accepted; if it lasts, the tasks are handed back
("yielded") for other Macs to run. Shutting the worker down (SIGTERM, as at
logout or shutdown) hands back everything at once.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from . import __version__, config, owner, pki, sandbox, sysinfo, toolinfo, wire

PLACEHOLDER = re.compile(r"\{(in|out):([A-Za-z0-9_.-]+)\}")
SHA = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
LOG_TAIL = 4000
MAX_JSON = 1 << 20
OWNER_POLL = 2.0
GC_EVERY = 60.0
ACTIVE = ("queued", "running", "paused")
QOS = {"utility", "background", "maintenance"}


class Task:
    def __init__(self, spec: dict, root: Path):
        self.spec = spec
        self.id = spec["task_id"]
        self.dir = root / self.id
        # queued -> running <-> paused -> done | failed | cancelled | yielded
        self.state = "queued"
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

    def blobs(self) -> set[str]:
        return {inp["blob"] for inp in self.spec.get("inputs", {}).values()}


class Worker:
    def __init__(self, cfg: dict, home: Path | None = None, owner_sample=None):
        self.cfg = cfg
        self.root = home or config.home()
        self.blobs = self.root / "blobs"
        self.work = self.root / "work"
        self.blobs.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.work, ignore_errors=True)  # tasks don't survive restarts
        self.work.mkdir(parents=True)
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(int(cfg["slots"]))
        self.extract_lock = threading.Lock()
        self.fingerprints = toolinfo.fingerprints(cfg, self.root)
        self.sandboxed = bool(cfg.get("sandbox")) and sandbox.available()
        self.owner_busy = ""      # why the owner needs the Mac right now, or ""
        self.draining = ""        # why this worker is going away, or ""
        self.stopped = threading.Event()
        self.watch = owner.OwnerWatch(cfg["owner"], sample=owner_sample)
        self._busy_since = 0.0
        self._calm_since: float | None = None
        self.collect_garbage()
        threading.Thread(target=self._gc_loop, daemon=True).start()
        if cfg["owner"].get("mode") != "off":
            threading.Thread(target=self._owner_loop, daemon=True).start()

    # ---- capabilities -------------------------------------------------
    def programs(self) -> dict[str, str]:
        return toolinfo.programs(self.cfg, self.root)

    def info(self) -> dict:
        with self.lock:
            active = [t for t in self.tasks.values() if t.state in ACTIVE]
        pw = sysinfo.power()
        return {
            "version": __version__,
            "node_id": self.cfg["node_id"],
            "node_name": self.cfg["node_name"],
            "slots": int(self.cfg["slots"]),
            "running": sum(t.state == "running" for t in active),
            "paused": sum(t.state == "paused" for t in active),
            "queued": sum(t.state == "queued" for t in active),
            "hardware": sysinfo.hardware(),
            "power": pw,
            "accepting": not self.refusal(pw),
            "refusal": self.refusal(pw),
            "programs": sorted(self.programs()),
            "fingerprints": self.fingerprints,
            "sandboxed": self.sandboxed,
        }

    def refusal(self, pw: dict) -> str:
        """Why this Mac won't take new work right now, or "" if it will."""
        if self.draining:
            return self.draining
        if self.owner_busy:
            return self.owner_busy
        free_gb = shutil.disk_usage(self.root).free / 2**30
        if free_gb < float(self.cfg.get("min_free_gb", 0)):
            return f"disk nearly full ({free_gb:.1f} GB free)"
        if not pw["on_battery"]:
            return ""
        if not self.cfg["accept_on_battery"]:
            return "on battery"
        if pw["battery_pct"] is not None and pw["battery_pct"] < self.cfg["min_battery"]:
            return f"battery at {pw['battery_pct']}%"
        return ""

    def accepting(self, pw: dict) -> bool:
        return not self.refusal(pw)

    # ---- blobs --------------------------------------------------------
    def put_blob(self, sha: str, stream, length: int) -> None:
        dest = self.blobs / sha
        if dest.exists():
            _drain(stream, length)
            _touch(dest)
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

    def has_blob(self, sha: str) -> bool:
        path = self.blobs / sha
        if path.exists():
            _touch(path)  # about to be used: keep it from expiring
            return True
        return False

    def extracted(self, sha: str) -> Path:
        """Directory inputs are tars; unpack each one once and clone per task."""
        target = self.blobs / f"{sha}.d"
        with self.extract_lock:
            if not target.exists():
                tmp = Path(tempfile.mkdtemp(dir=self.blobs, prefix=".x-"))
                wire.safe_extract(self.blobs / sha, tmp)
                tmp.rename(target)
        return target

    def blobs_in_use(self) -> set[str]:
        with self.lock:
            return set().union(*(t.blobs() for t in self.tasks.values() if t.state in ACTIVE))

    def delete_blob(self, sha: str) -> bool:
        """Remove a blob and its unpacked copy. False while an active task needs it."""
        with self.extract_lock:
            if sha in self.blobs_in_use():
                return False
            (self.blobs / sha).unlink(missing_ok=True)
            shutil.rmtree(self.blobs / f"{sha}.d", ignore_errors=True)
        return True

    def collect_garbage(self) -> int:
        """Delete blobs nobody has used for `blob_ttl` seconds: media shouldn't linger on lent Macs."""
        cutoff = time.time() - float(self.cfg.get("blob_ttl", 1800))
        removed = 0
        for path in list(self.blobs.iterdir()):
            try:
                old = path.stat().st_mtime < cutoff
            except FileNotFoundError:  # went with its blob or unpacked copy
                continue
            sha = path.name.removesuffix(".d")
            if path.name.startswith("."):  # half-done upload or unpack
                if old:
                    shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
                continue
            if SHA.match(sha) and old and self.delete_blob(sha):
                removed += 1
        return removed

    def _gc_loop(self) -> None:
        while not self.stopped.wait(GC_EVERY):
            try:
                self.collect_garbage()
            except OSError:
                pass

    # ---- tasks --------------------------------------------------------
    def submit(self, spec: dict) -> Task:
        tid = spec.get("task_id", "")
        if not TASK_ID.match(tid):
            raise ValueError("bad task_id")
        steps = spec.get("steps") or [spec.get("argv")]
        if not all(isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) for argv in steps):
            raise ValueError("argv (or each step) must be a non-empty list of strings")
        spec["steps"] = steps
        fail_if = spec.get("fail_if", [])
        if not isinstance(fail_if, list) or not all(isinstance(m, str) and m for m in fail_if):
            raise ValueError("fail_if must be a list of non-empty strings")
        for pattern in fail_if:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"fail_if {pattern!r} isn't a valid regular expression: {e}") from None
        programs = self.programs()
        for argv in steps:
            if argv[0] not in programs:
                raise PermissionError(f"program {argv[0]!r} is not allowed or not installed on this node")
        for name, inp in spec.get("inputs", {}).items():
            if not SHA.match(inp.get("blob", "")):
                raise ValueError(f"input {name}: bad blob hash")
            if not self.has_blob(inp["blob"]):
                raise FileNotFoundError(f"input {name}: blob not uploaded")
            _check_filename(inp.get("filename", ""))
        for fname in spec.get("outputs", {}).values():
            _check_filename(fname)
        with self.lock:
            if tid in self.tasks:
                raise FileExistsError("duplicate task_id")
            task = Task(spec, self.work)
            self.tasks[tid] = task
        threading.Thread(target=self._run, args=(task, [programs[a[0]] for a in steps]), daemon=True).start()
        return task

    def _run(self, task: Task, exes: list[str]) -> None:
        with self.slots:
            while self.owner_busy and task.state == "queued":
                time.sleep(0.5)  # don't start new work while the owner needs the Mac
            with self.lock:
                if task.state != "queued":
                    return
                task.state = "running"
            task.started = time.time()
            try:
                rc = self._execute(task, self._prepare(task, exes))
                task.returncode = rc
                if task.state in ("cancelled", "yielded"):
                    return
                if rc != 0:
                    task.state = "failed"
                    task.error = task.error or f"exit code {rc}"
                else:
                    self._collect(task)
                    task.state = "done"
            except Exception as e:  # report anything to the coordinator
                if task.state not in ("cancelled", "yielded"):
                    task.state, task.error = "failed", f"{type(e).__name__}: {e}"
            finally:
                task.finished = time.time()

    def _prepare(self, task: Task, exes: list[str]) -> list[list[str]]:
        (task.dir / "in").mkdir(parents=True)
        (task.dir / "out").mkdir()
        (task.dir / "tmp").mkdir()
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
            return PLACEHOLDER.sub(sub, arg).replace("{indir}", str(task.dir / "in"))

        table = toolinfo.tools(self.cfg, self.root) if self.sandboxed else {}
        priority = self.cfg.get("priority", "normal")
        steps = []
        for i, (exe, step) in enumerate(zip(exes, task.spec["steps"])):
            argv = [exe] + [expand(a) for a in step[1:]]
            if self.sandboxed:
                argv = sandbox.wrap(argv, task.dir, table.get(step[0], {}), name=f"step{i + 1}.sb")
            if priority in QOS and sysinfo.IS_MAC:
                # A QoS clamp makes macOS favour the owner's apps over this task.
                argv = ["taskpolicy", "-c", priority, *argv]
            steps.append(argv)
        (task.dir / "steps.json").write_text(json.dumps(steps, indent=1))
        return steps

    def _execute(self, task: Task, steps: list[list[str]]) -> int:
        """Run the steps in order in the task folder; the first that fails ends the task."""
        nice = int(self.cfg.get("nice", 0))
        env = dict(os.environ, TMPDIR=str(task.dir / "tmp"))
        deadline = time.time() + task.spec["timeout"] if task.spec.get("timeout") else None
        with open(task.dir / "task.log", "wb") as log:
            for i, argv in enumerate(steps):
                if len(steps) > 1:
                    log.write(f"--- step {i + 1}/{len(steps)}: {task.spec['steps'][i][0]}\n".encode())
                    log.flush()
                with self.lock:
                    if task.state not in ("running", "paused"):
                        return -1  # yielded or cancelled between steps
                    task.proc = subprocess.Popen(
                        argv, cwd=task.dir, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                        preexec_fn=(lambda: os.nice(nice)) if nice else None, start_new_session=True,
                    )
                    if task.state == "paused":  # the owner got busy while the last step was ending
                        self._signal(task, signal.SIGSTOP)
                awake = None
                if sysinfo.IS_MAC:
                    # Keep the Mac from idle-sleeping while this task runs.
                    awake = subprocess.Popen(["caffeinate", "-i", "-w", str(task.proc.pid)])
                try:
                    rc = task.proc.wait(timeout=max(deadline - time.time(), 0.1) if deadline else None)
                except subprocess.TimeoutExpired:
                    _kill(task.proc)
                    task.error = "timed out"
                    return -1
                finally:
                    if awake:
                        awake.wait()
                if rc != 0:
                    if len(steps) > 1:
                        task.error = f"step {i + 1} ({task.spec['steps'][i][0]}) exit code {rc}"
                    return rc
        text = (task.dir / "task.log").read_text(errors="replace")
        for pattern in task.spec.get("fail_if", []):
            if m := re.search(pattern, text, re.MULTILINE):
                task.error = f"the output says it failed: {m.group(0)!r}"
                return 1
        return 0

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
            was = task.state
            if was in ACTIVE:
                task.state = "cancelled"
        if was in ACTIVE and task.proc and task.proc.poll() is None:
            _kill(task.proc)
        shutil.rmtree(task.dir, ignore_errors=True)
        return True

    # ---- the owner comes first ----------------------------------------
    def _signal(self, task: Task, sig: int) -> None:
        if task.proc and task.proc.poll() is None:
            try:
                os.killpg(task.proc.pid, sig)
            except OSError:
                pass

    def pause_all(self) -> None:
        with self.lock:
            running = [t for t in self.tasks.values() if t.state == "running" and t.proc]
            for t in running:
                t.state = "paused"
        for t in running:
            self._signal(t, signal.SIGSTOP)

    def resume_all(self) -> None:
        with self.lock:
            paused = [t for t in self.tasks.values() if t.state == "paused"]
            for t in paused:
                t.state = "running"
        for t in paused:
            self._signal(t, signal.SIGCONT)

    def yield_all(self, reason: str) -> int:
        """Give every unfinished task back to its coordinator, which runs it elsewhere."""
        with self.lock:
            active = [t for t in self.tasks.values() if t.state in ACTIVE]
            for t in active:
                t.state, t.error = "yielded", reason
        for t in active:
            if t.proc and t.proc.poll() is None:
                self._signal(t, signal.SIGCONT)  # a stopped process can't act on SIGTERM
                _kill(t.proc)
        return len(active)

    def check_owner(self, now: float | None = None) -> None:
        """One step of the owner watch (the loop calls this every OWNER_POLL seconds)."""
        now = time.time() if now is None else now
        p = self.cfg["owner"]
        with self.lock:
            live = [t for t in self.tasks.values() if t.state in ("running", "paused") and t.proc]
        reason = self.watch.busy({t.proc.pid for t in live}, any(t.state == "paused" for t in live))
        if reason:
            self._calm_since = None
            if not self.owner_busy:
                self._busy_since = now
            self.owner_busy = reason
            self.pause_all()
            if now - self._busy_since >= float(p["yield_after"]):
                self.yield_all(f"yielded: {reason}")
        elif self.owner_busy:
            self._calm_since = self._calm_since or now
            if now - self._calm_since >= float(p["calm_seconds"]):
                self.owner_busy = ""
                self.resume_all()

    def _owner_loop(self) -> None:
        while not self.stopped.wait(OWNER_POLL):
            try:
                self.check_owner()
            except Exception:  # measuring must never take the worker down
                pass

    def drain(self, reason: str) -> int:
        self.draining = reason
        return self.yield_all(f"yielded: {reason}")


def _touch(path: Path) -> None:
    try:
        os.utime(path)
    except OSError:
        pass


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
    timeout = 60  # a connection silent this long is dropped

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            # Handshake here, in the request's own thread, so a slow client can't stall accept().
            self.request.settimeout(self.timeout)
            self.request.do_handshake()
        super().setup()

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
        if isinstance(self.connection, ssl.SSLSocket):
            der = self.connection.getpeercert(binary_form=True)
            if not der or pki.fingerprint_der(der) in set(self.worker.cfg.get("revoked", [])):
                self.close_connection = True
                self._reply(401, {"error": "certificate revoked"})
                return False
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
        ok = len(p) == 2 and p[0] == "blobs" and SHA.match(p[1]) and self.worker.has_blob(p[1])
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
        refusal = self.worker.refusal(sysinfo.power())
        if refusal:
            return self._reply(503, {"error": f"not accepting work ({refusal})"})
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
        if len(p) == 2 and p[0] == "blobs" and SHA.match(p[1]):
            if self.worker.delete_blob(p[1]):
                return self._reply(200, {"deleted": p[1]})
            return self._reply(409, {"error": "blob in use"})
        self._reply(404, {"error": "no such task"})

    def _send_file(self, path: Path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, wire.CHUNK)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Refused handshakes (wrong CA, plain HTTP, port scans) and dropped clients are routine.
        if os.environ.get("MACMESH_DEBUG"):
            super().handle_error(request, client_address)


def make_server(worker: Worker, host: str, port: int, tls: ssl.SSLContext | None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"worker": worker})
    server = Server((host, port), handler)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server


def serve(cfg: dict, host: str = "0.0.0.0", advertise: bool = True) -> None:
    worker = Worker(cfg)
    tls = pki.context(server=True) if cfg.get("tls", True) else None
    server = make_server(worker, host, int(cfg["port"]), tls)
    ad = None
    if advertise:
        from . import discovery
        ad = discovery.advertise(cfg)

    def on_term(signum, frame):
        # Logout, shutdown or launchctl stop: hand work back at once, then let pollers see it.
        n = worker.drain("worker shutting down")
        print(f"shutting down: handed back {n} task(s)", flush=True)
        threading.Thread(target=lambda: (time.sleep(2), server.shutdown()), daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    hw = sysinfo.hardware()
    print(f"macmesh worker '{cfg['node_name']}' on :{cfg['port']}  "
          f"[{hw['model']} · {hw['chip']} · score {hw['score']} · {cfg['slots']} slot(s)]")
    print(f"programs: {', '.join(sorted(worker.programs())) or '(none found)'}")
    print(f"transport: {'mutual TLS' if tls else 'plain HTTP (tls is off)'} · "
          f"sandbox: {'on' if worker.sandboxed else 'OFF'} · owner mode: {cfg['owner'].get('mode')} · "
          f"priority: {cfg.get('priority', 'normal')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        worker.drain("worker stopped")
    finally:
        worker.stopped.set()
        if ad:
            ad.close()
        server.server_close()
