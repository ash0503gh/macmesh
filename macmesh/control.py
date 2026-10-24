"""The daemon's local control channel, for the menu bar app and the MCP server.

`macmesh serve` listens on ~/.macmesh/control.sock (a Unix socket only this
user can open). Each request is one JSON line, {"id", "method", "params"};
each reply is one JSON line, {"id", "result"} or {"id", "error"}. Jobs started
here run inside the daemon, so the app and agents see the same jobs.

Methods:
  status                  this Mac, lending, pairing, Macs nearby, jobs
  macs                    the Macs on the mesh (refreshed in the background)
  apps                    app profiles, and whether this Mac has each app
  inspect {path}          what a dropped file is: which app, how many frames
  start {spec}            start a job; spec as for plan_job(); returns the job
  jobs                    every job this daemon ran, newest first
  job {id} / cancel {id}  one job / stop it
  lend {on}               let other Macs use this one, or not (remembered)
  owner {mode}            "shared" (step aside when the owner is busy) or "idle" (only while away)
  invite / revoke {fingerprint}

Joining a mesh happens before the daemon can run (it needs the certificate):
the app runs `macmesh init` or `macmesh join CODE` first.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import socketserver
import threading
import time
import uuid
from pathlib import Path

from . import config, jobspec, media, mesh, pairing, pki, profiles, toolinfo
from .planners import plan_app, plan_video
from .planners.ai import MEDIA_EXT
from .planners.app import probe_fps, probe_frames
from .planners.range import NEEDS_FPS
from .scheduler import JobFailed, Scheduler

VIDEO_EXT = MEDIA_EXT - {".wav", ".aif", ".aiff", ".mp3", ".m4a"}
REFRESH = 10.0
MAX_LINE = 1 << 20


class ControlError(Exception):
    pass


def socket_path(home: Path | None = None) -> Path:
    return (home or config.home()) / "control.sock"


class Job:
    def __init__(self, job: dict, spec: dict):
        self.id = uuid.uuid4().hex[:8]
        self.job, self.spec = job, spec
        self.name = spec.get("title") or job["name"]
        self.state = "starting"  # starting | running | merging | done | failed | cancelled
        self.error = ""
        self.output = ""
        self.created = time.time()
        self.started = self.finished = 0.0
        self.scheduler: Scheduler | None = None
        self.cancel_requested = False
        self.log: list[str] = []

    def summary(self) -> dict:
        tasks = self.scheduler.tasks if self.scheduler else []
        done = sum(t.state == "done" for t in tasks)
        total = len(tasks) or len(self.job["tasks"])
        elapsed = (self.finished or time.time()) - self.started if self.started else 0.0
        eta = None
        if self.state == "running" and done:
            eta = round(elapsed / done * (total - done))
        nodes, chunks = [], []
        if self.scheduler:
            names = {n.client.address: n.client.name for n in self.scheduler.nodes}
            for n in self.scheduler.nodes:
                nodes.append({"node_id": n.client.info.get("node_id"), "name": n.client.name,
                              "done": n.completed, "running": n.in_flight})
            for t in tasks:  # one entry per chunk, in order: which Mac did it, or is doing it
                where = t.done_by if t.state == "done" else next(iter(t.copies), "")
                chunks.append({"state": t.state, "mac": names.get(where, "")})
        compute = sum(t.elapsed for t in tasks)
        return {"id": self.id, "name": self.name, "state": self.state, "error": self.error,
                "output": self.output, "tasks": total, "done": done,
                "progress": round(done / total, 3) if total else 0.0, "elapsed": round(elapsed, 1),
                "eta": eta, "macs": nodes, "chunks": chunks, "created": self.created, "kind": self.spec.get("kind"),
                # Chunk time added up over wall time: how many Macs' worth of work ran at once.
                "speedup": round(compute / elapsed, 1) if elapsed > 1 and compute else None,
                "log": self.log[-20:]}


class Control:
    def __init__(self, cfg: dict, worker=None, home: Path | None = None):
        self.cfg, self.worker, self.home = cfg, worker, home or config.home()
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.nodes: list = []
        self.nodes_at = 0.0
        self.invite: dict = {}
        self.stopped = threading.Event()
        threading.Thread(target=self._refresh_loop, daemon=True).start()

    # ---- the Macs ------------------------------------------------------
    def _refresh_loop(self) -> None:
        while not self.stopped.is_set():
            self.refresh()
            self.stopped.wait(REFRESH)

    def refresh(self) -> list:
        try:
            found = mesh.find_nodes(self.cfg, seconds=2.0)
        except Exception:  # discovery must never take the daemon down
            found = []
        with self.lock:
            self.nodes, self.nodes_at = found, time.time()
        return found

    def _mac(self, c) -> dict:
        i = c.info
        hw, pw = i.get("hardware", {}), i.get("power", {})
        state = "working" if i.get("running") else "paused" if i.get("paused") else "ready"
        if not i.get("accepting", True):
            state = "unavailable"
        return {"node_id": i.get("node_id"), "name": i.get("node_name"), "address": c.address,
                "this_mac": i.get("node_id") == self.cfg["node_id"], "model": hw.get("model"),
                "chip": hw.get("chip"), "fanless": hw.get("fanless", False), "score": hw.get("score"),
                "cores": (hw.get("perf_cores") or 0) + (hw.get("eff_cores") or 0), "memory_gb": hw.get("memory_gb"),
                "slots": i.get("slots"), "state": state, "reason": i.get("refusal", ""),
                "on_battery": pw.get("on_battery"), "battery_pct": pw.get("battery_pct"),
                "programs": i.get("programs", []), "sandboxed": i.get("sandboxed", False),
                "fingerprint": c.fingerprint}

    def m_macs(self) -> list:
        with self.lock:
            nodes = list(self.nodes)
        return [self._mac(c) for c in nodes]

    # ---- what this Mac knows -------------------------------------------
    def m_apps(self) -> list:
        here, fps = toolinfo.programs(self.cfg, self.home), toolinfo.fingerprints(self.cfg, self.home)
        return [{"name": name, "description": p.get("description", ""), "installed": name in here,
                 "build": toolinfo.describe(fps[name]) if name in fps else "",
                 "extensions": p.get("extensions", []), "vars": p.get("vars", {})}
                for name, p in sorted(profiles.load(self.home).items())]

    def m_inspect(self, path: str) -> dict:
        p = Path(path).expanduser()
        if not p.exists():
            raise ControlError(f"there's no file at {p}")
        suffix = p.suffix.lower()
        for name, prof in profiles.load(self.home).items():
            if suffix in prof.get("extensions", []):
                info = {"kind": "app", "app": name, "path": str(p.resolve()), "name": p.stem,
                        "description": prof.get("description", ""), "vars": prof.get("vars", {}),
                        "needs_vars": sorted(v for v in _placeholders(prof) if v not in prof.get("vars", {})),
                        "installed": name in toolinfo.programs(self.cfg, self.home)}
                try:
                    needs_fps = any(NEEDS_FPS.search(a) for s in prof["range"]["steps"] for a in s)
                    fps = probe_fps(prof, str(p), prof.get("vars", {}), self.cfg) if needs_fps else None
                    info["frames"] = probe_frames(prof, str(p), prof.get("vars", {}), self.cfg, fps)
                    info["fps"] = fps
                except ValueError as e:
                    info["frames"], info["note"] = None, str(e)
                return info
        if suffix in VIDEO_EXT:
            s = media.summary(p)
            if s["video"]:
                return {"kind": "video", "path": str(p.resolve()), "name": p.stem, "duration": s["duration"],
                        "video": s["video"], "audio": s["audio"]}
        raise ControlError(f"macmesh doesn't know how to split {p.name} yet: add an app profile for it")

    # ---- jobs ------------------------------------------------------------
    def plan_job(self, spec: dict) -> dict:
        """spec: {"kind": "app", "path", "app"?, "start"?, "end"?, "vars"?, "chunk"?}
               | {"kind": "video", "path", "preset"?, "vf"?}  plus "title"? and "macs"? (node ids)."""
        out_root = Path(self.cfg.get("output_dir", "~/Movies/macmesh")).expanduser()
        kind, path = spec.get("kind"), spec.get("path")
        if not path:
            raise ControlError("say which file to work on (path)")
        stem = Path(path).stem
        workdir = out_root / ".work" / f"{stem}-{uuid.uuid4().hex[:6]}"
        slots = sum(int(m.get("slots") or 1) for m in self.m_macs() if self._chosen(m, spec)) or 1
        if kind == "app":
            info = self.m_inspect(path) if not spec.get("app") or spec.get("end") is None else {}
            app = spec.get("app") or info.get("app")
            start = int(spec.get("start", 0))
            end = spec.get("end")
            if end is None:
                if not info.get("frames"):
                    raise ControlError(info.get("note") or "give the last frame (end)")
                end = info["frames"] - 1
            end = int(end)
            # At least three chunks per slot, so a slow Mac never holds up the end for long.
            chunk = int(spec.get("chunk") or max(12, math.ceil((end - start + 1) / (3 * slots))))
            job = plan_app(app, path, start, end, chunk=chunk, fps=spec.get("fps"), vars=spec.get("vars"),
                           workdir=str(workdir), cfg=self.cfg)
            movie = any(m["op"] == "concat_video" for m in job["merge"])
            final = out_root / (f"{stem} (macmesh).mov" if movie else f"{stem} (macmesh)")
        elif kind == "video":
            duration = media.summary(path)["duration"]
            chunk = float(spec.get("chunk") or max(5.0, duration / (3 * slots)))
            job = plan_video(path, str(out_root / f"{stem} (macmesh).mp4"), preset=spec.get("preset", "hevc"),
                             vf=spec.get("vf", ""), chunk_seconds=chunk, workdir=str(workdir))
            final = Path(job.pop("final"))
        else:
            raise ControlError("kind is \"app\" or \"video\"")
        return {"job": jobspec.validate(job), "final": str(final)}

    @staticmethod
    def _chosen(mac: dict, spec: dict) -> bool:
        return not spec.get("macs") or mac["node_id"] in spec["macs"]

    def m_start(self, **spec) -> dict:
        planned = self.plan_job(spec)
        record = Job(planned["job"], spec)
        with self.lock:
            self.jobs[record.id] = record
        threading.Thread(target=self._run, args=(record, planned["final"]), daemon=True).start()
        return record.summary()

    def _run(self, record: Job, final: str) -> None:
        try:
            clients = [c for c in mesh.find_nodes(self.cfg, seconds=2.0)
                       if not record.spec.get("macs") or c.info["node_id"] in record.spec["macs"]]
            if not clients:
                raise JobFailed("no Macs are available")
            record.scheduler = Scheduler(record.job, clients, log=record.log.append,
                                         reference=toolinfo.fingerprints(self.cfg, self.home))
            if record.cancel_requested:  # cancelled while the Macs were being found
                raise JobFailed("cancelled")
            record.state, record.started = "running", time.time()
            record.scheduler.run()
            record.state = "merging"
            workdir = Path(record.job["workdir"])
            media.run_merge(workdir, record.job["merge"], log=record.log.append)
            produced = next((workdir / (s.get("output") or s.get("into"))
                             for s in record.job["merge"] if s.get("output") or s.get("into")), None)
            if produced and produced.exists() and str(produced) != final:
                dest = _free_name(Path(final))
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(produced), dest)
                record.output = str(dest)
                shutil.rmtree(workdir, ignore_errors=True)
            else:
                record.output = str(produced or workdir)
            record.state = "done"
        except JobFailed as e:
            record.state = "cancelled" if str(e) == "cancelled" else "failed"
            record.error = "" if record.state == "cancelled" else str(e)
        except Exception as e:  # anything else is reported, not raised into a dead thread
            record.state, record.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            record.finished = time.time()

    def m_jobs(self) -> list:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
        return [j.summary() for j in jobs]

    def _job(self, id: str) -> Job:
        with self.lock:
            if id not in self.jobs:
                raise ControlError(f"no job {id!r}")
            return self.jobs[id]

    def m_job(self, id: str) -> dict:
        return self._job(id).summary()

    def m_cancel(self, id: str) -> dict:
        record = self._job(id)
        if record.state in ("starting", "running"):
            record.cancel_requested = True
            if record.scheduler:
                record.scheduler.cancel()
        return record.summary()

    # ---- this Mac -------------------------------------------------------
    def m_lend(self, on: bool) -> dict:
        self.cfg["lend"] = bool(on)
        if self.worker:
            self.worker.cfg["lend"] = bool(on)
        saved = config.load() if config.config_path().exists() else dict(self.cfg)
        saved["lend"] = bool(on)
        config.save(saved)
        return self.m_status()

    def m_owner(self, mode: str) -> dict:
        if mode not in ("shared", "idle"):
            raise ControlError("mode is shared or idle")
        self.cfg["owner"]["mode"] = mode
        if self.worker:
            self.worker.cfg["owner"]["mode"] = mode  # its owner watch reads this dict live
        saved = config.load()
        saved["owner"]["mode"] = mode
        config.save(saved)
        return self.m_status()

    def m_status(self) -> dict:
        me = self.worker.info() if self.worker else {}
        return {"node_id": self.cfg["node_id"], "name": self.cfg["node_name"], "lend": self.cfg.get("lend", True),
                "accepting": me.get("accepting"), "reason": me.get("refusal", ""),
                "running": me.get("running", 0), "paused": me.get("paused", 0),
                "owner_mode": self.cfg["owner"].get("mode"), "tls": bool(self.cfg.get("tls", True)),
                "holds_ca": pki.paths(self.home)["ca_key"].exists(), "invite": self._invite_state(),
                "fingerprint": pki.own_fingerprint(self.home) if pki.ready(self.home) else "",
                "output_dir": str(Path(self.cfg.get("output_dir", "~/Movies/macmesh")).expanduser()),
                "macs": self.m_macs(), "jobs": self.m_jobs()[:10]}

    # ---- pairing ---------------------------------------------------------
    def _invite_state(self) -> dict:
        return {k: v for k, v in self.invite.items() if k != "obj"}

    def m_invite(self) -> dict:
        if not pki.paths(self.home)["ca_key"].exists():
            raise ControlError("only the Mac that started the mesh can invite (run `macmesh init` there)")
        code = pairing.new_code()
        inv = pairing.Invite(self.cfg, code, int(self.cfg["port"]) + 1, home=self.home)
        from . import PAIR_SERVICE_TYPE, discovery
        ad = discovery.advertise(self.cfg, PAIR_SERVICE_TYPE, inv.port)
        self.invite = {"code": code, "state": "waiting", "expires": time.time() + pairing.EXPIRY, "joined": ""}

        def wait():
            try:
                self.invite.update(state="paired", joined=inv.wait())
            except pairing.PairingError as e:
                self.invite.update(state="failed", error=str(e))
            finally:
                if ad:
                    ad.close()

        threading.Thread(target=wait, daemon=True).start()
        return self._invite_state()

    def m_revoke(self, fingerprint: str) -> dict:
        fp = fingerprint.lower().replace(":", "")
        if len(fp) != 64:
            raise ControlError("give the full 64-character certificate fingerprint")
        saved = config.load()
        if fp not in saved["revoked"]:
            saved["revoked"].append(fp)
        config.save(saved)
        self.cfg["revoked"] = saved["revoked"]
        return {"revoked": fp}

    # ---- dispatch --------------------------------------------------------
    METHODS = {"status", "macs", "apps", "inspect", "start", "jobs", "job", "cancel", "lend", "owner", "invite", "revoke"}

    def call(self, method: str, params: dict):
        if method not in self.METHODS:
            raise ControlError(f"unknown method {method!r}")
        return getattr(self, f"m_{method}")(**(params or {}))


def _placeholders(prof: dict) -> set[str]:
    names = set()
    for step in prof["range"]["steps"]:
        for a in step:
            names |= set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", a))
    return names - {"start", "end", "end_excl", "count", "index", "start_s", "end_s", "fps", "indir"}


def _free_name(path: Path) -> Path:
    """`path`, or "name 2.mov", "name 3.mov"... if it's taken."""
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} {n}{path.suffix}")
        n += 1
    return candidate


