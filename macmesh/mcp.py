"""`macmesh mcp`: an MCP server (JSON-RPC 2.0 over stdio) so AI agents can lend and borrow Macs.

Every tool goes through the daemon's control socket (macmesh/control.py), so
jobs an agent starts show up in the menu bar app, and the other way round.
stdout carries only JSON-RPC lines; anything else goes to stderr.
"""

from __future__ import annotations

import json
import sys

from . import __version__
from .control import ControlClient, ControlError

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = (
    "macmesh splits heavy renders, encodes and per-file work (such as stem separation) across the Macs on "
    "this network (two Macs per mesh for now). Start with get_status to see the Macs, then inspect_file on "
    "the user's project or video, then start_job and poll get_job until it is done; get_job also says what "
    "each Mac is doing, its CPU and GPU load, and what has happened. For many files, pass files (and "
    "template/out_ext) instead of path. Frames are 0-based and end is inclusive. Output lands in the user's "
    "macmesh folder (get_job's output). Ask before cancelling a job you didn't start, turning lending off "
    "or changing how much of the Mac it may use. get_status lists Macs asking to join and programs other "
    "Macs want installed here: never answer those without the user's explicit yes, and for a join, only "
    "after they confirm the 6-digit numbers match on both Macs."
)


def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
TOOLS = [
    {"name": "get_status", "description": "This Mac (lending, whether it takes work), the Macs on the mesh "
     "and their state, and recent jobs.", "inputSchema": _obj({}), "annotations": {"readOnlyHint": True}},
    {"name": "list_apps", "description": "Apps macmesh can split work for (from app profiles), the project "
     "file types each opens, and whether this Mac has it.", "inputSchema": _obj({}),
     "annotations": {"readOnlyHint": True}},
    {"name": "inspect_file", "description": "What a project or video file is: which app renders it, how many "
     "frames, and any values the job needs (needs_vars, e.g. comp).",
     "inputSchema": _obj({"path": {**S, "description": "Absolute path"}}, ["path"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "start_job", "description": "Render or encode a file across the Macs. Projects of a known app "
     "render (all frames unless start/end are given); videos are re-encoded (preset). With files instead of "
     "path, each file is one task (a batch: by default demucs-stem, splitting songs into stems). Returns the "
     "job; poll get_job for progress.",
     "inputSchema": _obj({
         "path": {**S, "description": "Absolute path of the project or video"},
         "files": {"type": "array", "items": S, "description": "Absolute paths, one task each (a batch job)"},
         "template": {"type": "array", "items": S, "description": "Batch command, e.g. "
                      "[\"demucs-stem\", \"{in:file}\", \"{out:result}\"] (the default)"},
         "out_ext": {**S, "description": "Batch result extension (default .zip)"},
         "start": {"type": "integer", "description": "First frame (default 0)"},
         "end": {"type": "integer", "description": "Last frame, inclusive (default the last)"},
         "vars": {"type": "object", "description": "Values the app needs, e.g. {\"comp\": \"Main\"}",
                  "additionalProperties": S},
         "macs": {"type": "array", "items": S, "description": "Node ids to use (default all available)"},
         "preset": {**S, "description": "Video presets: h264, hevc, prores, h264-hw, hevc-hw (default hevc)"},
         "title": {**S, "description": "Name to show for the job"},
     })},
    {"name": "get_job", "description": "A job's state (running, merging, done, failed, cancelled), progress, "
     "time left and output file; per Mac what it is doing now, its CPU (this job's) and GPU load over the "
     "last 2 minutes, how fast it's allowed to run for its owner, and bytes sent; every chunk; and events.",
     "inputSchema": _obj({"id": S}, ["id"]), "annotations": {"readOnlyHint": True}},
    {"name": "list_jobs", "description": "Jobs this Mac has run, newest first.", "inputSchema": _obj({}),
     "annotations": {"readOnlyHint": True}},
    {"name": "cancel_job", "description": "Stop a job and the work on every Mac.",
     "inputSchema": _obj({"id": S}, ["id"]), "annotations": {"destructiveHint": True}},
    {"name": "set_lending", "description": "Let the other Macs use this one, or stop; how (shared: slow down "
     "to make room for the owner, idle: only while they're away); and the most of the Mac, CPU and GPU, "
     "that the owner's work and macmesh's together may use (0.5-0.95, default 0.9). Remembered.",
     "inputSchema": _obj({"on": {"type": "boolean"}, "mode": {"type": "string", "enum": ["shared", "idle"]},
                          "limit": {"type": "number", "minimum": 0.5, "maximum": 0.95}})},
    {"name": "create_invite", "description": "A one-time code for adding another Mac, for when it can't find "
     "this one nearby (only on the Mac that started the mesh; a mesh holds two Macs for now). Show the code "
     "to the user; they run `macmesh join CODE` on the new Mac.",
     "inputSchema": _obj({})},
    {"name": "answer_join_request", "description": "Let a Mac that asked to join in, or not (get_status: "
     "invite.requests). Only with the user's yes after they checked both Macs show the same number.",
     "inputSchema": _obj({"id": S, "allow": {"type": "boolean"}}, ["id", "allow"]),
     "annotations": {"destructiveHint": True}},
    {"name": "answer_install_request", "description": "Let another Mac's job install a program on this Mac "
     "(get_status: installs, state asking), or not. Only with the user's explicit yes.",
     "inputSchema": _obj({"program": S, "allow": {"type": "boolean"}}, ["program", "allow"]),
     "annotations": {"destructiveHint": True}},
]


class Server:
    def __init__(self, client: ControlClient | None = None):
        self.client = client or ControlClient()

    def tool(self, name: str, args: dict):
        c = self.client
        if name == "get_status":
            return c.call("status")
        if name == "list_apps":
            return c.call("apps")
        if name == "inspect_file":
            return c.call("inspect", path=args["path"])
        if name == "start_job":
            if not args.get("path") and not args.get("files"):
                raise ControlError("give path (one project or video) or files (a batch)")
            if args.get("files"):
                spec = {k: v for k, v in args.items() if v is not None}
                spec.setdefault("kind", "batch")
                return c.call("start", **spec)
            kind = c.call("inspect", path=args["path"])["kind"]
            spec = {"kind": kind, **{k: v for k, v in args.items() if v is not None}}
            if kind != "video":
                spec.pop("preset", None)
            return c.call("start", **spec)
        if name == "get_job":
            return c.call("job", id=args["id"])
        if name == "list_jobs":
            return c.call("jobs")
        if name == "cancel_job":
            return c.call("cancel", id=args["id"])
        if name == "set_lending":
            status = None
            if "mode" in args or "limit" in args:
                status = c.call("owner", mode=args.get("mode"), limit=args.get("limit"))
            if "on" in args:
                status = c.call("lend", on=bool(args["on"]))
            return status or c.call("status")
        if name == "create_invite":
            return c.call("invite")
        if name == "answer_join_request":
            return c.call("approve", id=args["id"], allow=bool(args["allow"]))
        if name == "answer_install_request":
            return c.call("install", program=args["program"], allow=bool(args["allow"]))
        raise ControlError(f"unknown tool {name}")

    def handle(self, msg: dict) -> dict | None:
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:
            return None  # notifications get no reply
        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion")
            return _result(rid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "macmesh", "version": __version__},
                                 "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": TOOLS})
        if method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in {t["name"] for t in TOOLS}:
                return _error(rid, -32602, f"Unknown tool: {name}")
            try:
                text, failed = json.dumps(self.tool(name, args), indent=1), False
            except (ControlError, KeyError) as e:
                text, failed = (f"missing argument {e}" if isinstance(e, KeyError) else str(e)), True
            return _result(rid, {"content": [{"type": "text", "text": text}], "isError": failed})
        return _error(rid, -32601, f"Method not found: {method}")


def _result(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def main() -> int:
    server = Server()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "Parse error: not valid JSON.")
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "Expected an object.")
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0
