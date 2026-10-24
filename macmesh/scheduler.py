"""Pull-based dispatcher: every Mac takes the next chunk as soon as it has room.

No fixed 40/60 split. A fanless Air that throttles simply comes back for work
less often, and a Mac that disappears (lid closed, Wi-Fi dropped) costs one
chunk, which is re-queued elsewhere. At the tail of the job, idle Macs make
duplicate copies of straggling chunks (first result wins).

A Mac counts as gone after HEARTBEAT_MISSES status polls in a row go
unanswered (a couple of seconds), and a task it hands back because its owner
needs it ("yielded") goes straight back in the queue. Tasks only go to Macs
whose build of the program matches the coordinator's (see toolinfo).
"""

from __future__ import annotations

import os
import shutil
import statistics
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import jobspec, toolinfo, wire
from .client import NodeClient, NodeError, TaskRejected

POLL = 0.5
HEARTBEAT_MISSES = 2  # status polls in a row a Mac may miss before its task moves elsewhere
NODE_COOLDOWN = 10.0
BUSY_COOLDOWN = 30.0  # a Mac that refused work (owner busy, battery, disk) is asked again after this
MAX_NODE_ERRORS = 6  # per task: give up if Macs keep vanishing


class JobFailed(Exception):
    pass


@dataclass
class TaskState:
    spec: dict
    state: str = "pending"  # pending | running | done | failed
    attempts: int = 0
    node_errors: int = 0
    failed_on: set = field(default_factory=set)
    excluded: set = field(default_factory=set)  # nodes that can't run it at all
    copies: dict = field(default_factory=dict)  # node address -> start time
    done_by: str = ""
    elapsed: float = 0.0
    last_error: str = ""

    @property
    def id(self) -> str:
        return self.spec["id"]


@dataclass
class NodeState:
    client: NodeClient
    in_flight: int = 0
    cooldown_until: float = 0.0
    probing: bool = False
    speeds: list = field(default_factory=list)  # cost units per second, measured
    completed: int = 0
    busy_seconds: float = 0.0
    mismatched: set = field(default_factory=set)  # programs whose build differs from the coordinator's

    @property
    def capacity(self) -> int:
        # One extra so the next chunk uploads while the current one computes.
        return int(self.client.info.get("slots", 1)) + 1

    @property
    def score(self) -> float:
        return float(self.client.info.get("hardware", {}).get("score", 1.0))

    def can_run(self, spec: dict) -> bool:
        have = self.client.info.get("programs", [])
        return all(p in have and p not in self.mismatched for p in jobspec.programs(spec))


