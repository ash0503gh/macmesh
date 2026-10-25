"""Run with:  python3 -m unittest discover -s tests -v   (needs ffmpeg on PATH)"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path

from macmesh import ai, config, control, jobspec, media, mediarefs, owner, pairing, pki, profiles, sandbox, sysinfo, toolinfo, wire
from macmesh import mcp as mcp_module
from macmesh import worker as worker_module
from macmesh.client import NodeClient, NodeError, TaskRejected
from macmesh.planners import plan_app, plan_batch, plan_range, plan_video
from macmesh.planners.range import exact_fps
from macmesh.planners.ai import extract_plan
from macmesh.scheduler import JobFailed, Scheduler
from macmesh import scheduler as scheduler_module
from macmesh.worker import Worker, make_server

TOKEN = "test-token"
HAVE_FFMPEG = bool(shutil.which("ffmpeg"))

FAKE_RENDERER = """#!/bin/sh
# fake app: render <project> --start S --end E --output DIR
proj=$2; start=$4; end=$6; out=$8
test -f "$proj/project.json" || exit 3
[ -f "$proj/fail_at_$start" ] && exit 9
sleep "${FAKE_DELAY:-0}"
mkdir -p "$out"
i=$start; while [ $i -le $end ]; do echo "$i" > "$out/frame_$(printf %05d $i).txt"; i=$((i+1)); done
"""


class Node:
    """An in-process worker on an ephemeral port, standing in for one Mac.

    The owner watch is off unless `owner` is given (the test machine's real load
    would pause tasks at random); with tls=True the node's identity must already
    be in <root>/<name>/tls.
    """

    def __init__(self, root: Path, name: str, tools: dict, slots: int = 1, tls: bool = False,
                 owner: dict | None = None, owner_sample=None, **extra):
        self.home = root / name
        os.environ["MACMESH_HOME"] = str(self.home)
        cfg = config.defaults()
        cfg.update(node_name=name, token=TOKEN, slots=slots, port=0, tools=tools, **extra)
        cfg["owner"].update(owner or {"mode": "off"})
        self.worker = Worker(cfg, home=self.home, owner_sample=owner_sample)
        self.server = make_server(self.worker, "127.0.0.1", 0, pki.context(True, self.home) if tls else None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.address = f"127.0.0.1:{self.server.server_address[1]}"

    def client(self, tls=None, address: str = "", **kw) -> NodeClient:
        c = NodeClient(address or self.address, TOKEN, timeout=kw.pop("timeout", 5), tls=tls, **kw)
        c.fetch_info()
        return c

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class WireTests(unittest.TestCase):
    def test_signature_roundtrip_and_tamper(self):
        body = b'{"a":1}'
        h = wire.auth_headers(TOKEN, "POST", "/tasks", body)
        self.assertTrue(wire.verify(TOKEN, "POST", "/tasks", h, body))
        self.assertFalse(wire.verify("other", "POST", "/tasks", h, body))
        self.assertFalse(wire.verify(TOKEN, "POST", "/tasks", h, b'{"a":2}'))
        self.assertFalse(wire.verify(TOKEN, "DELETE", "/tasks", h, body))

    def test_old_timestamp_rejected(self):
        ts = str(int(time.time()) - 3600)
        bh = wire.hashlib.sha256(b"").hexdigest()
        h = {"X-Mesh-Time": ts, "X-Mesh-Body": bh, "X-Mesh-Sig": wire.sign(TOKEN, "GET", "/info", ts, bh)}
        self.assertFalse(wire.verify(TOKEN, "GET", "/info", h, b""))

    def test_safe_extract_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            evil = Path(d, "evil.tar")
            with tarfile.open(evil, "w") as tf:
                data = b"pwned"
                ti = tarfile.TarInfo("../../escape.txt")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            with self.assertRaises(Exception):
                wire.safe_extract(evil, Path(d, "out"))
            self.assertFalse(Path(d).parent.joinpath("escape.txt").exists())

    def test_tar_dir_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "Proj.bundle")
            (src / "a/b").mkdir(parents=True)
            (src / "a/b/x.txt").write_text("x")
            (src / "top.json").write_text("{}")
            wire.tar_dir(src, Path(d, "p.tar"))
            wire.safe_extract(Path(d, "p.tar"), Path(d, "out"))
            self.assertEqual(Path(d, "out/a/b/x.txt").read_text(), "x")
            self.assertTrue(Path(d, "out/top.json").exists())


class JobSpecTests(unittest.TestCase):
    def test_rejects_unmatched_placeholder(self):
        with self.assertRaisesRegex(jobspec.JobError, "no matching"):
            jobspec.validate({"tasks": [{"id": "t", "argv": ["ffmpeg", "{in:nope}"]}]})

    def test_rejects_escaping_output(self):
        with self.assertRaisesRegex(jobspec.JobError, "relative path"):
            jobspec.validate({"tasks": [{"id": "t", "argv": ["ffmpeg"], "outputs": {"o": "../x"}}]})

    def test_rejects_duplicate_outputs(self):
        with self.assertRaisesRegex(jobspec.JobError, "more than one task"):
            jobspec.validate({"tasks": [
                {"id": "a", "argv": ["x"], "outputs": {"o": "same.mp4"}},
                {"id": "b", "argv": ["x"], "outputs": {"o": "same.mp4"}}]})

    def test_range_chunks_cover_range_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "p.ecproj").mkdir()
            job = plan_range("effectcraft", str(Path(d, "p.ecproj")), 10, 254, ["{in:project}", "{start}", "{end}",
                             "{out:result}"], chunk=100)
        spans = [(int(t["steps"][0][2]), int(t["steps"][0][3])) for t in job["tasks"]]
        self.assertEqual(spans, [(10, 109), (110, 209), (210, 254)])

    def test_extract_plan_from_claude_wrapper_and_prose(self):
        inner = 'Sure!\n```json\n{"planner": "batch", "params": {"files": []}}\n```'
        self.assertEqual(extract_plan(json.dumps({"result": inner}))["planner"], "batch")
        self.assertEqual(extract_plan('text {"planner": "video", "params": {}} more')["planner"], "video")
        with self.assertRaises(ValueError):
            extract_plan("no json here")


class EstimateTests(unittest.TestCase):
    def test_a_chunk_queued_behind_another_is_not_counted_as_running(self):
        class Fake:
            address, name, info = "air:7477", "air", {"slots": 1, "hardware": {"score": 1.0}}
        job = {"name": "j", "workdir": tempfile.mkdtemp(), "merge": [],
               "tasks": [{"id": f"t{i}", "argv": ["x"], "cost": 10.0} for i in range(3)]}
        s = Scheduler(job, [Fake()])
        node, now = s.nodes[0], time.time()
        node.speeds = [1.0]  # 10 s per chunk
        s.tasks[0].copies["air:7477"] = now - 20  # running for 5 s: its Mac freed up 5 s ago
        s.tasks[1].copies["air:7477"] = now - 20  # queued behind it
        node.free_at = now - 5
        progress, _ = s.estimate(20)
        self.assertAlmostEqual(progress, 5 / 30, places=2)


class MeshCase(unittest.TestCase):
    """Two in-process Macs and a fake renderer; no tests of its own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        renderer = self.tmp / "fakeapp"
        renderer.write_text(FAKE_RENDERER)
        renderer.chmod(renderer.stat().st_mode | stat.S_IEXEC)
        self.tools = {"fakeapp": {"path": str(renderer)}}
        self.project = self.tmp / "Ad.ecproj"
        self.project.mkdir()
        (self.project / "project.json").write_text("{}")
        self.nodes = [Node(self.tmp, "pro", self.tools, slots=2), Node(self.tmp, "air", self.tools)]

    def tearDown(self):
        for n in self.nodes:
            n.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def range_job(self, frames=400, chunk=50, out="out"):
        return jobspec.validate(plan_range(
            "fakeapp", str(self.project), 0, frames - 1,
            ["render", "{in:project}", "--start", "{start}", "--end", "{end}", "--output", "{out:result}"],
            chunk=chunk, workdir=str(self.tmp / out)))

    def run_job(self, job, clients=None, **kw):
        sched = Scheduler(job, clients or [n.client() for n in self.nodes], log=kw.pop("log", lambda m: None), **kw)
        result = sched.run()
        media.run_merge(Path(job["workdir"]), job["merge"], log=lambda m: None)
        return result

    def blobs(self, node):
        return [b for b in os.listdir(node.worker.blobs) if not b.endswith(".d") and not b.startswith(".")]


