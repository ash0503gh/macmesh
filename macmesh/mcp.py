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
    "macmesh splits heavy renders and encodes across the Macs on this network. "
    "Start with get_status to see the Macs, then inspect_file on the user's project or video, "
    "then start_job and poll get_job until it is done. Frames are 0-based and end is inclusive. "
    "Output lands in the user's macmesh folder (get_job's output). Ask before cancelling a job "
    "you didn't start, or turning lending off."
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
     "render (all frames unless start/end are given); videos are re-encoded (preset). Returns the job; "
     "poll get_job for progress.",
     "inputSchema": _obj({
         "path": {**S, "description": "Absolute path of the project or video"},
         "start": {"type": "integer", "description": "First frame (default 0)"},
         "end": {"type": "integer", "description": "Last frame, inclusive (default the last)"},
         "vars": {"type": "object", "description": "Values the app needs, e.g. {\"comp\": \"Main\"}",
                  "additionalProperties": S},
         "macs": {"type": "array", "items": S, "description": "Node ids to use (default all available)"},
         "preset": {**S, "description": "Video presets: h264, hevc, prores, h264-hw, hevc-hw (default hevc)"},
         "title": {**S, "description": "Name to show for the job"},
     }, ["path"])},
    {"name": "get_job", "description": "A job's state (running, merging, done, failed, cancelled), progress, "
     "time left, chunks per Mac and output file.",
     "inputSchema": _obj({"id": S}, ["id"]), "annotations": {"readOnlyHint": True}},
    {"name": "list_jobs", "description": "Jobs this Mac has run, newest first.", "inputSchema": _obj({}),
     "annotations": {"readOnlyHint": True}},
    {"name": "cancel_job", "description": "Stop a job and the work on every Mac.",
     "inputSchema": _obj({"id": S}, ["id"]), "annotations": {"destructiveHint": True}},
    {"name": "set_lending", "description": "Let the other Macs use this one, or stop (remembered).",
     "inputSchema": _obj({"on": {"type": "boolean"}}, ["on"])},
    {"name": "create_invite", "description": "A one-time code for adding another Mac (only on the Mac that "
     "started the mesh). Show the code to the user; they run `macmesh join CODE` on the new Mac.",
     "inputSchema": _obj({})},
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
            return c.call("lend", on=bool(args["on"]))
        if name == "create_invite":
            return c.call("invite")
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