class Scheduler:
    def __init__(self, job: dict, clients: list[NodeClient], log=print,
                 max_attempts: int = 3, speculate: bool = True, reference: dict | None = None,
                 match: str = "version", keep_blobs: bool = False):
        self.job = job
        self.job_id = uuid.uuid4().hex[:8]
        self.workdir = Path(job["workdir"]).expanduser().resolve()
        self.tasks = [TaskState(t) for t in job["tasks"]]
        self.nodes = [NodeState(c) for c in clients]
        self.log = log
        self.max_attempts = max_attempts
        self.speculate = speculate
        self.cond = threading.Condition()
        self.blob_cache: dict[str, tuple[str, str, Path]] = {}
        self.blob_lock = threading.Lock()
        self.upload_locks: dict[tuple[str, str], threading.Lock] = {}
        self.staging = Path(tempfile.mkdtemp(prefix="macmesh-"))
        self.fatal: str = ""
        self.aborted = False
        # The coordinator's own builds; None = don't compare (e.g. this Mac lacks the tools).
        self.reference = reference
        self.match = match
        self.keep_blobs = keep_blobs

    # ---- public -------------------------------------------------------
    def run(self) -> dict:
        if not self.nodes:
            raise JobFailed("no worker nodes available")
        self._check_builds()
        for t in self.tasks:
            if not any(n.can_run(t.spec) for n in self.nodes):
                missing = [p for p in jobspec.programs(t.spec)
                           if not any(p in n.client.info.get("programs", []) and p not in n.mismatched for n in self.nodes)]
                prog = (missing or jobspec.programs(t.spec))[0]
                if any(prog in n.mismatched for n in self.nodes):
                    raise JobFailed(f"{t.id}: every Mac with '{prog}' has a different build from this one "
                                    "(install the same version, or use --match none)")
                raise JobFailed(f"{t.id}: no node has '{prog}' installed/allowed")
        self.workdir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        self.log(f"job {self.job['name']} ({self.job_id}): {len(self.tasks)} tasks on "
                 + ", ".join(f"{n.client.name}[{n.score}]" for n in self.nodes))
        try:
            with self.cond:
                while not self._finished():
                    if self.fatal:
                        raise JobFailed(self.fatal)
                    self._dispatch()
                    self.cond.wait(timeout=1.0)
        finally:
            self._cancel_all()
            if not self.keep_blobs:
                self._forget_blobs()
            shutil.rmtree(self.staging, ignore_errors=True)
        wall = time.time() - started
        return {
            "wall_seconds": wall,
            "compute_seconds": sum(t.elapsed for t in self.tasks),
            "nodes": {n.client.name: {"tasks": n.completed, "busy_seconds": n.busy_seconds}
                      for n in self.nodes},
        }

    def _check_builds(self) -> None:
        if self.reference is None or self.match == "none":
            return
        programs = {p for t in self.tasks for p in jobspec.programs(t.spec)}
        for prog in sorted(programs):
            ours = self.reference.get(prog)
            if ours is None:
                self.log(f"  ? {prog} isn't on this Mac: can't check the other Macs' builds of it")
                continue
            for n in self.nodes:
                theirs = n.client.info.get("fingerprints", {}).get(prog)
                if prog in n.client.info.get("programs", []) and not toolinfo.same_build(ours, theirs, self.match):
                    n.mismatched.add(prog)
                    seen = toolinfo.describe(theirs) if theirs else "unknown build"
                    self.log(f"  ✗ {n.client.name}: {prog} is {seen}, this Mac has {toolinfo.describe(ours)}; "
                             "not using it for these tasks")

    def _forget_blobs(self) -> None:
        """Ask every Mac to delete what this job uploaded: media shouldn't linger on lent Macs."""
        shas = {sha for sha, _, _ in self.blob_cache.values()}
        for node in self.nodes:
            for sha in shas:
                node.client.delete_blob(sha)

    def cancel(self) -> None:
        """Stop the job: run() raises JobFailed("cancelled") and the remote tasks are cancelled."""
        with self.cond:
            self.fatal = "cancelled"
            self.cond.notify_all()

    # ---- scheduling (called with self.cond held) -----------------------
    def _finished(self) -> bool:
        return all(t.state == "done" for t in self.tasks)

    def _dispatch(self) -> None:
        now = time.time()
        for node in sorted(self.nodes, key=self._speed, reverse=True):
            if node.cooldown_until > now:
                continue
            if node.cooldown_until and not node.probing:
                self._probe(node)  # back from cooldown: re-check it's alive
                continue
            if node.probing:
                continue
            while node.in_flight < node.capacity:
                task = self._pick(node)
                if task is None:
                    break
                self._start(task, node)
            if node.in_flight == 0 and self.speculate:
                task = self._straggler(node)
                if task:
                    self.log(f"  ↻ {task.id}: duplicating on {node.client.name} (straggler)")
                    self._start(task, node)

    def _pick(self, node: NodeState) -> TaskState | None:
        pending = [t for t in self.tasks if t.state == "pending"
                   and node.client.address not in t.excluded and node.can_run(t.spec)]
        if not pending:
            return None
        # Prefer not to retry a failed task on the node it failed on.
        def untried_elsewhere(t: TaskState) -> bool:
            return any(n is not node and not n.cooldown_until and n.can_run(t.spec)
                       and n.client.address not in t.failed_on | t.excluded for n in self.nodes)

        pending = ([t for t in pending if node.client.address not in t.failed_on]
                   or [t for t in pending if not untried_elsewhere(t)])
        if not pending:
            return None
        # Strong Macs take the biggest chunks, weaker ones the smallest.
        speeds = [self._speed(n) for n in self.nodes]
        strong = self._speed(node) >= statistics.median(speeds)
        pending.sort(key=lambda t: t.spec["cost"], reverse=strong)
        return pending[0]

    def _speed(self, node: NodeState) -> float:
        """Measured cost/second, or a score-based estimate until measured."""
        if node.speeds:
            return statistics.mean(node.speeds[-5:])
        measured = [n for n in self.nodes if n.speeds]
        if measured:
            k = statistics.mean(statistics.mean(n.speeds[-5:]) / n.score for n in measured)
            return k * node.score
        return node.score

    def _straggler(self, idle: NodeState) -> TaskState | None:
        if any(t.state == "pending" for t in self.tasks):
            return None
        if not any(n.speeds for n in self.nodes):
            return None  # no timings yet: can't tell slow from normal
        now = time.time()
        best, best_gain = None, 0.0
        for t in self.tasks:
            if t.state != "running" or len(t.copies) != 1 or not idle.can_run(t.spec):
                continue
            if idle.client.address in t.copies or idle.client.address in t.excluded:
                continue
            addr, start = next(iter(t.copies.items()))
            runner = next(n for n in self.nodes if n.client.address == addr)
            expected = t.spec["cost"] / self._speed(runner)
            elapsed = now - start
            remaining = max(expected - elapsed, 0.0)
            if elapsed > 1.5 * expected:  # overdue: assume it's stuck or throttled
                remaining = elapsed
            fresh_run = t.spec["cost"] / self._speed(idle)
            gain = remaining - fresh_run
            if gain > max(5.0, 0.3 * fresh_run) and gain > best_gain:
                best, best_gain = t, gain
        return best

    def _start(self, task: TaskState, node: NodeState) -> None:
        task.state = "running"
        task.copies[node.client.address] = time.time()
        node.in_flight += 1
        threading.Thread(target=self._attempt, args=(task, node), daemon=True).start()

    def _probe(self, node: NodeState) -> None:
        node.probing = True

        def probe():
            try:
                node.client.fetch_info()
                ok = node.client.info.get("accepting", True)
            except NodeError:
                ok = False
            with self.cond:
                node.probing = False
                if ok:
                    node.cooldown_until = 0.0
                    self.log(f"  ✓ {node.client.name} is available again")
                else:
                    node.cooldown_until = time.time() + NODE_COOLDOWN
                self.cond.notify_all()

        threading.Thread(target=probe, daemon=True).start()

    # ---- one attempt of one task on one node (worker thread) -----------
    def _attempt(self, task: TaskState, node: NodeState) -> None:
        c = node.client
        remote_id = f"{self.job_id}-{task.id}-{uuid.uuid4().hex[:6]}"
        submitted = False
        outcome, detail, elapsed = "error", "", 0.0
        try:
            t0 = time.time()
            inputs = {}
            for name, local in task.spec["inputs"].items():
                sha, kind, path = self._blob(local)
                inputs[name] = {"blob": sha, "kind": kind, "filename": Path(local).name}
                self._ensure_uploaded(c, sha, path)
            upload_s = time.time() - t0
            c.submit({
                "task_id": remote_id, "steps": task.spec["steps"], "fail_if": task.spec.get("fail_if", []),
                "inputs": inputs,
                "outputs": {k: Path(v).name for k, v in task.spec["outputs"].items()},
                "timeout": task.spec.get("timeout"),
            })
            submitted = True
            self.log(f"  → {task.id} on {c.name}" + (f" (upload {upload_s:.1f}s)" if upload_s > 0.5 else ""))
            misses = 0
            while True:
                if self.aborted or self._done_elsewhere(task, c.address):
                    outcome = "superseded"
                    return
                try:
                    st = c.status(remote_id)
                    misses = 0
                except NodeError:
                    misses += 1  # one slow answer on busy Wi-Fi isn't a dead Mac; two in a row is
                    if misses >= HEARTBEAT_MISSES:
                        raise
                    continue
                if st["state"] == "done":
                    elapsed = st["elapsed"]
                    break
                if st["state"] == "yielded":
                    outcome, detail = "yielded", st["error"]
                    return
                if st["state"] in ("failed", "cancelled"):
                    outcome, detail = "failed", f"{st['error']}\n{st['log_tail'][-1500:]}"
                    return
                time.sleep(POLL)  # queued, running or paused (paused for long enough looks like a straggler)
            if self._done_elsewhere(task, c.address):
                outcome = "superseded"
                return
            self._fetch_outputs(task, c, remote_id, st["outputs"])
            outcome = "done"
        except TaskRejected as e:
            outcome = "busy" if e.code == 503 else ("excluded" if e.code == 403 else "error")
            detail = str(e)
        except (NodeError, OSError, ValueError, KeyError) as e:
            outcome, detail = "error", str(e)
        finally:
            if submitted and outcome == "error":
                # The Mac may be gone: don't hold up re-queuing on a cleanup call that will time out.
                threading.Thread(target=c.delete, args=(remote_id,), daemon=True).start()
            elif submitted:
                c.delete(remote_id)
            self._record(task, node, outcome, detail, elapsed)

    def _done_elsewhere(self, task: TaskState, address: str) -> bool:
        with self.cond:
            return task.state == "done" and task.done_by != address

    def _blob(self, local: str) -> tuple[str, str, Path]:
        """Hash an input once per job; directories are packed into a tar first."""
        key = str(Path(local).resolve())
        with self.blob_lock:
            if key not in self.blob_cache:
                p = Path(key)
                if p.is_dir():
                    tar = self.staging / f"{len(self.blob_cache)}.tar"
                    wire.tar_dir(p, tar)
                    self.blob_cache[key] = (wire.sha256_file(tar), "dir", tar)
                else:
                    self.blob_cache[key] = (wire.sha256_file(p), "file", p)
            return self.blob_cache[key]

    def _ensure_uploaded(self, c: NodeClient, sha: str, path: Path) -> None:
        with self.blob_lock:
            lock = self.upload_locks.setdefault((c.address, sha), threading.Lock())
        with lock:
            if not c.has_blob(sha):
                c.put_blob(sha, path)

    def _fetch_outputs(self, task: TaskState, c: NodeClient, remote_id: str, manifest: dict) -> None:
        tmpdir = Path(tempfile.mkdtemp(dir=self.workdir, prefix=".dl-"))
        try:
            staged = {}
            for name, dest in task.spec["outputs"].items():
                meta = manifest[name]
                got = tmpdir / name
                c.download(remote_id, name, got)
                if wire.sha256_file(got) != meta["sha256"]:
                    raise NodeError(f"{c.address}: output {name} corrupted in transfer")
                if meta["kind"] == "dir":
                    wire.safe_extract(got, tmpdir / f"{name}.d")
                    got = tmpdir / f"{name}.d"
                staged[dest] = got
            with self.cond:
                if task.state == "done":  # a duplicate won the race
                    return
                for dest, got in staged.items():
                    final = self.workdir / dest
                    final.parent.mkdir(parents=True, exist_ok=True)
                    if final.is_dir():
                        shutil.rmtree(final)
                    elif final.exists():
                        final.unlink()
                    os.replace(got, final)
                task.state, task.done_by = "done", c.address
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _record(self, task: TaskState, node: NodeState, outcome: str, detail: str, elapsed: float) -> None:
        with self.cond:
            node.in_flight -= 1
            task.copies.pop(node.client.address, None)
            name = node.client.name
            if outcome == "done":
                task.elapsed = elapsed
                node.completed += 1
                node.busy_seconds += elapsed
                if elapsed > 0:
                    node.speeds.append(task.spec["cost"] / elapsed)
                done = sum(t.state == "done" for t in self.tasks)
                self.log(f"  ✓ {task.id} on {name} in {elapsed:.1f}s  [{done}/{len(self.tasks)}]")
            elif outcome == "superseded" or task.state == "done":
                pass
            elif task.copies:
                pass  # another copy is still running; let it finish
            elif outcome == "failed":
                task.attempts += 1
                task.failed_on.add(node.client.address)
                task.last_error = detail
                self.log(f"  ✗ {task.id} failed on {name} (attempt {task.attempts}/{self.max_attempts})")
                if task.attempts >= self.max_attempts:
                    task.state = "failed"
                    self.fatal = f"task {task.id} failed {task.attempts} times. Last error:\n{detail}"
                else:
                    task.state = "pending"
            elif outcome == "excluded":
                task.excluded.add(node.client.address)
                task.state = "pending"
                if all(n.client.address in task.excluded or not n.can_run(task.spec) for n in self.nodes):
                    self.fatal = f"task {task.id}: no node will run it ({detail})"
            elif outcome == "yielded":  # the Mac's owner needs it, or it's shutting down
                task.state = "pending"
                node.cooldown_until = time.time() + BUSY_COOLDOWN
                self.log(f"  ↩ {name} handed back {task.id} ({detail.removeprefix('yielded: ')}); re-queued")
            else:  # node trouble: unreachable, refusing work, lost task
                task.state = "pending"
                task.node_errors += outcome == "error"
                wait = BUSY_COOLDOWN if outcome == "busy" else NODE_COOLDOWN
                node.cooldown_until = time.time() + wait
                self.log(f"  ! {name}: {detail.splitlines()[0] if detail else outcome}; "
                         f"re-queued {task.id}, pausing node {wait:.0f}s")
                if task.node_errors >= MAX_NODE_ERRORS:
                    self.fatal = f"task {task.id}: nodes kept failing ({detail})"
            if task.state == "running" and not task.copies:
                task.state = "pending"
            self.cond.notify_all()

    def _cancel_all(self, grace: float = 15.0) -> None:
        """Stop remaining attempts and wait (briefly) for them to clean up remotely."""
        deadline = time.time() + grace
        with self.cond:
            self.aborted = True
            while any(n.in_flight for n in self.nodes) and time.time() < deadline:
                self.cond.wait(timeout=0.5)