class MeshTests(MeshCase):
    def test_bad_token_is_rejected(self):
        with self.assertRaises(NodeError):
            NodeClient(self.nodes[0].address, "wrong", timeout=5).fetch_info()

    def test_range_job_uses_both_nodes(self):
        result = self.run_job(self.range_job())
        frames = sorted(os.listdir(self.tmp / "out/frames"))
        self.assertEqual(len(frames), 400)
        self.assertEqual(frames[0], "frame_00000.txt")
        self.assertTrue(all(n["tasks"] > 0 for n in result["nodes"].values()), result)
        for n in self.nodes:  # remote task folders are cleaned up
            self.assertEqual(os.listdir(n.worker.work), [])

    def test_project_uploaded_once_per_node(self):
        self.run_job(self.range_job(), keep_blobs=True)
        for n in self.nodes:
            self.assertEqual(len(self.blobs(n)), 1)

    def test_uploads_are_deleted_after_the_job(self):
        self.run_job(self.range_job())
        for n in self.nodes:
            self.assertEqual(os.listdir(n.worker.blobs), [])

    def test_unused_blobs_expire(self):
        self.run_job(self.range_job(), keep_blobs=True)
        n = self.nodes[0]
        n.worker.cfg["blob_ttl"] = 3600
        self.assertEqual(n.worker.collect_garbage(), 0)
        n.worker.cfg["blob_ttl"] = -1  # everything counts as old
        self.assertEqual(n.worker.collect_garbage(), 1)
        self.assertEqual(os.listdir(n.worker.blobs), [])

    def test_failing_chunk_fails_job_with_log(self):
        (self.project / "fail_at_100").write_text("")
        with self.assertRaisesRegex(JobFailed, "f000100-000149 failed 3 times"):
            self.run_job(self.range_job())

    def test_node_dying_mid_job_requeues(self):
        os.environ["FAKE_DELAY"] = "0.3"
        try:
            job = self.range_job(frames=800)
            killer = threading.Timer(1.0, self.nodes[1].stop)
            killer.start()
            self.run_job(job)
            killer.join()
        finally:
            os.environ.pop("FAKE_DELAY")
        self.assertEqual(len(os.listdir(self.tmp / "out/frames")), 800)

    def test_disallowed_program_never_runs(self):
        job = jobspec.validate({"workdir": str(self.tmp / "o"), "tasks": [{"id": "t", "argv": ["rm", "-rf", "/"]}]})
        with self.assertRaisesRegex(JobFailed, "no node has 'rm'"):
            self.run_job(job)
        with self.assertRaises(Exception):  # and the worker refuses it directly too
            self.nodes[0].client().submit({"task_id": "x", "argv": ["rm", "-rf", "/"]})

    def test_batch_job(self):
        files = []
        for i in range(4):
            f = self.tmp / f"in{i}.txt"
            f.write_text(f"hello {i}")
            files.append(str(f))
        tools = dict(self.tools, cp={"path": shutil.which("cp")})
        for n in self.nodes:
            n.worker.cfg["tools"] = tools
        job = jobspec.validate(plan_batch(files, ["cp", "{in:file}", "{out:result}"], ".out",
                                          workdir=str(self.tmp / "b")))
        self.run_job(job)
        self.assertEqual((self.tmp / "b/results/in2.out").read_text(), "hello 2")

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_video_split_encode_stitch_is_frame_exact(self):
        src = self.tmp / "src.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "24", "-c:v", "libx264", "-g", "60",
                        "-c:a", "aac", "-shortest", str(src)], check=True)
        job = plan_video(str(src), str(self.tmp / "final.mp4"), preset="h264", chunk_seconds=4,
                         workdir=str(self.tmp / "v"))
        final = job.pop("final")
        self.run_job(jobspec.validate(job))
        out = self.tmp / "v" / Path(final).name
        count = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(count, "720")
        self.assertIsNotNone(media.summary(out)["audio"])


class Relay:
    """A TCP relay to a worker. cut() makes it swallow traffic, like a Mac that slept or left Wi-Fi."""

    def __init__(self, target: str):
        host, _, port = target.rpartition(":")
        self.target = (host, int(port))
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.address = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.cut_at = 0.0
        self.open: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def cut(self) -> None:
        self.cut_at = time.time()

    def _accept(self) -> None:
        while True:
            try:
                a, _ = self.sock.accept()
            except OSError:
                return
            self.open.append(a)
            if self.cut_at:
                continue  # accepted, never answered
            b = socket.create_connection(self.target)
            self.open.append(b)
            for src, dst in ((a, b), (b, a)):
                threading.Thread(target=self._pipe, args=(src, dst), daemon=True).start()

    def _pipe(self, src, dst) -> None:
        while True:
            try:
                data = src.recv(65536)
            except OSError:
                return
            if not data:
                return
            if not self.cut_at:
                try:
                    dst.sendall(data)
                except OSError:
                    return

    def close(self) -> None:
        self.sock.close()
        for s in self.open:
            s.close()


class OwnWorkTests(MeshCase):
    def test_a_mac_not_lending_still_runs_its_own_jobs(self):
        air = self.nodes[1]
        air.worker.cfg["lend"] = False
        mine = {"task_id": "own-1", "argv": ["fakeapp"], "origin": {"node_id": air.worker.cfg["node_id"]}}
        theirs = {"task_id": "their-1", "argv": ["fakeapp"], "origin": {"node_id": "someone-else"}}
        c = air.client()
        self.assertIn(c.submit(mine)["state"], ("queued", "running"))  # accepted
        with self.assertRaises(TaskRejected) as refused:
            c.submit(theirs)
        self.assertEqual(refused.exception.code, 503)
        self.assertIn("not lending", str(refused.exception))

    def test_a_job_no_mac_will_take_says_why(self):
        air = self.nodes[1]
        air.worker.cfg["lend"] = False
        lines = []
        sched = Scheduler(self.range_job(frames=50), [air.client()], log=lines.append)
        threading.Thread(target=lambda: self.assertRaises(JobFailed, sched.run), daemon=True).start()
        for _ in range(100):
            if any("waiting: no Mac will take work" in m for m in lines):
                break
            time.sleep(0.05)
        sched.cancel()
        self.assertTrue(any("waiting: no Mac will take work right now (air: not lending)" in m for m in lines), lines)


