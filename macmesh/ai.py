"""Asking an AI agent (Claude Code or Antigravity, through their CLIs) and reading its answers.

The agent never runs anything for macmesh: it reads a prompt and answers with JSON, which
macmesh checks before acting on it. It runs in an empty temporary folder with its tools turned
off where the CLI allows (Claude Code: --tools ""), or read-only (Antigravity: --mode plan
--sandbox). Prompts may include project files' contents (text and JSON only, size-capped);
media is never sent.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# In order of preference for agent "auto".
AGENT_ORDER = ["claude", "antigravity"]
SEARCH = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
MEDIA_EXT = {".mov", ".mp4", ".m4v", ".mkv", ".mxf", ".avi", ".webm", ".wav", ".aif", ".aiff", ".mp3", ".m4a",
             ".flac", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".dpx", ".psd", ".heic", ".gif"}
FILE_LIMIT = 64_000      # characters of one project file
TOTAL_LIMIT = 200_000    # characters of project contents in one prompt


class AgentError(RuntimeError):
    pass


def _which(program: str) -> str | None:
    found = shutil.which(program)
    if found:
        return found
    for folder in SEARCH:
        candidate = Path(folder).expanduser() / program
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def available(cfg: dict) -> dict[str, str]:
    """Configured agents whose CLI is installed: name -> executable."""
    out = {}
    for name, template in cfg["agents"].items():
        exe = _which(template[0])
        if exe:
            out[name] = exe
    return out


def pick(cfg: dict, agent: str | None = None) -> str:
    """The agent to use: the one asked for, the config's choice, or for "auto" the first installed."""
    choice = agent or cfg.get("agent", "auto")
    have = available(cfg)
    if choice != "auto":
        if choice not in cfg["agents"]:
            raise AgentError(f"unknown agent {choice!r}; configured: {', '.join(sorted(cfg['agents']))}")
        if choice not in have:
            raise AgentError(f"{choice} isn't installed on this Mac ({cfg['agents'][choice][0]} wasn't found)")
        return choice
    for name in AGENT_ORDER + sorted(cfg["agents"]):
        if name in have:
            return name
    raise AgentError("no AI agent found: install Claude Code (claude) or Antigravity (agy)")


PREFACE = ("Answer from the information in this message alone. Don't use tools, run commands or open files: "
           "everything you need is below, and you're in an empty folder. Reply directly.\n\n")
REMINDER = ("Your last reply was empty because you tried to use a tool, which isn't allowed here. "
            "Reply now with the JSON only.\n\n")


def ask(prompt: str, cfg: dict, agent: str | None = None) -> str:
    """Run the agent on `prompt` and return what it printed (asking again once if it said nothing)."""
    out = _run(PREFACE + prompt, cfg, agent)
    if _empty(out):
        out = _run(PREFACE + REMINDER + prompt, cfg, agent)
        if _empty(out):
            raise AgentError("the agent didn't answer (it tried to use tools instead); try again, or switch agents")
    return out


def _empty(out: str) -> bool:
    """A CLI reply with no answer in it: blank, or a JSON wrapper whose answer is blank."""
    text = out.strip()
    if not text:
        return True
    try:
        reply = json.loads(text)
    except ValueError:
        return False
    if not isinstance(reply, dict):
        return False
    answers = [reply.get(f) for f in ("result", "response", "text", "output", "content") if f in reply]
    return bool(answers) and all(isinstance(a, str) and not a.strip() for a in answers)


def _run(prompt: str, cfg: dict, agent: str | None = None) -> str:
    name = pick(cfg, agent)
    template = cfg["agents"][name]
    argv = [available(cfg)[name]] + [a.replace("{prompt}", prompt) for a in template[1:]]
    if os.environ.get("MACMESH_AGENT_TTY") and platform.system() == "Darwin":
        argv = ["/usr/bin/script", "-q", "/dev/null"] + argv  # for CLIs that hang without a terminal
    timeout = float(cfg.get("agent_timeout", 300))
    with tempfile.TemporaryDirectory(prefix="macmesh-agent-") as empty:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, cwd=empty)
        except subprocess.TimeoutExpired:
            raise AgentError(f"{name} didn't answer within {timeout:.0f} s") from None
        except OSError as e:
            raise AgentError(f"couldn't run {name}: {e}") from None
    if proc.returncode != 0:
        raise AgentError(_failure(name, proc))
    return proc.stdout


