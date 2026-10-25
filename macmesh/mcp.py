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
    "the user's project or video (or list_folder for a folder of files), then preview_job, show the user what "
    "it will do and ask; only after their yes call start_job with confirmed: true, and poll get_job until it "
    "is done; get_job also says what each Mac is doing, its CPU and GPU load, and what has happened. For "
    "many files, pass a folder (with extensions) or files, and template/out_ext, instead of path. Frames are 0-based and end is inclusive. Output lands in the user's "
    "macmesh folder (get_job's output). Ask before cancelling a job you didn't start, turning lending off "
    "or changing how much of the Mac it may use. get_status lists Macs asking to join and programs other "
    "Macs want installed here: never answer those without the user's explicit yes, and for a join, only "
    "after they confirm the 6-digit numbers match on both Macs."
)


def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
JOB_ARGS = {
    "path": {**S, "description": "Absolute path of the project or video (one file)"},
    "files": {"type": "array", "items": S, "description": "Absolute paths, one task each (a batch job)"},
    "folder": {**S, "description": "A folder whose files are the batch (instead of listing files)"},
    "extensions": {"type": "array", "items": S, "description": "With folder: only these types, e.g. [\".mp3\"]"},
    "recursive": {"type": "boolean", "description": "With folder: include subfolders"},
    "template": {"type": "array", "items": S, "description": "Batch command, e.g. "
                 "[\"demucs-stem\", \"{in:file}\", \"{out:result}\"] (the default)"},
    "out_ext": {**S, "description": "Batch result extension (default .zip)"},
    "name": {**S, "description": "The job's name (a batch's results folder is named after it)"},
    "output": {**S, "description": "Where results go: a new folder (or file) path"},
    "start": {"type": "integer", "description": "First frame (default 0)"},
    "end": {"type": "integer", "description": "Last frame, inclusive (default the last)"},
    "vars": {"type": "object", "description": "Values the app needs, e.g. {\"comp\": \"Main\"}", "additionalProperties": S},
    "macs": {"type": "array", "items": S, "description": "Node ids to use (default all available)"},
    "slots": {"type": "integer", "minimum": 1, "description": "Tasks this Mac runs at once (remembered)"},
    "preset": {**S, "description": "Video presets: h264, hevc, prores, h264-hw, hevc-hw (default hevc)"},
    "title": {**S, "description": "Name to show for the job"},
}

# /macmesh in Claude Code and Antigravity: the calling agent runs a short intake with the user, then
# previews and only starts on their yes. The agent itself is the conversation; macmesh's own AI isn't used.
PROMPT = """You're helping the user run a job with macmesh, which splits heavy work across the Macs on their network. Use the macmesh tools.

1. Call get_status (the Macs, what each has, how many tasks each runs at once) and list_apps.
2. Work out the job. Ask ONLY what you can't infer: at most 3 questions, one at a time, using your question picker if you have one, each with sensible default options:
   a. What to do: re-encode a video, render an app's project, or a batch with one of the programs the Macs have (one task per file).
   b. Which files: a project, a video, or a folder. If they describe a folder loosely, ask for it; then call list_folder to see what's in it and confirm the count ("612 MP3s").
   c. Where results go: default a new folder named after the job in their macmesh folder; ask only if they mentioned a place.
   Don't ask about Macs or how many at once unless they brought it up: default to all available Macs and current settings. If they want more in parallel, suggest a number for this Mac (2-4 helps tools that leave the GPU partly idle; never more than its cores) as slots.
3. Call preview_job with exactly what you'd start. Show a short summary: what runs on how many files or frames, which Macs (and any that would be asked to install a program), how many at once on this Mac, where results go. Ask: "Start it?"
4. Only after a clear yes, call start_job with the same arguments and confirmed: true. If they change something, preview again.
5. Then check get_job a few times (about every 30 s) and report progress briefly: done of total, time left, what each Mac is doing. Tell them it's also live on the Jobs page of the macmesh app.

Never, without the user's explicit yes in this conversation: start or cancel a job, change lending or the limit, answer a Mac's join request (only after they compared the numbers on both Macs) or an install request.

What they asked for: {request}"""
PROMPTS = [{"name": "macmesh", "title": "macmesh: run a job across your Macs",
            "description": "Split heavy work (renders, encodes, a batch of files) across your Macs: asks a few "
                           "questions, shows the plan, starts only when you say yes.",
            "arguments": [{"name": "request", "description": "What to run, e.g. \"split the 600 songs into stems\"",
                           "required": False}]}]

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
    {"name": "preview_job", "description": "What start_job would do with these arguments, WITHOUT starting "
     "anything: tasks, programs, the first task, which Macs (and any missing a program, which would be asked "
     "to install it), how many tasks run at once on this Mac, and where results go. Show this to the user and "
     "ask before calling start_job.",
     "inputSchema": _obj(JOB_ARGS), "annotations": {"readOnlyHint": True}},
    {"name": "start_job", "description": "Start a job across the Macs: a project of a known app renders (all "
     "frames unless start/end are given), a video is re-encoded (preset), or with files/folder each file is "
     "one task (a batch). Only call it after showing preview_job's result and the user saying yes, with "
     "confirmed: true. Returns the job; poll get_job for progress.",
     "inputSchema": _obj({**JOB_ARGS, "confirmed": {"type": "boolean", "description":
                          "true only once the user has said yes to the preview"}}, ["confirmed"])},
    {"name": "list_folder", "description": "What's in a folder: how many files of each type, the total size, "
     "subfolders and up to 200 names. Never file contents. Use it to find the files the user means.",
     "inputSchema": _obj({"path": {**S, "description": "Absolute path of the folder"}}, ["path"]),
     "annotations": {"readOnlyHint": True}},
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
        if name == "list_folder":
            return c.call("folder", path=args["path"])
        if name in ("start_job", "preview_job"):
            if name == "start_job" and args.get("confirmed") is not True:
                raise ControlError("show the user preview_job's result first; call start_job with confirmed: true "
                                   "only after they say yes")
            args = {k: v for k, v in args.items() if k != "confirmed"}
            method = "start" if name == "start_job" else "preview"
            if not args.get("path") and not args.get("files") and not args.get("folder"):
                raise ControlError("give path (one project or video), or files or a folder (a batch)")
            if args.get("files") or args.get("folder"):
                spec = {k: v for k, v in args.items() if v is not None}
                spec.setdefault("kind", "batch")
                return c.call(method, **spec)
            kind = c.call("inspect", path=args["path"])["kind"]
            spec = {"kind": kind, **{k: v for k, v in args.items() if v is not None}}
            if kind != "video":
                spec.pop("preset", None)
            return c.call(method, **spec)
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
                                 "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                                 "serverInfo": {"name": "macmesh", "version": __version__},
                                 "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": TOOLS})
        if method == "prompts/list":
            return _result(rid, {"prompts": PROMPTS})
        if method == "prompts/get":
            params = msg.get("params") or {}
            if params.get("name") != "macmesh":
                return _error(rid, -32602, f"Unknown prompt: {params.get('name')}")
            request = str((params.get("arguments") or {}).get("request") or "").strip()
            text = PROMPT.replace("{request}", request or "(nothing yet: ask what they'd like to run)")
            return _result(rid, {"description": PROMPTS[0]["description"],
                                 "messages": [{"role": "user", "content": {"type": "text", "text": text}}]})
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
