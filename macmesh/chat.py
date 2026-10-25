"""Planning a job by talking it through with the AI, until both sides agree on it.

Each turn sends the agent the whole conversation, the Macs, the tools and what's in the folders
the person picked (names, sizes and types: never file contents), and gets back a reply, and maybe
a request for a folder, a plan, or suggestions (workers per Mac, which Macs, where results go).
A plan is checked with the same planners and safety rules as one-shot planning before it's shown,
as a card the person runs. Nothing starts without their click. Conversations are saved in
~/.macmesh/chats/ and linked to the jobs they started.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from . import ai, config, jobspec, profiles, toolinfo
from .planners.ai import SCHEMA_DOC, build_prompt, plan_to_job, unsafe_reasons

SAMPLE_NAMES = 40       # file names per folder shown to the agent
MAX_FILES = 20000       # files counted per folder
HISTORY_TURNS = 30      # messages of the conversation sent each turn

CHAT_DOC = """\
You are talking with the person who wants this done. Work out together exactly what job to run,
then propose it. Keep replies short and plain (1-4 sentences). Ask ONE question at a time, only
when you can't decide sensibly yourself. You can't see their disk: when you need files, ask for a
folder with "ask_folder" and they'll pick it; you'll then see what it contains.

Reply with ONE JSON object and nothing else:
{
  "reply": "what you say to them",
  "ask_folder": "why you need a folder (e.g. 'Which folder has the songs?')" or null,
  "plan": null, or a plan as described below once you both agree on what to do,
  "suggest": null or {"slots": {"<Mac name>": tasks at once}, "macs": ["<Mac name>", ...],
                      "output_folder": "a new folder name or path for the results"}
}
A plan replaces nothing until they press Run, so propose one as soon as the job is clear, and
change it when they ask. In a plan you may use every file in a picked folder without listing them:
for planner "batch", "files" may contain {"folder": "<picked folder>", "extensions": [".mp3", ...],
"recursive": true|false}. "suggest.slots" is how many tasks a Mac runs at once: tools that leave the
GPU or cores partly idle gain from 2-4 at once on a 16 GB Mac; never more than its performance cores.
"""


def folder_summary(path: str, names: int = SAMPLE_NAMES) -> dict:
    """What's in a folder, for the agent: counts by type, size, and some names. Never contents."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise OSError(f"there's no folder at {root}")
    files, top = [], []
    for p in root.rglob("*"):
        if p.is_file() and not p.name.startswith("."):
            files.append(p)
            if p.parent == root:
                top.append(p)
            if len(files) >= MAX_FILES:
                break
    by_ext = Counter(p.suffix.lower() or "(none)" for p in files)
    return {"folder": str(root), "files": len(files), "files_directly_inside": len(top),
            "subfolders": sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))[:20],
            "by_type": dict(by_ext.most_common(12)),
            "total_gb": round(sum(p.stat().st_size for p in files) / 1e9, 2),
            "some_names": [p.relative_to(root).as_posix() for p in sorted(files)[:names]]}


