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

from macmesh import config, control, jobspec, media, mediarefs, owner, pairing, pki, profiles, sandbox, toolinfo, wire
from macmesh import mcp as mcp_module
from macmesh import worker as worker_module
from macmesh.client import NodeClient, NodeError
from macmesh.planners import plan_app, plan_batch, plan_range, plan_video
from macmesh.planners.range import exact_fps
from macmesh.planners.ai import extract_plan
from macmesh.scheduler import JobFailed, Scheduler
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
            os.environ["FAKE_DELAY"] = "0.3"
            lines = []
            threading.Timer(1.0, lambda: busy.update(cpu=0.9)).start()
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
        policy = dict(config.defaults()["owner"])
        sample = {"cpu": 0.1, "gpu": 0.2, "idle": 300.0}
        w = owner.OwnerWatch(policy, sample=lambda ours: sample)
        self.assertEqual(w.busy(set(), paused=False), "")
        sample["cpu"] = 0.8
        self.assertIn("CPU", w.busy(set(), paused=False))
        # GPU only counts while someone is at the keyboard (or our tasks are paused)...
        sample.update(cpu=0.1, gpu=0.9, idle=300.0)
        self.assertEqual(w.busy(set(), paused=False), "")
        sample["idle"] = 2.0
        self.assertIn("GPU", w.busy(set(), paused=False))
        # ...and if it goes quiet the moment we pause, it was ours: ignore the GPU for a while.
        sample["gpu"] = 0.05
        self.assertEqual(w.busy(set(), paused=True), "")
        sample["gpu"] = 0.9
        self.assertEqual(w.busy(set(), paused=False), "")
        idle = owner.OwnerWatch(dict(policy, mode="idle"), sample=lambda ours: {"cpu": 0, "gpu": 0, "idle": 5.0})
        self.assertIn("active", idle.busy(set(), paused=False))

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
        with self.assertRaisesRegex(pairing.PairingError, "wrong code"):
            self.pair(code_for_joiner=pairing.new_code())
        self.assertFalse(pki.ready(self.b))
        self.assertNotEqual(self.cfg_b["token"], TOKEN)

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