class InstallTests(MeshCase):
    """A Mac without the job's program is asked to set it up; its owner decides."""

    def setUp(self):
        super().setUp()
        self.nodes[1].stop()
        air = self.tmp / "air"
        (air / "apps").mkdir(parents=True, exist_ok=True)
        (air / "apps" / "fakeapp.json").write_text(json.dumps({
            "name": "fakeapp", "program": {"path": str(air / "tools" / "fakeapp" / "fakeapp")},
            "install": {"summary": "The fake renderer", "files": {"fakeapp": FAKE_RENDERER},
                        "executable": ["fakeapp"], "steps": [["{python}", "-c", "print('setting up')"]]}}))
        self.nodes[1] = Node(self.tmp, "air", {})

    def answer(self, allow: bool):
        installer = self.nodes[1].worker.installer

        def owner():
            for _ in range(200):
                if any(r["state"] == "asking" for r in installer.list()):
                    installer.decide("fakeapp", allow)
                    return
                time.sleep(0.02)
        threading.Thread(target=owner, daemon=True).start()

    def run_with_installs(self, job, clients=None):
        lines = []
        os.environ["FAKE_DELAY"] = "0.4"
        old, scheduler_module.INSTALL_RECHECK = scheduler_module.INSTALL_RECHECK, 0.3
        try:
            self.run_job(job, clients, log=lines.append,
                         installer=lambda client, prog: client.request_install(prog, "pro"))
        finally:
            os.environ.pop("FAKE_DELAY")
            scheduler_module.INSTALL_RECHECK = old
        return lines

    def test_an_allowed_install_joins_the_running_job(self):
        self.answer(True)
        lines = self.run_with_installs(self.range_job(frames=800))
        self.assertEqual(len(os.listdir(self.tmp / "out/frames")), 800)
        self.assertTrue(any("asked air to install fakeapp" in m for m in lines), lines)
        self.assertTrue(any("air installed fakeapp: using it now" in m for m in lines), lines)
        self.assertTrue(any(" on air in " in m for m in lines), lines)
        self.assertIn("fakeapp", self.nodes[1].worker.programs())

    def test_a_declined_install_leaves_the_mac_out(self):
        self.answer(False)
        lines = self.run_with_installs(self.range_job(frames=400))
        self.assertEqual(len(os.listdir(self.tmp / "out/frames")), 400)
        self.assertEqual(self.nodes[1].worker.installer.states(), {"fakeapp": "declined"})
        self.assertFalse(any(" on air in " in m for m in lines), lines)
        self.assertNotIn("fakeapp", self.nodes[1].worker.programs())

    def test_a_job_only_the_declining_mac_could_run_fails(self):
        self.answer(False)
        with self.assertRaisesRegex(JobFailed, "no node has 'fakeapp'"):
            self.run_with_installs(self.range_job(frames=100), clients=[self.nodes[1].client()])

    def test_the_program_appears_only_once_its_install_has_finished(self):
        air = self.tmp / "air"
        prof = json.loads((air / "apps" / "fakeapp.json").read_text())
        prof["install"]["steps"] = [["{python}", "-c", "import time; time.sleep(1.5)"]]
        (air / "apps" / "fakeapp.json").write_text(json.dumps(prof))
        installer = self.nodes[1].worker.installer
        installer.ask("fakeapp", "pro")
        installer.decide("fakeapp", True)
        time.sleep(0.5)
        self.assertEqual(installer.states(), {"fakeapp": "installing"})
        self.assertNotIn("fakeapp", self.nodes[1].worker.programs())  # not before it works
        for _ in range(100):
            if installer.states()["fakeapp"] != "installing":
                break
            time.sleep(0.05)
        self.assertEqual(installer.states(), {"fakeapp": "done"})
        self.assertIn("fakeapp", self.nodes[1].worker.programs())

    def test_only_this_macs_own_recipes_run(self):
        installer = self.nodes[1].worker.installer
        self.assertEqual(installer.ask("not-a-tool", "pro")["state"], "unavailable")
        self.assertEqual(installer.ask("fakeapp", "pro")["state"], "asking")
        self.assertFalse((self.tmp / "air" / "tools").exists())  # nothing runs before Allow


class ResilienceTests(MeshCase):
    """Macs that vanish, shut down, or whose owner needs them back."""

    def test_silent_node_is_dropped_within_seconds(self):
        os.environ["FAKE_DELAY"] = "0.3"
        relay = Relay(self.nodes[1].address)
        lines = []
        try:
            clients = [self.nodes[0].client(), self.nodes[1].client(address=relay.address, timeout=2)]
            threading.Timer(1.0, relay.cut).start()
            self.run_job(self.range_job(frames=800), clients=clients,
                         log=lambda m: lines.append((time.time(), m)))
        finally:
            os.environ.pop("FAKE_DELAY")
            relay.close()
        self.assertEqual(len(os.listdir(self.tmp / "out/frames")), 800)
        # Its work moves on after two missed 2 s polls (or a duplicate starts), not a 30 s socket timeout.
        moved = [t for t, m in lines if "re-queued" in m or "duplicating" in m]
        self.assertTrue(moved, lines)
        self.assertLess(moved[0] - relay.cut_at, 8.0, lines)

    def test_shutdown_hands_work_back(self):
        os.environ["FAKE_DELAY"] = "0.3"
        lines = []
        try:
            air = self.nodes[1].worker

            def drain_mid_task():
                while not any(t.state == "running" and t.proc for t in list(air.tasks.values())):
                    time.sleep(0.01)
                air.drain("worker shutting down")

            threading.Thread(target=drain_mid_task, daemon=True).start()
            self.run_job(self.range_job(frames=800), log=lines.append)
        finally:
            os.environ.pop("FAKE_DELAY")
        self.assertEqual(len(os.listdir(self.tmp / "out/frames")), 800)
        self.assertTrue(any("handed back" in m and "shutting down" in m for m in lines), lines)

    def test_busy_owner_gets_their_mac_back(self):
        busy = {"cpu": 0.0}
        self.nodes[1].stop()
        old_poll = worker_module.OWNER_POLL
        worker_module.OWNER_POLL = 0.1
        try:
            self.nodes[1] = Node(self.tmp, "air2", self.tools, owner={"mode": "shared", "yield_after": 0.3},
                                 owner_sample=lambda ours: {"cpu": busy["cpu"], "gpu": None, "idle": None})
            os.environ["FAKE_DELAY"] = "0.6"
            lines = []
            air = self.nodes[1].worker

            def busy_mid_task():
                while not any(t.state == "running" and t.proc for t in list(air.tasks.values())):
                    time.sleep(0.01)
                busy.update(cpu=0.9)

            threading.Thread(target=busy_mid_task, daemon=True).start()
            result = self.run_job(self.range_job(frames=800), log=lines.append)
        finally:
            worker_module.OWNER_POLL = old_poll
            os.environ.pop("FAKE_DELAY")
        self.assertEqual(len(os.listdir(self.tmp / "out/frames")), 800)
        self.assertTrue(any("handed back" in m and "owner using 90% CPU" in m for m in lines), lines)
        self.assertFalse(self.nodes[1].client().info["accepting"])


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_watch_rules(self):
        policy = dict(config.defaults()["owner"])  # shared, limit 90%
        sample = {"cpu": 0.5, "idle": 300.0}
        w = owner.OwnerWatch(policy, sample=lambda ours: sample)
        self.assertEqual(w.busy(set(), paused=False), "")  # the Governor makes room; no need to stop
        sample["cpu"] = 0.85  # leaves us under 10%: step aside
        self.assertIn("CPU", w.busy(set(), paused=False))
        idle = owner.OwnerWatch(dict(policy, mode="idle"), sample=lambda ours: {"cpu": 0, "idle": 5.0})
        self.assertIn("active", idle.busy(set(), paused=False))

    def test_governor_keeps_the_mac_under_the_limit(self):
        owner_load = {"cpu": 0.0, "gpu": 0.0}
        # Our task alone would use 30% of the CPU and 99% of the GPU; it gets `duty` of the time.
        g = owner.Governor({"limit": 0.9}, sample=lambda: {"cpu": min(1.0, owner_load["cpu"] + 0.3 * g.duty),
                                                           "gpu": min(1.0, owner_load["gpu"] + 0.99 * g.duty)})
        for _ in range(15):
            g.step()
        self.assertLessEqual(0.99 * g.duty, 0.91)  # the GPU held at about 90%...
        self.assertGreater(g.duty, 0.8)            # ...slowing only as much as needed
        owner_load["cpu"] = 0.8  # the owner starts something big: back right off
        for _ in range(10):
            g.step()
        self.assertLessEqual(0.8 + 0.3 * g.duty, 0.92)
        owner_load["cpu"] = 0.1  # they're done: speed up again
        for _ in range(15):
            g.step()
        self.assertGreater(g.duty, 0.8)

    def test_governor_slows_running_tasks(self):
        cfg = config.defaults()
        cfg.update(node_name="t", token=TOKEN, port=0, tools={"busy": {"path": "/bin/sh"}})
        w = Worker(cfg, home=self.tmp / "g", owner_sample=lambda ours: {"cpu": 0.0, "idle": None},
                   governor_sample=lambda: {"cpu": 0.0, "gpu": None})
        w.governor.duty = 0.25  # as if the Mac were busy: run a quarter of the time
        w.governor.step = w.governor.reset = lambda: 0.25
        task = w.submit({"task_id": "y", "argv": ["busy", "-c", "while :; do :; done"]})
        for _ in range(100):
            if task.proc:
                break
            time.sleep(0.05)
        time.sleep(1.0)
        t0 = sysinfo.process_table()[task.proc.pid][1]
        time.sleep(3.0)
        used = sysinfo.process_table()[task.proc.pid][1] - t0
        w.cancel("y")
        w.stopped.set()
        self.assertLess(used, 1.6)   # a quarter of 3 s, give or take; unthrottled would be ~3 s
        self.assertGreater(used, 0.3)

    def test_pause_resume_then_yield(self):
        sample = {"cpu": 0.0, "gpu": None, "idle": None}
        cfg = config.defaults()
        cfg.update(node_name="t", token=TOKEN, port=0, tools={"sleep": {"path": "/bin/sleep"}})
        cfg["owner"].update(mode="off", calm_seconds=10, yield_after=30)  # off: steps are driven by hand here
        w = Worker(cfg, home=self.tmp / "n", owner_sample=lambda ours: sample)
        task = w.submit({"task_id": "s", "argv": ["sleep", "30"]})
        for _ in range(100):
            if task.proc:
                break
            time.sleep(0.05)

        def proc_state():
            return subprocess.run(["ps", "-o", "stat=", "-p", str(task.proc.pid)], capture_output=True,
                                  text=True).stdout.strip()

        sample["cpu"] = 0.9
        w.check_owner(now=100)
        self.assertEqual(task.state, "paused")
        self.assertTrue(proc_state().startswith("T"), proc_state())  # stopped
        self.assertFalse(w.accepting({"on_battery": False, "battery_pct": None}))
        sample["cpu"] = 0.0
        w.check_owner(now=105)
        self.assertEqual(task.state, "paused")  # calm, but not for long enough yet
        w.check_owner(now=116)
        self.assertEqual(task.state, "running")
        self.assertFalse(proc_state().startswith("T"))
        sample["cpu"] = 0.9
        w.check_owner(now=120)
        w.check_owner(now=151)
        self.assertEqual(task.state, "yielded")
        task.proc.wait(timeout=10)
        w.stopped.set()