# ---- transport -------------------------------------------------------------
class _Handler(socketserver.StreamRequestHandler):
    control: Control

    def handle(self):
        for raw in self.rfile:
            if len(raw) > MAX_LINE:
                return
            try:
                req = json.loads(raw)
                rid = req.get("id")
            except ValueError:
                self._send({"id": None, "error": "not JSON"})
                continue
            try:
                result = self.control.call(req.get("method", ""), req.get("params") or {})
                self._send({"id": rid, "result": result})
            except (ControlError, ValueError, FileNotFoundError, JobFailed, pairing.PairingError,
                    pki.PKIError, TypeError) as e:
                self._send({"id": rid, "error": str(e)})

    def _send(self, obj) -> None:
        self.wfile.write(json.dumps(obj).encode() + b"\n")
        self.wfile.flush()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(control: Control, path: Path | None = None) -> _Server:
    """Listen on the control socket in a background thread; only this user can connect."""
    path = path or socket_path(control.home)
    if len(str(path).encode()) > 103:  # sun_path holds 104 bytes on macOS
        raise ControlError(f"{path} is too long for a Unix socket: use a shorter MACMESH_HOME")
    path.unlink(missing_ok=True)
    old = os.umask(0o177)
    try:
        server = _Server(str(path), type("H", (_Handler,), {"control": control}))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ControlClient:
    """Talks to a running daemon's control socket."""

    def __init__(self, path: Path | None = None, timeout: float = 30):
        self.path, self.timeout = path or socket_path(), timeout
        self.next_id = 0

    def call(self, method: str, **params):
        self.next_id += 1
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(str(self.path))
                s.sendall(json.dumps({"id": self.next_id, "method": method, "params": params}).encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            raise ControlError("macmesh isn't running on this Mac: open the macmesh app, or run `macmesh serve`") from e
        reply = json.loads(buf)
        if "error" in reply:
            raise ControlError(reply["error"])
        return reply["result"]
