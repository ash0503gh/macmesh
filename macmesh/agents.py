"""/macmesh in Claude Code and Antigravity: a skill with the same intake as the MCP prompt, and the
MCP server registered with each agent that's installed.

Both agents run a skill when the user types /<its name>; the skill tells the agent how to use the
macmesh MCP tools (mcp.PROMPT, the one source of the intake). `install()` writes the skill where each
agent looks (~/.claude/skills/macmesh, ~/.gemini/config/skills/macmesh) and runs the agent's own
`mcp add` when macmesh isn't registered yet. Nothing is touched for an agent that isn't installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import ai, mcp

SKILL_DIRS = {"claude": "~/.claude/skills/macmesh", "antigravity": "~/.gemini/config/skills/macmesh"}


def skill_text() -> str:
    body = mcp.PROMPT.replace("{request}", "the text after /macmesh, if any (else ask what they'd like to run)")
    return (
        "---\n"
        "name: macmesh\n"
        "description: >-\n"
        "  Run heavy work (renders, video encodes, a batch of files such as songs to split into stems)\n"
        "  across the user's Macs with macmesh: asks a few questions, previews the plan, starts only on\n"
        "  their yes. Use when the user types /macmesh, or asks to split, render, encode or batch-process\n"
        "  work across their Macs. Needs the macmesh MCP server (tools get_status, list_folder,\n"
        "  preview_job, start_job, get_job...).\n"
        "---\n\n"
        "# macmesh\n\n" + body + "\n"
    )


def engine_binary() -> str:
    """The `macmesh` command of this installation (inside the app, or the one on PATH)."""
    exe = Path(sys.executable).resolve()
    for candidate in (exe.parent.parent.parent / "bin" / "macmesh", exe.parent / "macmesh"):
        if candidate.is_file():
            return str(candidate)
    return "macmesh"


def _registered(program: str, list_args: list[str]) -> bool:
    try:
        out = subprocess.run([program, *list_args], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[:1] == ["macmesh"] or line.startswith("macmesh:") for line in out.splitlines())


def install(cfg: dict) -> list[str]:
    """Set up /macmesh for each agent installed here; returns what was done, in plain words."""
    done = []
    found = ai.available(cfg)  # {"claude": exe, "antigravity": exe} for those installed
    binary = engine_binary()
    for name, folder in SKILL_DIRS.items():
        label = {"claude": "Claude Code", "antigravity": "Antigravity"}[name]
        if name not in found:
            done.append(f"{label} isn't installed here: skipped")
            continue
        skill = Path(folder).expanduser() / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(skill_text())
        done.append(f"{label}: /macmesh installed ({skill})")
        exe = found[name]
        if name == "claude":
            listed, add = ["mcp", "list"], ["mcp", "add", "--scope", "user", "macmesh", "--", binary, "mcp"]
        else:
            listed, add = ["mcp", "list"], ["mcp", "add", "macmesh", binary, "mcp"]
        if _registered(exe, listed):
            done.append(f"{label}: the macmesh MCP server was already registered")
            continue
        try:
            r = subprocess.run([exe, *add], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        done.append(f"{label}: macmesh MCP server " + ("registered" if ok else
                    f"not registered; run: {Path(exe).name} {' '.join(add)}"))
    return done