class BuildMatchTests(MeshCase):
    def other_build(self):
        other = self.tmp / "fakeapp-v2"
        other.write_text(FAKE_RENDERER + "# v2\n")
        other.chmod(other.stat().st_mode | stat.S_IEXEC)
        return other

    def test_mismatched_build_is_not_used(self):
        self.nodes[1].worker.cfg["tools"] = {"fakeapp": {"path": str(self.other_build())}}
        self.nodes[1].worker.fingerprints = toolinfo.fingerprints(self.nodes[1].worker.cfg)
        reference = toolinfo.fingerprints(self.nodes[0].worker.cfg)
        lines = []
        result = self.run_job(self.range_job(), reference=reference, log=lines.append)
        self.assertEqual(result["nodes"]["air"]["tasks"], 0, result)
        self.assertTrue(any("air: fakeapp is" in m and "not using it" in m for m in lines), lines)
        result = self.run_job(self.range_job(out="out2"), reference=reference, match="none")
        self.assertGreater(result["nodes"]["air"]["tasks"], 0, result)

    def test_no_matching_build_anywhere_fails_clearly(self):
        reference = {"fakeapp": {"sha256": "0" * 64, "version": ""}}
        with self.assertRaisesRegex(JobFailed, "different build"):
            self.run_job(self.range_job(), reference=reference)

    def test_version_beats_hash_when_both_known(self):
        a = {"sha256": "a" * 64, "version": "ffmpeg version 7.1"}
        self.assertTrue(toolinfo.same_build(a, {"sha256": "b" * 64, "version": "ffmpeg version 7.1"}, "version"))
        self.assertFalse(toolinfo.same_build(a, {"sha256": "b" * 64, "version": "ffmpeg version 7.1"}, "hash"))
        self.assertFalse(toolinfo.same_build(a, {"sha256": "b" * 64, "version": ""}, "version"))


class BlobInUseTests(MeshCase):
    def test_blob_in_use_is_not_deleted(self):
        n = self.nodes[0]
        n.worker.cfg["tools"] = {"sleep": {"path": "/bin/sleep"}}
        f = self.tmp / "input.bin"
        f.write_bytes(b"secret media")
        sha = wire.sha256_file(f)
        c = n.client()
        c.put_blob(sha, f)
        c.submit({"task_id": "s", "argv": ["sleep", "30"],
                  "inputs": {"x": {"blob": sha, "kind": "file", "filename": "input.bin"}}})
        self.assertFalse(c.delete_blob(sha))
        c.delete("s")
        self.assertTrue(c.delete_blob(sha))
        self.assertFalse(c.has_blob(sha))


def make_ca_mac(home: Path, node_id: str) -> None:
    pki.create_ca(node_id, home)
    pki.self_issue(node_id, home)


class TLSTests(unittest.TestCase):
    """Pairing with a code, then mutual TLS between the paired Macs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.a, self.b = self.tmp / "pro", self.tmp / "air"
        make_ca_mac(self.a, "aaaa")
        self.cfg_a = dict(config.defaults(), token=TOKEN)
        self.cfg_b = config.defaults()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pair(self, code_for_joiner: str = ""):
        code = pairing.new_code()
        invite = pairing.Invite(self.cfg_a, code, port=0, home=self.a, host="127.0.0.1")
        result = {}
        t = threading.Thread(target=lambda: result.update(name=invite.wait(timeout=10)), daemon=True)
        t.start()
        try:
            pairing.join(self.cfg_b, code_for_joiner or code, f"127.0.0.1:{invite.port}", home=self.b)
        finally:
            if code_for_joiner:
                invite.sock.close()
        t.join(5)
        return result

    def test_pairing_gives_certificate_and_token(self):
        self.assertEqual(self.pair()["name"], self.cfg_b["node_name"])
        self.assertTrue(pki.ready(self.b))
        self.assertEqual(self.cfg_b["token"], TOKEN)

    def test_wrong_code_is_refused(self):
        with self.assertRaisesRegex(pairing.PairingError, "wrong or expired code"):
            self.pair(code_for_joiner=pairing.new_code())
        self.assertFalse(pki.ready(self.b))
        self.assertNotEqual(self.cfg_b["token"], TOKEN)

    def request(self, decide: bool | None, cfg: dict | None = None, home: Path | None = None):
        """Ask to join; the first Mac's side decides once the request shows up. Returns (numbers seen, error)."""
        cfg, home = cfg or self.cfg_b, home or self.b
        server = pairing.PairingServer(self.cfg_a, 0, home=self.a, host="127.0.0.1")
        seen, error = {}, None

        def first_mac():
            for _ in range(100):
                pending = server.pending()
                if pending:
                    seen["first"] = pending[0]["number"]
                    seen["name"] = pending[0]["name"]
                    if decide is not None:
                        server.decide(pending[0]["id"], decide)
                    return
                time.sleep(0.05)

        threading.Thread(target=first_mac, daemon=True).start()
        try:
            pairing.request_join(cfg, [f"127.0.0.1:{server.port}"], home=home,
                                 on_number=lambda n: seen.update(new=n))
        except pairing.PairingError as e:
            error = str(e)
        finally:
            server.close()
        return seen, error

    def test_asking_to_join_and_being_let_in(self):
        seen, error = self.request(True)
        self.assertIsNone(error)
        self.assertRegex(seen["new"], r"^\d{3} \d{3}$")
        self.assertEqual(seen["new"], seen["first"])  # both Macs show the same number
        self.assertEqual(seen["name"], self.cfg_b["node_name"])
        self.assertTrue(pki.ready(self.b))
        self.assertEqual(self.cfg_b["token"], TOKEN)

    def test_a_refused_request_gets_nothing(self):
        seen, error = self.request(False)
        self.assertIn("didn't let this one in", error)
        self.assertFalse(pki.ready(self.b))
        self.assertNotEqual(self.cfg_b["token"], TOKEN)

    def test_a_mesh_takes_two_macs_for_now(self):
        self.assertIsNone(self.request(True)[1])
        third_cfg, third = config.defaults(), self.tmp / "mini"
        seen, error = self.request(True, third_cfg, third)
        self.assertIn("already has 2 Macs", error)
        self.assertNotIn("new", seen)  # refused before any number was shown
        self.assertFalse(pki.ready(third))
        self.assertIsNone(self.request(True)[1])  # the Mac already in may ask again
        self.cfg_a["revoked"] = [pki.own_fingerprint(self.b)]  # removing it makes room
        self.assertIsNone(self.request(True, third_cfg, third)[1])

    def test_a_broken_commitment_is_refused(self):
        server = pairing.PairingServer(self.cfg_a, 0, home=self.a, host="127.0.0.1")
        try:
            s, _ = pairing._connect([f"127.0.0.1:{server.port}"])
            with s:
                pairing._send(s, {"v": 2, "kind": "request", "node_id": "bbbb", "name": "x",
                                  "csr": pki.new_csr("bbbb", self.b), "commit": "0" * 64})
                pairing._read_line(s)
                pairing._send(s, {"nj": "f" * 32})  # doesn't hash to the commitment
                self.assertIn("commitment", pairing._read_line(s)["error"])
            self.assertEqual(server.pending(), [])
        finally:
            server.close()

    def test_code_format(self):
        code = pairing.new_code()
        self.assertRegex(code, r"^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$")
        self.assertEqual(pairing.normalize(code.lower().replace("-", " ")), code.replace("-", ""))
        with self.assertRaises(pairing.PairingError):
            pairing.normalize("1234")

    def test_job_over_mutual_tls_and_rejections(self):
        self.pair()
        tmp = self.tmp
        renderer = tmp / "fakeapp"
        renderer.write_text(FAKE_RENDERER)
        renderer.chmod(renderer.stat().st_mode | stat.S_IEXEC)
        project = tmp / "Ad.ecproj"
        project.mkdir()
        (project / "project.json").write_text("{}")
        tools = {"fakeapp": {"path": str(renderer)}}
        nodes = [Node(tmp, "pro", tools, tls=True), Node(tmp, "air", tools, tls=True)]
        try:
            ctx = pki.context(False, self.a)
            job = jobspec.validate(plan_range(
                "fakeapp", str(project), 0, 199,
                ["render", "{in:project}", "--start", "{start}", "--end", "{end}", "--output", "{out:result}"],
                chunk=50, workdir=str(tmp / "out")))
            result = Scheduler(job, [n.client(tls=ctx) for n in nodes], log=lambda m: None).run()
            self.assertTrue(all(v["tasks"] > 0 for v in result["nodes"].values()), result)
            self.assertEqual(len(os.listdir(tmp / "out/chunks")), 4)

            with self.assertRaises(NodeError):  # plain HTTP to a TLS worker
                nodes[0].client(tls=None)
            rogue = tmp / "rogue"
            make_ca_mac(rogue, "bbbb")
            with self.assertRaises(NodeError):  # a certificate from another mesh's CA
                nodes[0].client(tls=pki.context(False, rogue))
            air_fp = pki.own_fingerprint(self.b)
            nodes[0].worker.cfg["revoked"] = [air_fp]
            with self.assertRaises(NodeError):  # the worker refuses a revoked Mac
                nodes[0].client(tls=pki.context(False, self.b))
            with self.assertRaises(NodeError):  # and a coordinator refuses a revoked worker
                nodes[1].client(tls=ctx, revoked=[air_fp])
        finally:
            for n in nodes:
                n.stop()


