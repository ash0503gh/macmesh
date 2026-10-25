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
  invite                  a one-time code for `macmesh join CODE` (the app's Join asks instead)
  approve {id, allow}     let a Mac that asked to join in, or not (status.invite.requests)
  revoke {fingerprint}
  ai / set_ai {agent?, autorun?}   which AI agent is used (Claude Code or Antigravity), autorun of safe plans
  plan {request, paths?, macs?}    an AI plan for a free-form request; safe ones start at once
  run_plan {id}                    start a plan that waited for a person's OK
  teach {program, name, sample}    an AI-written app profile, kept only if a test render passes
  explain {id}                     what went wrong with a failed job, in plain words
  ai_task {id}                     how an AI task is going (they take a while)

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

from . import __version__, ai, config, firewall, jobspec, media, mesh, pairing, pki, profiles, teach, toolinfo
from .planners import plan_app, plan_batch, plan_video
from .planners.ai import MEDIA_EXT, plan_to_job, plan_with_agent, unsafe_reasons
from .planners.app import probe_fps, probe_frames
from .planners.range import NEEDS_FPS
from .client import NodeError
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
        self.events: list[tuple[float, str]] = []      # the log with times, for the activity feed
        self.load: dict[str, list[dict]] = {}          # node id -> [{"at", "cpu", "gpu"}], last 2 minutes

    def note(self, line: str) -> None:
        self.log.append(line)
        self.events.append((time.time(), line.strip()))
        del self.events[:-200]

    def watch_load(self) -> None:
        """Every 2 s while the job runs, each Mac's CPU used by this job and its GPU load."""
        while self.state in ("starting", "running"):
            sched = self.scheduler
            if sched:
                def sample(n):
                    try:
                        u = n.client.usage()
                    except (NodeError, OSError):
                        return
                    points = self.load.setdefault(n.client.info.get("node_id") or n.client.address, [])
                    points.append({"at": round(time.time(), 1), "cpu": u.get("jobs", {}).get(sched.job_id, 0.0),
                                   "gpu": u.get("gpu"), "cores": u.get("cores"),
                                   "duty": u.get("duty"), "limit": u.get("limit")})
                    del points[:-60]
                threads = [threading.Thread(target=sample, args=(n,), daemon=True) for n in list(sched.nodes)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(3)
            time.sleep(2)

    def summary(self) -> dict:
        tasks = self.scheduler.tasks if self.scheduler else []
        done = sum(t.state == "done" for t in tasks)
        total = len(tasks) or len(self.job["tasks"])
        elapsed = (self.finished or time.time()) - self.started if self.started else 0.0
        progress, eta = (done / total if total else 0.0), None
        if self.state == "running" and self.scheduler:
            progress, eta = self.scheduler.estimate(elapsed)
        elif self.state in ("merging", "done"):
            progress = 1.0
        nodes, chunks = [], []
        if self.scheduler:
            names = {n.client.address: n.client.name for n in self.scheduler.nodes}
            now = time.time()
            for n in self.scheduler.nodes:
                up, down = n.rates()
                doing = [{"label": _label(t), **{k: (round(now - v, 1) if k == "since" else v)
                                                for k, v in t.phase[n.client.address].items()}}
                         for t in tasks if n.client.address in t.phase]
                node_id = n.client.info.get("node_id")
                nodes.append({"node_id": node_id, "name": n.client.name,
                              "done": n.completed, "running": n.in_flight, "doing": doing,
                              "sent": n.sent, "received": n.received, "up_rate": round(up), "down_rate": round(down),
                              "load": list(self.load.get(node_id or n.client.address, [])),
                              "installing": sorted(n.installing)})
            for t in tasks:  # one entry per chunk, in order: which Mac did it, or is doing it
                where = t.done_by if t.state == "done" else next(iter(t.copies), "")
                step = t.phase.get(where, {})
                chunks.append({"state": t.state, "mac": names.get(where, ""), "label": _label(t),
                               "phase": "done" if t.state == "done" else step.get("phase", "waiting"),
                               "seconds": round(t.elapsed, 1) if t.state == "done" else
                               (round(now - t.copies[where], 1) if where in t.copies else None),
                               "attempts": t.attempts})
        compute = sum(t.elapsed for t in tasks)
        return {"id": self.id, "name": self.name, "state": self.state, "error": self.error,
                "output": self.output, "tasks": total, "done": done,
                "progress": round(progress, 3), "elapsed": round(elapsed, 1),
                "eta": round(eta) if eta is not None else None, "macs": nodes, "chunks": chunks, "created": self.created, "kind": self.spec.get("kind"),
                # Chunk time added up over wall time: how many Macs' worth of work ran at once.
                "speedup": round(compute / elapsed, 1) if elapsed > 1 and compute else None,
                "log": self.log[-20:],
                "events": [{"at": round(at, 1), "text": _named(text, tasks)} for at, text in self.events[-80:]]}


def _named(line: str, tasks) -> str:
    """A log line with chunk ids (b00005, f000050-000099) replaced by what they work on."""
    labels = {t.id: _label(t) for t in tasks}
    return re.sub(r"\b([bf]\d{5,6}(?:-\d{6})?)\b", lambda m: labels.get(m.group(1), m.group(1)), line)


def _label(t) -> str:
    """A chunk as a person would name it: the file it works on, or its frames."""
    if t.spec.get("label"):
        return t.spec["label"]
    m = re.match(r"^f(\d+)-(\d+)$", t.id)
    if m:
        return f"Frames {int(m.group(1))}\u2013{int(m.group(2))}"
    inputs = t.spec.get("inputs", {})
    if len(inputs) == 1:
        return Path(next(iter(inputs.values()))).name
    return t.id


class AiTask:
    """Something an AI agent is working on (a plan, a new app's profile, an explanation): slow, so polled."""

    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.state = "working"  # working | done | failed
        self.steps: list[str] = []
        self.result: dict | None = None
        self.error = ""
        self.pending = None  # a plan waiting for a person's OK: (job, spec, final)
        self.created = time.time()

    def step(self, text: str) -> None:
        self.steps.append(text)

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.kind, "state": self.state, "steps": self.steps[-12:],
                "result": self.result, "error": self.error, "awaiting_ok": self.pending is not None}


class Control:
    def __init__(self, cfg: dict, worker=None, home: Path | None = None):
        self.cfg, self.worker, self.home = cfg, worker, home or config.home()
        self.jobs: dict[str, Job] = {}
        self.ai_tasks: dict[str, AiTask] = {}
        self.lock = threading.Lock()
        self.nodes: list = []
        self.nodes_at = 0.0
        self.pairing: pairing.PairingServer | None = None
        self.pairing_ad = None
        self.pairing_problem = ""
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
                    fps = probe_fps(prof, str(p), prof.get("vars", {}), self.cfg, self.home) if needs_fps else None
                    info["frames"] = probe_frames(prof, str(p), prof.get("vars", {}), self.cfg, fps, self.home)
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
               | {"kind": "video", "path", "preset"?, "vf"?}
               | {"kind": "batch", "files", "template"?, "out_ext"?, "name"?}
               plus "title"? and "macs"? (node ids)."""
        out_root = Path(self.cfg.get("output_dir", "~/Movies/macmesh")).expanduser()
        kind, path = spec.get("kind"), spec.get("path")
        if kind == "batch":
            files = spec.get("files") or ([path] if path else [])
            if not files:
                raise ControlError("say which files to work on (files)")
            stem = spec.get("name") or spec.get("title") or "batch"
            workdir = Path(spec.get("workdir") or (out_root / ".work" / f"{stem}-{uuid.uuid4().hex[:6]}")).expanduser().resolve()
            template = spec.get("template") or ["demucs-stem", "{in:file}", "{out:result}"]
            out_ext = spec.get("out_ext", ".zip")
            job = plan_batch(files, template, out_ext=out_ext, name=stem, workdir=str(workdir))
            final_out = spec.get("workdir") or str(out_root / f"{stem} (macmesh)")
            return {"job": jobspec.validate(job), "final": str(final_out)}

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
                           workdir=str(workdir), cfg=self.cfg, home=self.home)
        elif kind == "video":
            duration = media.summary(path)["duration"]
            chunk = float(spec.get("chunk") or max(5.0, duration / (3 * slots)))
            job = plan_video(path, f"{stem}.mp4", preset=spec.get("preset", "hevc"),
                             vf=spec.get("vf", ""), chunk_seconds=chunk, workdir=str(workdir))
            job.pop("final")
        else:
            raise ControlError("kind is \"app\", \"video\" or \"batch\"")
        # The result lands as "<name> (macmesh)" plus its own extension (or as a folder).
        return {"job": jobspec.validate(job), "final": str(out_root / f"{stem} (macmesh)")}

    @staticmethod
    def _chosen(mac: dict, spec: dict) -> bool:
        return not spec.get("macs") or mac["node_id"] in spec["macs"]

    def m_start(self, **spec) -> dict:
        planned = self.plan_job(spec)
        return self._start(planned["job"], spec, planned["final"]).summary()

    def m_submit(self, job: dict, final: str | None = None, kind: str | None = None, title: str | None = None,
                 options: dict | None = None) -> dict:
        """A job already planned elsewhere (the command line): run it here like any other, so the app
        shows it live. `final` is the exact output path, or None to leave results in the job's workdir.
        options: max_attempts, speculate, match, keep_blobs (the Scheduler's)."""
        try:
            job = jobspec.validate(job)
        except (ValueError, KeyError) as e:
            raise ControlError(f"not a valid job: {e}") from None
        spec = {"kind": kind or "job", "title": title or job["name"], "exact_final": True,
                "options": {k: v for k, v in (options or {}).items()
                            if k in ("max_attempts", "speculate", "match", "keep_blobs") and v is not None}}
        return self._start(job, spec, final).summary()

    def _start(self, job: dict, spec: dict, final: str | None) -> Job:
        record = Job(job, spec)
        with self.lock:
            self.jobs[record.id] = record
        threading.Thread(target=self._run, args=(record, final), daemon=True).start()
        return record

    def _run(self, record: Job, final: str | None) -> None:
        try:
            clients = [c for c in mesh.find_nodes(self.cfg, seconds=2.0)
                       if not record.spec.get("macs") or c.info["node_id"] in record.spec["macs"]]
            if not clients:
                raise JobFailed("no Macs are available")
            record.scheduler = Scheduler(record.job, clients, log=record.note,
                                         reference=toolinfo.fingerprints(self.cfg, self.home),
                                         origin={"node_id": self.cfg["node_id"], "name": self.cfg["node_name"]},
                                         installer=lambda client, prog: client.request_install(prog, self.cfg["node_name"]),
                                         **record.spec.get("options", {}))
            if record.cancel_requested:  # cancelled while the Macs were being found
                raise JobFailed("cancelled")
            record.state, record.started = "running", time.time()
            threading.Thread(target=record.watch_load, daemon=True).start()
            record.scheduler.run()
            record.state = "merging"
            workdir = Path(record.job["workdir"])
            media.run_merge(workdir, record.job["merge"], log=record.note)
            produced = next((workdir / (s.get("output") or s.get("into"))
                             for s in record.job["merge"] if s.get("output") or s.get("into")), None)
            if not (produced and produced.exists()):
                produced = workdir / "results" if (workdir / "results").is_dir() else workdir  # batch jobs
            if final is None:  # the command line's "outputs in <workdir>"
                record.output = str(produced)
            else:
                if record.spec.get("exact_final"):  # the command line names the file, as `-o` always has
                    dest = Path(final).expanduser()
                    if dest.is_file() and dest.resolve() != produced.resolve():
                        dest.unlink()
                else:
                    dest = _free_name(Path(final + (produced.suffix if produced.is_file() else "")))
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.resolve() != produced.resolve():
                    shutil.move(str(produced), dest)
                record.output = str(dest)
                if produced != workdir:
                    shutil.rmtree(workdir, ignore_errors=True)
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
            jobs = [j.summary() for j in self.jobs.values()]
        return sorted(jobs + self._lending(), key=lambda j: j.get("created") or 0, reverse=True)

    def _lending(self) -> list:
        """Work this Mac is doing for other Macs right now, one entry per job they asked for."""
        if not self.worker:
            return []
        with self.worker.lock:
            tasks = [t for t in self.worker.tasks.values() if t.state in ("queued", "running", "paused")]
        groups: dict[str, dict] = {}
        for t in tasks:
            origin = t.spec.get("origin") or {}
            if origin.get("node_id") == self.cfg["node_id"]:
                continue  # this Mac's own jobs show as jobs
            key = origin.get("job_id") or t.id.split("-")[0]
            g = groups.setdefault(key, {
                "id": f"lend-{key}", "kind": "lending", "state": "running", "error": "", "output": "",
                "name": f"For {origin.get('name') or 'another Mac'}: {origin.get('job') or 'a job'}",
                "tasks": 0, "done": 0, "progress": 0.0, "elapsed": 0.0, "eta": None, "speedup": None,
                "created": t.started or time.time(), "chunks": [],
                "macs": [{"name": self.cfg["node_name"], "done": 0, "running": 0}]})
            g["tasks"] += 1
            g["macs"][0]["running"] += t.state == "running"
            g["chunks"].append({"state": "running" if t.state == "running" else "pending", "mac": self.cfg["node_name"]})
            if t.started:
                g["created"] = min(g["created"], t.started)
                g["elapsed"] = round(max(g["elapsed"], time.time() - t.started), 1)
            if t.state == "paused":
                g["state"] = "paused"
        return list(groups.values())

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

    def m_owner(self, mode: str | None = None, limit: float | None = None) -> dict:
        """How this Mac lends: "shared" or "idle", and the most of it (0.5-0.95) the whole Mac may use."""
        changes = {}
        if mode is not None:
            if mode not in ("shared", "idle"):
                raise ControlError("mode is shared or idle")
            changes["mode"] = mode
        if limit is not None:
            if not 0.5 <= float(limit) <= 0.95:
                raise ControlError("limit is between 0.5 and 0.95")
            changes["limit"] = round(float(limit), 2)
        self.cfg["owner"].update(changes)
        if self.worker:
            self.worker.cfg["owner"].update(changes)  # its owner watch and governor read this dict live
        saved = config.load()
        saved["owner"].update(changes)
        config.save(saved)
        return self.m_status()

    def m_status(self) -> dict:
        me = self.worker.info() if self.worker else {}
        return {"node_id": self.cfg["node_id"], "name": self.cfg["node_name"], "lend": self.cfg.get("lend", True),
                "accepting": me.get("accepting"), "reason": me.get("refusal", ""),
                "running": me.get("running", 0), "paused": me.get("paused", 0),
                "owner_mode": self.cfg["owner"].get("mode"), "tls": bool(self.cfg.get("tls", True)),
                "limit": float(self.cfg["owner"].get("limit", 0.9)),
                "duty": round(self.worker.governor.duty, 2) if self.worker else 1.0,
                "holds_ca": pki.paths(self.home)["ca_key"].exists(), "invite": self._invite_state(),
                "fingerprint": pki.own_fingerprint(self.home) if pki.ready(self.home) else "",
                "mesh": self._mesh_id(), "firewall": firewall.state(), "version": __version__,
                "installs": self.worker.installer.list() if self.worker else [],
                "output_dir": str(Path(self.cfg.get("output_dir", "~/Movies/macmesh")).expanduser()),
                "macs": self.m_macs(), "jobs": self.m_jobs()[:10]}

    # ---- pairing ---------------------------------------------------------
    def start_pairing(self, port: int | None = None, advertise: bool = True) -> None:
        """On the Mac that holds the CA: listen for new Macs all the time, and say so over Bonjour."""
        if self.pairing or not pki.paths(self.home)["ca_key"].exists():
            return
        try:
            self.pairing = pairing.PairingServer(self.cfg, int(self.cfg["port"]) + 1 if port is None else port, self.home,
                                                 seen=self._seen_ids)
        except OSError as e:
            self.pairing_problem = f"can't listen for new Macs: {e}"
            return
        if advertise:
            from . import PAIR_SERVICE_TYPE, discovery
            self.pairing_ad = discovery.advertise(self.cfg, PAIR_SERVICE_TYPE, self.pairing.port,
                                                  properties={"mesh": self._mesh_id()})

    def _seen_ids(self) -> set:
        with self.lock:
            return {c.info.get("node_id") for c in self.nodes}

    def _mesh_id(self) -> str:
        p = pki.paths(self.home)["ca_crt"]
        return pki.fingerprint_pem(p.read_text())[:12] if p.exists() else ""

    def stop_pairing(self) -> None:
        if self.pairing_ad:
            self.pairing_ad.close()
            self.pairing_ad = None
        if self.pairing:
            self.pairing.close()
            self.pairing = None

    def _invite_state(self) -> dict:
        if not self.pairing:
            return {"code": None, "state": None, "expires": None, "joined": None, "error": self.pairing_problem or None,
                    "requests": [], "full": False, "max_macs": pairing.MAX_MACS}
        code, expires = self.pairing.code_state()
        recent = [j for j in self.pairing.joined if time.time() - j["at"] < 120]
        return {"code": code, "state": "waiting" if code else ("paired" if recent else None),
                "expires": expires or None, "joined": recent[-1]["name"] if recent else None,
                "error": self.pairing.last_error or None, "requests": self.pairing.pending(),
                "full": self.pairing.full(), "max_macs": pairing.MAX_MACS}

    def m_invite(self) -> dict:
        """A one-time code, for joining from the command line (the app asks to join instead)."""
        if not self.pairing:
            raise ControlError(self.pairing_problem or "only the Mac that started the mesh can add Macs")
        if self.pairing.full():
            raise ControlError(self.pairing._full_error())
        code, _ = self.pairing.code_state()
        if not code:
            self.pairing.set_code()
        return self._invite_state()

    def m_approve(self, id: str, allow: bool) -> dict:
        """Let a Mac that asked to join in, or not."""
        if not self.pairing or not self.pairing.decide(id, allow):
            raise ControlError("that request has gone (it may have timed out)")
        return self._invite_state()

    def m_install(self, program: str, allow: bool) -> dict:
        """Let another Mac's job set up a program here, or not (the owner's answer to the request)."""
        if not self.worker or not self.worker.installer.decide(program, allow):
            raise ControlError("that install request has gone")
        return {"program": program, "allowed": bool(allow)}

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

    def m_stop(self) -> dict:
        def kill():
            time.sleep(0.1)
            os._exit(0)
        threading.Thread(target=kill, daemon=True).start()
        return {"stopping": True}

    # ---- AI: planning, teaching, explaining ------------------------------
    def _ai(self, kind: str, work) -> dict:
        task = AiTask(kind)
        with self.lock:
            self.ai_tasks[task.id] = task

        def run():
            try:
                task.result = work(task)
                task.state = "done"
            except (ai.AgentError, teach.TeachError, ValueError, ControlError, JobFailed, OSError, KeyError) as e:
                task.state, task.error = "failed", str(e)
            except Exception as e:  # reported, never raised into a dead thread
                task.state, task.error = "failed", f"{type(e).__name__}: {e}"

        threading.Thread(target=run, daemon=True).start()
        return task.summary()

    def m_ai(self) -> dict:
        try:
            using, problem = ai.pick(self.cfg), ""
        except ai.AgentError as e:
            using, problem = None, str(e)
        return {"choice": self.cfg.get("agent", "auto"), "using": using, "problem": problem,
                "available": sorted(ai.available(self.cfg)), "autorun": bool(self.cfg.get("ai_autorun", True))}

    def m_set_ai(self, agent: str | None = None, autorun: bool | None = None) -> dict:
        saved = config.load()
        if agent is not None:
            if agent != "auto" and agent not in self.cfg["agents"]:
                raise ControlError(f"agent is auto or one of {', '.join(sorted(self.cfg['agents']))}")
            self.cfg["agent"] = saved["agent"] = agent
        if autorun is not None:
            self.cfg["ai_autorun"] = saved["ai_autorun"] = bool(autorun)
        config.save(saved)
        return self.m_ai()

    def m_plan(self, request: str, paths: list | None = None, macs: list | None = None) -> dict:
        """Ask the agent to plan a free-form request. Safe plans start at once when autorun is on."""
        paths = [str(Path(p).expanduser()) for p in (paths or [])]
        if not request.strip():
            raise ControlError("say what you'd like done")

        def work(task: AiTask) -> dict:
            task.step(f"Asking {ai.pick(self.cfg)} to plan it")
            nodes = [c.info for c in self.refresh() if not macs or c.info["node_id"] in macs]
            if not nodes:
                raise ControlError("no Macs are available")
            plan = plan_with_agent(request, paths, nodes, self.cfg, home=self.home)
            task.step("Checking the plan")
            name = re.sub(r"[^A-Za-z0-9 _.-]+", "", Path(paths[0]).stem if paths else request)[:40].strip() or "job"
            out_root = Path(self.cfg.get("output_dir", "~/Movies/macmesh")).expanduser()
            workdir = out_root / ".work" / f"{name}-{task.id}"
            job = jobspec.validate(plan_to_job(plan, paths, self.cfg, workdir=str(workdir), home=self.home))
            reasons = unsafe_reasons(job, set(profiles.load(self.home)))
            first = job["tasks"][0]
            result = {"explanation": plan.get("explanation", ""), "planner": plan["planner"],
                      "tasks": len(job["tasks"]), "programs": sorted({p for t in job["tasks"] for p in jobspec.programs(t)}),
                      "example": [" ".join(step) for step in first["steps"]],
                      "merge": [m["op"] for m in job["merge"]], "safe": not reasons, "reasons": reasons, "job_id": None}
            spec = {"kind": "plan", "title": name, "macs": macs, "request": request, "paths": paths}
            final = str(out_root / f"{name} (macmesh)")
            if not reasons and self.cfg.get("ai_autorun", True):
                result["job_id"] = self._start(job, spec, final).id
                task.step("Started")
            else:
                task.pending = (job, spec, final)
            return result

        return self._ai("plan", work)

    def m_run_plan(self, id: str) -> dict:
        task = self._ai_task(id)
        if not task.pending:
            raise ControlError("that plan has already run, or didn't finish")
        record = self._start(*task.pending)
        task.pending = None
        task.result["job_id"] = record.id
        return record.summary()

    def m_teach(self, program: str, name: str, sample: str) -> dict:
        return self._ai("teach", lambda task: teach.teach(program, name, sample, self.cfg, self.home, log=task.step))

    def m_explain(self, id: str) -> dict:
        record = self._job(id)
        if record.state != "failed":
            raise ControlError("only failed jobs need explaining")
        app = record.spec.get("app") or (self.m_inspect(record.spec["path"]).get("app")
                                         if record.spec.get("kind") == "app" else None)
        profile = profiles.load(self.home).get(app) if app else None

        def work(task: AiTask) -> dict:
            task.step(f"Asking {ai.pick(self.cfg)} what went wrong")
            return ai.explain(record.summary(), record.spec, profile, self.cfg)

        return self._ai("explain", work)

    def _ai_task(self, id: str) -> AiTask:
        with self.lock:
            if id not in self.ai_tasks:
                raise ControlError(f"no AI task {id!r}")
            return self.ai_tasks[id]

    def m_ai_task(self, id: str) -> dict:
        return self._ai_task(id).summary()

    # ---- dispatch --------------------------------------------------------
    METHODS = {"status", "macs", "apps", "inspect", "start", "submit", "jobs", "job", "cancel", "lend", "owner", "invite", "approve",
               "install", "revoke", "stop",
               "ai", "set_ai", "plan", "run_plan", "teach", "explain", "ai_task"}

    def call(self, method: str, params: dict):
        if method not in self.METHODS:
            raise ControlError(f"unknown method {method!r}")
        return getattr(self, f"m_{method}")(**(params or {}))


def _placeholders(prof: dict) -> set[str]:
    names = set()
    for step in (prof.get("range") or {}).get("steps", []):
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
            except Exception as e:
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
