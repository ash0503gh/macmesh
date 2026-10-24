"""Run with:  python3 -m unittest discover -s tests -v   (needs ffmpeg on PATH)"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from macmesh import config, jobspec, media, wire
from macmesh.client import NodeClient, NodeError
from macmesh.planners import plan_batch, plan_range, plan_video
from macmesh.planners.ai import extract_plan
from macmesh.scheduler import JobFailed, Scheduler
from macmesh.worker import Handler, Worker

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
    """An in-process worker on an ephemeral port, standing in for one Mac."""

    def __init__(self, root: Path, name: str, tools: dict, slots: int = 1):
        os.environ["MACMESH_HOME"] = str(root / name)
        cfg = config.defaults()
        cfg.update(node_name=name, token=TOKEN, slots=slots, port=0, tools=tools)
        self.worker = Worker(cfg)
        handler = type("H", (Handler,), {"worker": self.worker})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.address = f"127.0.0.1:{self.server.server_address[1]}"

    def client(self) -> NodeClient:
        c = NodeClient(self.address, TOKEN, timeout=5)
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
        spans = [(int(t["argv"][2]), int(t["argv"][3])) for t in job["tasks"]]
        self.assertEqual(spans, [(10, 109), (110, 209), (210, 254)])

    def test_extract_plan_from_claude_wrapper_and_prose(self):
        inner = 'Sure!\n```json\n{"planner": "batch", "params": {"files": []}}\n```'
        self.assertEqual(extract_plan(json.dumps({"result": inner}))["planner"], "batch")
        self.assertEqual(extract_plan('text {"planner": "video", "params": {}} more')["planner"], "video")
        with self.assertRaises(ValueError):
            extract_plan("no json here")


class MeshTests(unittest.TestCase):
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

    def range_job(self, frames=400, chunk=50):
        return jobspec.validate(plan_range(
            "fakeapp", str(self.project), 0, frames - 1,
            ["render", "{in:project}", "--start", "{start}", "--end", "{end}", "--output", "{out:result}"],
            chunk=chunk, workdir=str(self.tmp / "out")))

    def run_job(self, job, **kw):
        sched = Scheduler(job, [n.client() for n in self.nodes], log=lambda m: None, **kw)
        result = sched.run()
        media.run_merge(Path(job["workdir"]), job["merge"], log=lambda m: None)
        return result

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
        self.run_job(self.range_job())
        for n in self.nodes:
            blobs = [b for b in os.listdir(n.worker.blobs) if not b.endswith(".d")]
            self.assertEqual(len(blobs), 1)

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


if __name__ == "__main__":
    unittest.main()