FAKE_APP = """#!/bin/sh
# fake app with a project that refers to media: relink <project> <dir> | render <project> <start> <end_excl> <out>
case "$1" in
  info) cat "$2" ;;
  relink) test -f "$3/clip.dat" || { echo "no media in $3"; exit 4; }; echo relinked > "$2.linked" ;;
  render) test -f "$2.linked" || { echo '{"offline": ["clip.dat"]}'; exit 0; }
          if [ -n "$FAKE_BUSY" ]; then end=$(($(date +%s) + FAKE_BUSY)); while [ "$(date +%s)" -lt $end ]; do i=0; while [ $i -lt 20000 ]; do i=$((i+1)); done; done; fi
          mkdir -p "$5"; i=$3; while [ $i -lt $4 ]; do echo "$i" > "$5/frame_$(printf %05d $i).txt"; i=$((i+1)); done ;;
esac
"""


class StepsAndMarkersTests(MeshCase):
    def sh_job(self, steps, fail_if=None, inputs=None):
        for n in self.nodes:
            n.worker.cfg["tools"] = {"sh": {"path": "/bin/sh"}}
        return jobspec.validate({"workdir": str(self.tmp / "o"), "tasks": [
            {"id": "t", "steps": steps, "inputs": inputs or {}, "outputs": {"r": "r.txt"}, "fail_if": fail_if or []}]})

    def test_steps_run_in_order_in_one_folder(self):
        f = self.tmp / "in.txt"
        f.write_text("x")
        job = self.sh_job([["sh", "-c", "ls {indir} > step1.txt"], ["sh", "-c", "cat step1.txt > {out:r}"]],
                          inputs={"f": str(f)})
        self.run_job(job)
        self.assertEqual((self.tmp / "o/r.txt").read_text().split(), ["in.txt"])

    def test_a_failing_step_stops_the_task(self):
        job = self.sh_job([["sh", "-c", "exit 3"], ["sh", "-c", "echo no > {out:r}"]])
        with self.assertRaisesRegex(JobFailed, r"step 1 \(sh\) exit code 3"):
            self.run_job(job, max_attempts=1)

    def test_fail_if_catches_success_that_isnt(self):
        job = self.sh_job([["sh", "-c", 'echo \'{"missing": 2}\'; echo ok > {out:r}']], fail_if=[r'"missing":\s*[1-9]'])
        with self.assertRaisesRegex(JobFailed, "the output says it failed"):
            self.run_job(job, max_attempts=1)
        job = self.sh_job([["sh", "-c", 'echo \'{"missing": 0}\'; echo ok > {out:r}']], fail_if=[r'"missing":\s*[1-9]'])
        self.run_job(job)
        with self.assertRaisesRegex(jobspec.JobError, "regular expression"):
            self.sh_job([["sh", "-c", "true"]], fail_if=["("])


class RangePlaceholderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "p.json"
        self.proj.write_text("{}")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_seconds_land_exactly_on_ntsc_frames(self):
        self.assertEqual(exact_fps(29.97), exact_fps(30000 / 1001))
        self.assertEqual(str(exact_fps(23.976)), "24000/1001")
        self.assertEqual(str(exact_fps(25)), "25")
        job = plan_range("app", str(self.proj), 7, 36, steps=[["{start_s}", "{end_s}", "{end_excl}", "{fps}"]],
                         chunk=30, fps=29.97)
        args = job["tasks"][0]["steps"][0][1:]
        self.assertEqual(args, [repr(7 * 1001 / 30000), repr(37 * 1001 / 30000), "37", repr(30000 / 1001)])

    def test_missing_values_are_explained(self):
        with self.assertRaisesRegex(ValueError, "needs the frame rate"):
            plan_range("app", str(self.proj), 0, 9, steps=[["{start_s}"]])
        with self.assertRaisesRegex(ValueError, "--var comp="):
            plan_range("app", str(self.proj), 0, 9, steps=[["--comp", "{comp}"]])
        job = plan_range("app", str(self.proj), 0, 9, steps=[["--comp", "{comp}", "{indir}"]], vars={"comp": "Main"})
        self.assertEqual(job["tasks"][0]["steps"][0], ["app", "--comp", "Main", "{indir}"])


class MediaRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        (self.tmp / "media").mkdir()
        for name in ("a.mov", "b.wav"):
            (self.tmp / "media" / name).write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_paths_and_file_urls_in_any_json(self):
        a, b = self.tmp / "media/a.mov", self.tmp / "media/b.wav"
        proj = self.tmp / "p.gradeproject"
        # Grade writes file URLs with escaped slashes; FilmCraft plain paths. Missing and system files don't count.
        proj.write_text(json.dumps({"items": [{"url": a.as_uri()}, {"path": str(b)}, {"path": "/Volumes/Gone/c.mov"},
                                              {"lut": "/System/Library/x.cube"}, {"name": "a.mov"}]}).replace("/", "\\/"))
        self.assertEqual(mediarefs.scan(proj), sorted([str(a), str(b)]))
        bundle = self.tmp / "Ad.ecproj"
        (bundle / "Footage").mkdir(parents=True)
        (bundle / "Footage/inside.mov").write_bytes(b"x")
        (bundle / "project.json").write_text(json.dumps({"f": [str(bundle / "Footage/inside.mov"), str(a)]}))
        self.assertEqual(mediarefs.scan(bundle), [str(a)])  # what's inside the bundle travels with it

    def test_two_files_with_one_name_are_refused(self):
        (self.tmp / "other").mkdir()
        (self.tmp / "other/a.mov").write_bytes(b"y")
        with self.assertRaisesRegex(ValueError, "both called 'a.mov'"):
            mediarefs.as_inputs([str(self.tmp / "media/a.mov"), str(self.tmp / "other/a.mov")])