def files_in(folder: str, extensions: list[str] | None = None, recursive: bool = False) -> list[str]:
    """The files in a folder (optionally its subfolders), of the given types, in name order."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"there's no folder at {root}")
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions or []}
    walk = root.rglob("*") if recursive else root.iterdir()
    return sorted(str(p) for p in walk if p.is_file() and not p.name.startswith(".")
                  and (not exts or p.suffix.lower() in exts))


def expand_files(plan: dict, folders: list[str]) -> dict:
    """Turn {"folder", "extensions", "recursive"} entries in a batch plan into the files they mean."""
    if plan.get("planner") != "batch" or not isinstance(plan.get("params", {}).get("files"), list):
        return plan
    roots = [Path(f).expanduser().resolve() for f in folders]
    files: list[str] = []
    for entry in plan["params"]["files"]:
        if isinstance(entry, str):
            files.append(entry)
            continue
        if not isinstance(entry, dict) or "folder" not in entry:
            raise ValueError(f"files entries are paths or {{'folder', 'extensions'}}, not {entry!r}")
        folder = Path(entry["folder"]).expanduser().resolve()
        if not any(folder == r or r in folder.parents for r in roots):
            raise ValueError(f"{folder} isn't a folder they picked")
        files += files_in(str(folder), entry.get("extensions"), bool(entry.get("recursive")))
    if not files:
        raise ValueError("that matches no files")
    return dict(plan, params=dict(plan["params"], files=files))


class Chat:
    def __init__(self, home: Path, title: str = ""):
        self.home = home
        self.id = uuid.uuid4().hex[:8]
        self.created = time.time()
        self.title = title
        self.messages: list[dict] = []   # {"role": "user"|"ai"|"note", "text", "at"}
        self.folders: list[str] = []
        self.paths: list[str] = []       # files added with the request
        self.state = "waiting"           # thinking | waiting (for the person) | failed
        self.error = ""
        self.ask_folder: str | None = None
        self.plan: dict | None = None    # the agent's plan, files expanded
        self.card: dict | None = None    # what the plan would do, for the Run card
        self.suggest: dict = {}
        self.job_ids: list[str] = []
        self.lock = threading.Lock()

    def path(self) -> Path:
        return self.home / "chats" / f"{self.id}.json"

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {k: getattr(self, k) for k in ("id", "created", "title", "messages", "folders", "paths", "error",
                                               "ask_folder", "plan", "card", "suggest", "job_ids")}
        data["state"] = "waiting" if self.state == "thinking" else self.state
        p.write_text(json.dumps(data, indent=1))

    @classmethod
    def load(cls, home: Path, chat_id: str) -> "Chat":
        p = home / "chats" / f"{chat_id}.json"
        if not re.fullmatch(r"[0-9a-f]{8}", chat_id) or not p.exists():
            raise KeyError(chat_id)
        data = json.loads(p.read_text())
        chat = cls(home)
        for k, v in data.items():
            setattr(chat, k, v)
        return chat

    def add(self, role: str, text: str) -> None:
        self.messages.append({"role": role, "text": text, "at": time.time()})
        if role == "user" and not self.title:
            self.title = text.strip().splitlines()[0][:60]

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "created": self.created, "messages": self.messages,
                "folders": self.folders, "state": self.state, "error": self.error, "ask_folder": self.ask_folder,
                "card": self.card, "suggest": self.suggest, "job_ids": self.job_ids}


def _prompt(chat: Chat, nodes: list[dict], cfg: dict, home: Path) -> str:
    apps = {k: p for k, p in profiles.load(home).items() if p.get("range")}
    base = build_prompt("(see the conversation below)", chat.paths, nodes, toolinfo.tools(cfg, home), apps)
    base = base.replace(SCHEMA_DOC, "")  # the plan format is repeated below, inside the reply format
    talk = [f"{'THEM' if m['role'] == 'user' else 'YOU' if m['role'] == 'ai' else 'MACMESH'}: {m['text']}"
            for m in chat.messages[-HISTORY_TURNS:]]
    folders = [folder_summary(f) for f in chat.folders]
    return (f"{base}\nFOLDERS THEY PICKED:\n{json.dumps(folders, indent=1)}\n\n"
            f"CONVERSATION SO FAR:\n" + "\n".join(talk) + "\n\n"
            f"{CHAT_DOC}\nPLAN FORMAT (for \"plan\"):\n{SCHEMA_DOC}")


def turn(chat: Chat, nodes: list[dict], cfg: dict, home: Path, workdir: str, agent: str | None = None) -> None:
    """One reply from the agent. A plan that doesn't check out goes back to the agent once to fix."""
    for attempt in (1, 2):
        raw = ai.ask(_prompt(chat, nodes, cfg, home), cfg, agent)
        try:
            answer = ai.extract_json(raw, "reply")
        except ai.AgentError:
            answer = {"reply": raw.strip()[:2000]}  # plain prose: still a reply
        plan, problem = answer.get("plan"), ""
        if isinstance(plan, dict) and plan.get("planner"):
            try:
                plan = expand_files(plan, chat.folders)
                job = jobspec.validate(plan_to_job(plan, chat.folders + chat.paths, cfg, workdir=workdir, home=home))
            except (ValueError, KeyError, TypeError) as e:
                problem = str(e)
            else:
                chat.plan, chat.card = plan, card(plan, job, nodes, home)
        if problem and attempt == 1:
            chat.add("note", f"macmesh couldn't use that plan: {problem}. Fix it, or ask them what's missing.")
            continue
        chat.add("ai", str(answer.get("reply") or "").strip() or "(no reply)")
        if problem:
            chat.add("note", f"The plan couldn't be used: {problem}")
        ask = answer.get("ask_folder")
        chat.ask_folder = str(ask) if ask else None
        if isinstance(answer.get("suggest"), dict):
            chat.suggest = {k: v for k, v in answer["suggest"].items() if k in ("slots", "macs", "output_folder") and v}
        return


def card(plan: dict, job: dict, nodes: list[dict], home: Path) -> dict:
    """What a plan would do, for the Run card."""
    programs = sorted({p for t in job["tasks"] for p in jobspec.programs(t)})
    first = job["tasks"][0]
    missing = {n["node_name"]: [p for p in programs if p not in n.get("programs", [])] for n in nodes}
    names = {**{f"{{in:{k}}}": Path(v).name for k, v in first.get("inputs", {}).items()},
             **{f"{{out:{k}}}": Path(v).name for k, v in first.get("outputs", {}).items()}}
    example = [" ".join(names.get(a, a) for a in step) for step in first["steps"]]
    return {"explanation": plan.get("explanation", ""), "planner": plan["planner"], "tasks": len(job["tasks"]),
            "programs": programs, "example": example,
            "name": plan["params"].get("name") or job.get("name") or "job",
            "reasons": unsafe_reasons(job, set(profiles.load(home))),
            "needs_install": {mac: progs for mac, progs in missing.items() if progs}}