SIGN_IN = {"claude": "Open Terminal, run `claude` and sign in again.",
           "antigravity": "Open Terminal, run `agy` and sign in again."}


def _failure(name: str, proc: subprocess.CompletedProcess) -> str:
    """A readable reason from a CLI that failed: its JSON error message when it gave one."""
    text = (proc.stdout or proc.stderr or "").strip()
    try:
        reply = json.loads(text)
        message = str(reply.get("result") or reply.get("error") or "") if isinstance(reply, dict) else ""
    except ValueError:
        message = ""
    message = message or text.splitlines()[-1][-400:] if text else f"status {proc.returncode}"
    label = {"claude": "Claude Code", "antigravity": "Antigravity"}.get(name, name)
    if any(w in message.lower() for w in ("authenticat", "log in", "login", "sign in", "oauth", "credential")):
        return f"{label} isn't signed in ({message}). {SIGN_IN.get(name, '')}".strip()
    return f"{label} couldn't answer: {message}"


def extract_json(raw: str, key: str) -> dict:
    """The first JSON object containing `key` in what a CLI printed (wrappers and prose allowed)."""
    text = raw.strip()
    try:
        wrapper = json.loads(text)
        if isinstance(wrapper, dict) and key in wrapper:
            return wrapper
        if isinstance(wrapper, dict):  # claude: {"result": "..."}; others: {"response"/"text"/"output": ...}
            for field in ("result", "response", "text", "output", "content"):
                if isinstance(wrapper.get(field), str):
                    text = wrapper[field]
                    break
    except ValueError:
        pass
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    decoder = json.JSONDecoder()
    for candidate in fenced + [text[i:] for i, ch in enumerate(text) if ch == "{"]:
        try:
            obj, _ = decoder.raw_decode(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict) and key in obj:
            return obj
    raise AgentError(f"the agent's answer had no JSON with {key!r}:\n{text[:1200]}")


def _readable(path: Path) -> str | None:
    if path.suffix.lower() in MEDIA_EXT or path.stat().st_size > 8 * FILE_LIMIT:
        return None
    data = path.read_bytes()[: 4 * FILE_LIMIT]
    if b"\0" in data:
        return None  # binary
    text = data.decode("utf-8", errors="replace")
    return text if len(text) <= FILE_LIMIT else text[:FILE_LIMIT] + "\n… (cut)"


def project_contents(paths: list[str]) -> list[dict]:
    """Text of the project files named (and of text files inside project bundles), capped."""
    out, total = [], 0
    for raw in paths:
        p = Path(raw).expanduser()
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())[:40] if p.is_dir() else []
        for f in files:
            if total >= TOTAL_LIMIT:
                return out
            try:
                text = _readable(f)
            except OSError:
                continue
            if text is None:
                continue
            text = text[: TOTAL_LIMIT - total]
            total += len(text)
            out.append({"path": str(f), "contents": text})
    return out


# ---- explaining a failure ----------------------------------------------------
EXPLAIN = """\
You are helping someone whose macmesh job failed. macmesh splits a render or encode into chunks, runs
each chunk on a Mac on their network (sandboxed: no network, no access to their files, writes only to
the task folder), then joins the pieces.

Say in plain words what went wrong and what they can do about it. Be specific to the logs. Reply with
ONE JSON object and nothing else:
{"summary": "1-2 sentences: what went wrong", "fix": "1-3 sentences: what to do", "where": "this Mac" |
"another Mac" | "the project" | "macmesh" | "unknown"}

JOB:
"""


def explain(job: dict, spec: dict, profile: dict | None, cfg: dict, agent: str | None = None) -> dict:
    facts = {"name": job.get("name"), "state": job.get("state"), "error": job.get("error"),
             "log": job.get("log", [])[-30:], "spec": spec, "app_profile": profile}
    answer = extract_json(ask(EXPLAIN + json.dumps(facts, indent=1, default=str), cfg, agent), "summary")
    return {"summary": str(answer.get("summary", "")), "fix": str(answer.get("fix", "")),
            "where": str(answer.get("where", "unknown"))}