class ProfileTests(MeshCase):
    def write_profile(self, home: Path, prof: dict):
        (home / "apps").mkdir(parents=True, exist_ok=True)
        (home / "apps" / f"{prof['name']}.json").write_text(json.dumps(prof))

    def fake_profile(self, app: Path) -> dict:
        return {"name": "fakeapp2", "description": "test app", "program": {"path": str(app)}, "media": "scan",
                "range": {"steps": [["relink", "{in:project}", "{indir}"],
                                    ["render", "{in:project}", "{start}", "{end_excl}", "{out:result}"]],
                          "out_ext": "", "merge": "dirs"},
                "fail_if": [r'"offline":\s*\[\s*"']}

    def test_shipped_profiles_are_valid(self):
        shipped = {p["name"] for p in profiles.load(self.tmp / "nobody").values()}
        self.assertTrue({"filmcraft", "effectcraft", "grade"} <= shipped, shipped)

    def test_bad_profile_is_rejected(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.validate({"name": "x", "program": {"path": "/x"}, "range": {"steps": "nope"}})
        with self.assertRaises(profiles.ProfileError):
            profiles.validate({"name": "x", "program": {"path": "/x"}, "range": {"steps": [["a"]]}, "fail_if": ["("]})

    def test_app_job_ships_media_and_relinks_on_each_mac(self):
        app = self.tmp / "fakeapp2"
        app.write_text(FAKE_APP)
        app.chmod(app.stat().st_mode | stat.S_IEXEC)
        media_dir = self.tmp / "footage"
        media_dir.mkdir()
        (media_dir / "clip.dat").write_bytes(b"pixels")
        project = self.tmp / "show.json"
        project.write_text(json.dumps({"clips": [{"path": str(media_dir / "clip.dat")}]}))
        prof = self.fake_profile(app)
        for home in [config.home(), *(n.home for n in self.nodes)]:
            self.write_profile(home, prof)  # the coordinator's and each worker's ~/.macmesh/apps
        for n in self.nodes:
            n.worker.fingerprints = toolinfo.fingerprints(n.worker.cfg, n.home)
        cfg = dict(config.defaults(), tools={})
        job = jobspec.validate(plan_app("fakeapp2", str(project), 0, 99, chunk=25, workdir=str(self.tmp / "out"),
                                        cfg=cfg))
        self.assertIn(str((media_dir / "clip.dat").resolve()), job["tasks"][0]["inputs"].values())
        self.run_job(job)
        self.assertEqual(len(os.listdir(self.tmp / "out/frames")), 100)
        # Without the media the app "renders" a placeholder and exits 0: fail_if turns that into a failure.
        prof["media"] = "none"
        prof["range"]["steps"] = prof["range"]["steps"][1:]
        for home in [config.home(), *(n.home for n in self.nodes)]:
            self.write_profile(home, prof)
        job = jobspec.validate(plan_app("fakeapp2", str(project), 0, 24, chunk=25, workdir=str(self.tmp / "out2"),
                                        cfg=cfg))
        with self.assertRaisesRegex(JobFailed, "the output says it failed"):
            self.run_job(job, max_attempts=1)

    def test_user_profile_overrides_shipped_one(self):
        prof = self.fake_profile(Path("/bin/echo"))
        prof["name"] = "grade"
        self.write_profile(self.tmp / "u", prof)
        self.assertEqual(profiles.load(self.tmp / "u")["grade"]["program"]["path"], "/bin/echo")

    def test_v01_gui_entries_are_dropped_from_old_configs(self):
        home = self.tmp / "old"
        home.mkdir()
        os.environ["MACMESH_HOME"] = str(home)
        old = dict(config.defaults(), tools={
            "filmcraft": {"path": "/Applications/FilmCraft.app/Contents/MacOS/filmcraft", "description": "d", "usage": "u"},
            "mine": {"path": "/usr/local/bin/mine"}})
        (home / "config.json").write_text(json.dumps(old))
        self.assertEqual(set(config.load()["tools"]), {"mine"})


class ControlTests(MeshCase):
    """The daemon's control socket and the MCP server on top of it, with in-process Macs."""

    write_profile = ProfileTests.write_profile
    fake_profile = ProfileTests.fake_profile

    def setUp(self):
        super().setUp()
        app = self.tmp / "fakeapp2"
        app.write_text(FAKE_APP)
        app.chmod(app.stat().st_mode | stat.S_IEXEC)
        prof = self.fake_profile(app)
        prof["extensions"] = [".show"]
        prof["frames_probe"] = {"args": ["info", "{project}"], "json": "frames"}
        self.home = self.tmp / "coordinator"
        for home in [self.home, *(n.home for n in self.nodes)]:
            self.write_profile(home, prof)
        for n in self.nodes:
            n.worker.fingerprints = toolinfo.fingerprints(n.worker.cfg, n.home)
        os.environ["MACMESH_HOME"] = str(self.home)
        (self.tmp / "footage").mkdir()
        (self.tmp / "footage/clip.dat").write_bytes(b"pixels")
        self.project = self.tmp / "ad.show"
        self.project.write_text(json.dumps({"frames": 120, "clips": [{"path": str(self.tmp / "footage/clip.dat")}]}))
        cfg = dict(config.defaults(), token=TOKEN, tls=False, tools={}, output_dir=str(self.tmp / "Movies"),
                   peers=[n.address for n in self.nodes])
        config.save(cfg)
        self.ctl = control.Control(cfg, worker=self.nodes[0].worker, home=self.home)
        self.server = control.serve(self.ctl)
        self.client = control.ControlClient(control.socket_path(self.home))

    def tearDown(self):
        self.ctl.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def wait_job(self, call, jid, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = call(jid)
            if job["state"] in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.2)
        self.fail("job didn't finish")

    def test_status_inspect_and_a_whole_project_job(self):
        self.ctl.refresh()
        st = self.client.call("status")
        self.assertEqual(len(st["macs"]), 2)
        info = self.client.call("inspect", path=str(self.project))
        self.assertEqual((info["kind"], info["app"], info["frames"]), ("app", "fakeapp2", 120))
        with self.assertRaisesRegex(control.ControlError, "doesn't know how to split"):
            self.client.call("inspect", path=str(self.tmp / "footage/clip.dat"))
        job = self.client.call("start", kind="app", path=str(self.project))
        job = self.wait_job(lambda j: self.client.call("job", id=j), job["id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["done"], job["tasks"])
        self.assertTrue(all(m["done"] > 0 for m in job["macs"]), job["macs"])
        self.assertEqual(len(job["chunks"]), job["tasks"])
        self.assertTrue(all(c["state"] == "done" and c["mac"] in {"pro", "air"} for c in job["chunks"]), job["chunks"])
        self.assertEqual(Path(job["output"]).name, "ad (macmesh)")
        self.assertEqual(len(os.listdir(job["output"])), 120)
        self.assertEqual(self.client.call("jobs")[0]["id"], job["id"])

    def test_a_running_job_shows_what_each_mac_is_doing(self):
        os.environ["FAKE_BUSY"] = "6"  # each chunk keeps a core busy for 6 s
        seen_running, seen_cpu = False, False
        try:
            job = self.client.call("start", kind="app", path=str(self.project), chunk=60)
            deadline = time.time() + 60
            while time.time() < deadline:
                job = self.client.call("job", id=job["id"])
                for mac in job["macs"]:
                    if any(d["phase"] == "running" and d["label"].startswith("Frames ") for d in mac["doing"]):
                        seen_running = True
                    if any(point["cpu"] > 5 and point["cores"] for point in mac["load"]):
                        seen_cpu = True
                if job["state"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.3)
        finally:
            os.environ.pop("FAKE_BUSY")
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(seen_running, "no Mac ever showed a chunk running")
        self.assertTrue(seen_cpu, "no CPU used by the job was ever sampled")
        self.assertEqual([c["label"] for c in job["chunks"]], ["Frames 0\u201359", "Frames 60\u2013119"])
        self.assertTrue(all(c["phase"] == "done" and c["seconds"] >= 4.5 for c in job["chunks"]), job["chunks"])
        self.assertGreater(sum(m["sent"] for m in job["macs"]), 0)  # the project and its media went out
        self.assertTrue(any("on pro in" in e["text"] for e in job["events"]), job["events"])
        self.assertTrue(all(e["at"] > job["created"] - 1 for e in job["events"]))

    def test_command_line_jobs_run_in_the_engine(self):
        from contextlib import redirect_stdout
        from macmesh import cli
        self.project = self.tmp / "Ad.ecproj"  # the fake renderer's project folder (MeshCase's)
        jobspec.save(self.range_job(frames=100, out="cli-out"), self.tmp / "job.json")
        printed = io.StringIO()
        with redirect_stdout(printed):
            rc = cli.main(["run", str(self.tmp / "job.json"), "--yes"])
        self.assertEqual(rc, 0, printed.getvalue())
        self.assertEqual(len(os.listdir(self.tmp / "cli-out/frames")), 100)
        job = self.client.call("jobs")[0]  # the app sees it like any other job
        self.assertEqual((job["state"], job["kind"], job["done"]), ("done", "job", 2))
        self.assertIn("running in macmesh's engine", printed.getvalue())
        self.assertIn("Frames 0\u201349", printed.getvalue())  # its activity, as the app shows it

    def test_command_line_says_to_open_macmesh_when_it_isnt_running(self):
        from contextlib import redirect_stdout
        from macmesh import cli
        self.project = self.tmp / "Ad.ecproj"
        jobspec.save(self.range_job(frames=10, out="cli-out"), self.tmp / "job.json")
        self.server.shutdown()
        self.server.server_close()
        control.socket_path(self.home).unlink(missing_ok=True)
        printed = io.StringIO()
        with redirect_stdout(printed):
            rc = cli.main(["run", str(self.tmp / "job.json"), "--yes"])
        self.server = control.serve(self.ctl)  # for tearDown
        self.assertEqual(rc, 1)
        self.assertIn("Open the macmesh app", printed.getvalue())
        self.assertFalse((self.tmp / "cli-out/frames").exists())  # no hidden job ran

    def test_cancel_and_lending(self):
        os.environ["FAKE_DELAY"] = "1"
        try:
            job = self.client.call("start", kind="app", path=str(self.project), chunk=5)
            self.client.call("cancel", id=job["id"])
            job = self.wait_job(lambda j: self.client.call("job", id=j), job["id"])
        finally:
            os.environ.pop("FAKE_DELAY")
        self.assertEqual(job["state"], "cancelled")
        st = self.client.call("lend", on=False)
        self.assertEqual((st["lend"], st["accepting"], st["reason"]), (False, False, "not lending"))
        self.assertFalse(config.load()["lend"])
        self.assertTrue(self.client.call("lend", on=True)["accepting"])
        self.assertEqual(oct(control.socket_path(self.home).stat().st_mode & 0o777), "0o600")

    def test_mcp_tools(self):
        server = mcp_module.Server(self.client)
        init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "macmesh")
        names = {t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertTrue({"get_status", "inspect_file", "start_job", "get_job", "cancel_job"} <= names)

        def call(name, **args):
            r = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}})
            return r["result"]

        started = call("start_job", path=str(self.project), end=49)
        self.assertFalse(started["isError"], started)
        jid = json.loads(started["content"][0]["text"])["id"]
        job = self.wait_job(lambda j: json.loads(call("get_job", id=j)["content"][0]["text"]), jid)
        self.assertEqual((job["state"], len(os.listdir(job["output"]))), ("done", 50))
        bad = call("inspect_file", path="/nope/x.show")
        self.assertTrue(bad["isError"])
        self.assertIn("no file", bad["content"][0]["text"])
        self.assertIn("error", server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                              "params": {"name": "rm_rf"}}))
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        # 0.3.4: batches, the owner's limit, and answering requests (only with the user's yes)
        tools = {t["name"]: t for t in server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})["result"]["tools"]}
        self.assertTrue({"files", "template", "out_ext"} <= set(tools["start_job"]["inputSchema"]["properties"]))
        self.assertTrue({"answer_join_request", "answer_install_request"} <= set(tools))
        self.assertIn("give path", call("start_job")["content"][0]["text"])
        st = json.loads(call("set_lending", limit=0.8, mode="shared")["content"][0]["text"])
        self.assertEqual((st["limit"], st["owner_mode"]), (0.8, "shared"))
        self.assertEqual(config.load()["owner"]["limit"], 0.8)
        self.assertTrue(call("set_lending", limit=0.99)["isError"])
        gone = call("answer_install_request", program="demucs-stem", allow=True)
        self.assertTrue(gone["isError"])
        self.assertIn("gone", gone["content"][0]["text"])


FAKE_AGENT = """#!/usr/bin/env python3
# Answers like `claude -p --output-format json`: {"result": "<reply>"}. Logs each prompt; replies
# from $FAKE_AGENT_DIR: plan.json, explain.json, profile-1.json, profile-2.json (one per attempt).
import json, os, sys
d = os.environ["FAKE_AGENT_DIR"]
prompt = sys.argv[sys.argv.index("-p") + 1]
n = len([f for f in os.listdir(d) if f.startswith("prompt-")]) + 1
open(os.path.join(d, f"prompt-{n}.txt"), "w").write(prompt)
if "planner for macmesh" in prompt:
    reply = open(os.path.join(d, "plan.json")).read()
elif "job failed" in prompt:
    reply = open(os.path.join(d, "explain.json")).read()
else:
    attempt = 2 if "IT FAILED THE TEST RENDER" in prompt else 1
    reply = open(os.path.join(d, f"profile-{attempt}.json")).read()
print(json.dumps({"type": "result", "result": "Here you go:\\n" + reply}))
"""


class AgentLookupTests(unittest.TestCase):
    def test_old_antigravity_command_is_replaced_on_load(self):
        home = Path(tempfile.mkdtemp())
        old_home = os.environ.get("MACMESH_HOME")
        os.environ["MACMESH_HOME"] = str(home)
        try:
            cfg = config.defaults()
            cfg["agents"]["antigravity"] = config.OLD_AGENTS["antigravity"][0]
            cfg["agents"]["claude"] = ["claude", "-p", "{prompt}", "--my-own-flag"]  # the user's own: kept
            config.save(cfg)
            loaded = config.load()
            self.assertEqual(loaded["agents"]["antigravity"], config.DEFAULT_AGENTS["antigravity"])
            self.assertNotIn("plan", loaded["agents"]["antigravity"])
            self.assertEqual(loaded["agents"]["claude"][-1], "--my-own-flag")
        finally:
            if old_home is None:
                os.environ.pop("MACMESH_HOME", None)
            else:
                os.environ["MACMESH_HOME"] = old_home
            shutil.rmtree(home, ignore_errors=True)

    def test_finds_antigravity_where_its_installer_puts_it(self):
        home = Path(tempfile.mkdtemp())
        agy = home / ".gemini/bin/agy"
        agy.parent.mkdir(parents=True)
        agy.write_text("#!/bin/sh\n")
        agy.chmod(0o755)
        old = dict(os.environ)
        os.environ.update(HOME=str(home), PATH="/usr/bin:/bin")  # the app's engine gets a bare PATH
        saved, ai._login_path = ai._login_path, []
        try:
            cfg = config.defaults()
            self.assertEqual(ai.available(cfg).get("antigravity"), str(agy))
            self.assertTrue(ai._agent_env(str(agy))["PATH"].startswith(str(agy.parent) + ":"))
        finally:
            os.environ.clear()
            os.environ.update(old)
            ai._login_path = saved
            shutil.rmtree(home, ignore_errors=True)


class AITests(ControlTests):
    """Planning, teaching and explaining with a fake agent standing in for Claude Code."""

    def setUp(self):
        super().setUp()
        self.agent_dir = self.tmp / "agent"
        self.agent_dir.mkdir()
        fake = self.tmp / "fake-claude"
        fake.write_text(FAKE_AGENT)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ["FAKE_AGENT_DIR"] = str(self.agent_dir)
        self.ctl.cfg["agents"] = {"claude": [str(fake), "-p", "{prompt}"], "antigravity": ["/nonexistent/agy", "-p", "{prompt}"]}
        self.ctl.cfg["agent"] = "auto"

    def tearDown(self):
        os.environ.pop("FAKE_AGENT_DIR", None)
        super().tearDown()

    def reply(self, name, obj):
        (self.agent_dir / name).write_text(json.dumps(obj))

    def wait_task(self, tid, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = self.client.call("ai_task", id=tid)
            if t["state"] != "working":
                return t
            time.sleep(0.2)
        self.fail("AI task didn't finish")

    test_status_inspect_and_a_whole_project_job = None  # covered by ControlTests
    test_cancel_and_lending = None
    test_mcp_tools = None

    def test_agent_is_picked_by_whats_installed(self):
        info = self.client.call("ai")
        self.assertEqual((info["choice"], info["using"], info["available"]), ("auto", "claude", ["claude"]))
        with self.assertRaisesRegex(ai.AgentError, "isn't installed"):
            ai.pick(self.ctl.cfg, "antigravity")

    def test_safe_plan_runs_at_once_and_sees_the_project(self):
        self.reply("plan.json", {"planner": "app", "explanation": "Render it in chunks of 10.",
                                 "params": {"app": "fakeapp2", "project": str(self.project), "start": 0, "end": 59, "chunk": 10}})
        task = self.wait_task(self.client.call("plan", request="render the first 60 frames", paths=[str(self.project)])["id"])
        self.assertEqual(task["state"], "done", task)
        r = task["result"]
        self.assertTrue(r["safe"])
        self.assertEqual((r["tasks"], r["programs"]), (6, ["fakeapp2"]))
        job = self.wait_job(lambda j: self.client.call("job", id=j), r["job_id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(len(os.listdir(job["output"])), 60)
        prompt = (self.agent_dir / "prompt-1.txt").read_text()
        self.assertIn('\\"frames\\": 120', prompt)  # the project's contents went along
        self.assertNotIn("pixels", prompt)  # the media didn't

    def test_unsafe_plan_waits_for_ok(self):
        self.reply("plan.json", {"planner": "job", "explanation": "Custom.", "params": {
            "name": "custom", "tasks": [{"id": "t1", "steps": [["fakeapp2", "relink", "{in:p}", "{indir}"],
                                                           ["fakeapp2", "render", "{in:p}", "0", "5", "{out:r}"]],
                                         "inputs": {"p": str(self.project), "m": str(self.tmp / "footage/clip.dat")},
                                         "outputs": {"r": "frames"}}],
            "merge": [{"op": "command", "argv": ["true"]}]}})
        task = self.wait_task(self.client.call("plan", request="do it my way", paths=[str(self.project)])["id"])
        self.assertEqual(task["state"], "done", task)
        self.assertFalse(task["result"]["safe"])
        self.assertIn("runs a command on this Mac", task["result"]["reasons"][0])
        self.assertTrue(task["awaiting_ok"])
        self.assertIsNone(task["result"]["job_id"])
        job = self.wait_job(lambda j: self.client.call("job", id=j), self.client.call("run_plan", id=task["id"])["id"])
        self.assertEqual(job["state"], "done", job)

    def test_plan_cant_reach_files_you_didnt_name(self):
        secret = self.tmp / "secret.txt"
        secret.write_text("private")
        self.reply("plan.json", {"planner": "batch", "params": {"files": [str(secret)], "template": ["fakeapp2", "{in:file}"],
                                                                "out_ext": ".txt"}})
        task = self.wait_task(self.client.call("plan", request="process it", paths=[str(self.project)])["id"])
        self.assertEqual(task["state"], "failed")
        self.assertIn("not one of the files you gave", task["error"])

    def test_explaining_a_failed_job(self):
        self.project.write_text(json.dumps({"frames": 20}))  # no media: the fake app "renders" placeholders
        prof = self.fake_profile(self.tmp / "fakeapp2")
        prof["range"]["steps"] = prof["range"]["steps"][1:]
        prof["media"] = "none"
        for home in [self.home, *(n.home for n in self.nodes)]:
            self.write_profile(home, dict(prof, extensions=[".show"], frames_probe={"args": ["info", "{project}"], "json": "frames"}))
        job = self.wait_job(lambda j: self.client.call("job", id=j),
                            self.client.call("start", kind="app", path=str(self.project))["id"])
        self.assertEqual(job["state"], "failed", job)
        self.reply("explain.json", {"summary": "The app couldn't find clip.dat.", "fix": "Keep the media with the project.",
                                    "where": "the project"})
        task = self.wait_task(self.client.call("explain", id=job["id"])["id"])
        self.assertEqual(task["result"]["where"], "the project", task)
        self.assertIn("offline", (self.agent_dir / "prompt-1.txt").read_text())  # it saw the failure

    def test_teaching_an_app_keeps_only_a_profile_that_passes(self):
        self.ctl.cfg["node_id"] = self.nodes[0].worker.cfg["node_id"]
        home = self.nodes[0].home
        self.ctl.home = home
        prof = self.fake_profile(self.tmp / "fakeapp2")
        prof.update(name="taught", extensions=[".show"], frames_probe={"args": ["info", "{project}"], "json": "frames"})
        wrong = json.loads(json.dumps(prof))
        wrong["range"]["steps"][1][3] = "{end}"  # off by one: the last frame of every chunk goes missing
        self.reply("profile-1.json", {"profile": wrong, "explanation": "first try"})
        self.reply("profile-2.json", {"profile": prof, "explanation": "Relinks, then renders frames."})
        task = self.wait_task(self.client.call("teach", program=str(self.tmp / "fakeapp2"), name="taught",
                                               sample=str(self.project))["id"], timeout=120)
        self.assertEqual(task["state"], "done", task)
        self.assertEqual(task["result"]["attempts"], 2)
        self.assertIn("render the same whole and in 3 chunks", task["result"]["verified"])
        self.assertTrue((home / "apps/taught.json").exists())
        retry = (self.agent_dir / "prompt-2.txt").read_text()
        self.assertIn("gave 11 frames, not 12", retry)  # the agent was told what failed
        self.assertIn('"name": "filmcraft"', retry)  # and saw the shipped profiles as examples


@unittest.skipUnless(sandbox.available(), "sandbox-exec not available")
class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        tools = {"sh": {"path": "/bin/sh"}, "curl": {"path": "/usr/bin/curl"}}
        self.node = Node(self.tmp, "air", tools)

    def tearDown(self):
        self.node.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_task(self, argv, inputs=None):
        job = jobspec.validate({"workdir": str(self.tmp / "out"), "tasks": [
            {"id": "t", "argv": argv, "inputs": inputs or {}, "outputs": {"result": "r.txt"}}]})
        Scheduler(job, [self.node.client()], log=lambda m: None, max_attempts=1).run()
        return (self.tmp / "out/r.txt").read_text()

    def test_task_works_inside_its_folder(self):
        f = self.tmp / "in.txt"
        f.write_text("frames")
        self.assertEqual(self.run_task(["sh", "-c", "cat {in:f} > {out:result}"], {"f": str(f)}), "frames")

    def test_task_cannot_read_owner_files(self):
        secret = Path.home() / "Library/Preferences/.GlobalPreferences.plist"
        self.assertTrue(secret.exists())
        with self.assertRaisesRegex(JobFailed, "Operation not permitted"):
            self.run_task(["sh", "-c", f"cat '{secret}' > {{out:result}}"])

    def test_task_cannot_read_owners_temp_files(self):
        private = sandbox.user_private_dir()
        self.assertIsNotNone(private)
        secret = Path(tempfile.mkstemp(dir=private / "T")[1])
        try:
            secret.write_text("someone else's temp file")
            with self.assertRaisesRegex(JobFailed, "Operation not permitted"):
                self.run_task(["sh", "-c", f"cat '{secret}' > {{out:result}}"])
        finally:
            secret.unlink()

    def test_task_cannot_write_outside_or_use_network(self):
        escape = Path("/tmp") / f"macmesh-escape-{os.getpid()}"
        with self.assertRaisesRegex(JobFailed, "Operation not permitted"):
            self.run_task(["sh", "-c", f"echo x > {escape}; echo x > {{out:result}}; test -f {escape}"])
        self.assertFalse(escape.exists())
        with self.assertRaises(JobFailed):
            self.run_task(["sh", "-c", f"curl -sf -m 3 http://{self.node.address}/ > {{out:result}}"])


if __name__ == "__main__":
    unittest.main()
