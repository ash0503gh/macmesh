"""Teaching macmesh a new app: an AI agent writes the profile, a test render proves it.

1. Read the program's help (run inside the sandbox: no network, no writes outside a temp folder).
2. Ask the agent for a profile (see macmesh/profiles.py); it may ask for up to 6 more help pages.
3. Check the profile, then render the same few frames of a sample project twice: as one task on
   this Mac, and split into chunks across the mesh. The two must match frame for frame, or the
   pieces don't join back exactly and the profile is no good.
4. On failure the agent sees what went wrong and tries again (3 attempts). Only a profile that
   passed is saved, in ~/.macmesh/apps/.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ai, config, jobspec, media, mesh, profiles, sandbox, toolinfo, wire
from .planners.app import plan_app
from .scheduler import JobFailed, Scheduler

MAX_ATTEMPTS = 3
MAX_HELP = 6
TEST_FRAMES = 12
TEST_CHUNK = 4

PROMPT = """\
You are writing an app profile for macmesh, which splits a render across the Macs on a network:
each Mac renders a range of frames of the same project with the app's command-line tool, and
macmesh joins the pieces. Write the profile for the program below.

THE PROFILE FORMAT (from macmesh/profiles.py):
{doc}

PROFILES THAT WORK (for other apps):
{examples}

RULES
- Steps are the program's arguments WITHOUT the program name. Use only flags the help shows.
- Pieces must join back exactly. Movie chunks: an intra-frame codec (ProRes) with PCM sound in .mov,
  "out_ext": ".mov", "merge": "concat". Or frames: "out_ext": "", "merge": "dirs", one file per frame
  named by its absolute frame number (so chunks never collide).
- Frame ranges: {{start}} and {{end}} are inclusive, {{end_excl}} is end + 1. If the app takes seconds,
  use {{start_s}} {{end_s}} and give an fps_probe, or a frames_probe that reports seconds.
- The project travels with its media into {{indir}}. If the project refers to media by absolute paths
  and the app can relink from a folder, make the first step a relink to {{indir}} and set "media": "scan".
- If the app might succeed while rendering placeholders for missing media or fonts, add fail_if regexes
  that match what it prints then.
- Tasks run sandboxed: no network, writes only inside the task folder (TMPDIR points there). If the app
  must read or write elsewhere (its caches, models), list those folders under "sandbox".
- Give "extensions" (the project file types) and a "frames_probe" if the tool can report the length.
- Name it "{name}"; program.path "{program}".

Reply with ONE JSON object and nothing else, either
  {{"profile": {{...}}, "explanation": "what it does, in one or two sentences"}}
or, if you need more help pages first (at most {max_help}, each ending in --help or -h, or starting with help):
  {{"need_help": [["render", "--help"], ...]}}

PROGRAM: {program}
HELP:
{help}

SAMPLE PROJECT (what the test render uses): {sample}
{sample_contents}
"""

RETRY = """
YOUR LAST PROFILE:
{profile}
IT FAILED THE TEST RENDER:
{failure}
Fix the profile. Same reply format.
"""


class TeachError(RuntimeError):
    pass


def run_help(program: str, args: list[str]) -> str:
    """The program's output for a help request, sandboxed, cut to 12,000 characters."""
    if not (args and (args[-1] in ("--help", "-h") or args[0] == "help")):
        raise TeachError(f"not a help request: {args}")
    with tempfile.TemporaryDirectory(prefix="macmesh-help-") as d:
        argv = [program, *args]
        if sandbox.available():
            argv = sandbox.wrap(argv, Path(d), {})
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=20, cwd=d,
                               stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "TMPDIR": d, "HOME": d})
        except (OSError, subprocess.SubprocessError) as e:
            return f"(couldn't run: {e})"
    return (p.stdout + p.stderr)[:12_000] or "(no output)"


def _examples() -> str:
    shipped = {**profiles.examples(), **profiles.load(Path("/nonexistent"))}
    return json.dumps([{k: v for k, v in p.items() if k != "source"} for p in shipped.values()], indent=1)


def _hashes(path: Path) -> list[str]:
    """Per-frame identity of a render: decoded frame hashes for a movie, file hashes for frames."""
    if path.is_dir():
        return [f"{f.name}:{wire.sha256_file(f)}" for f in sorted(path.iterdir()) if f.is_file()]
    if not shutil.which("ffmpeg"):
        raise TeachError("ffmpeg is needed to compare movie renders (brew install ffmpeg)")
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise TeachError(f"the render isn't a readable movie: {out.stderr.strip()[-300:]}")
    return [line.rsplit(",", 1)[1].strip() for line in out.stdout.splitlines() if line and not line.startswith("#")]


def verify(profile: dict, sample: str, cfg: dict, home: Path, log=lambda m: None) -> str:
    """Render a few frames whole and in chunks; raise TeachError unless they match. Returns a summary."""
    name = profile["name"]
    frames = TEST_FRAMES
    try:
        from .planners.app import probe_frames
        frames = min(TEST_FRAMES, probe_frames(profile, sample, profile.get("vars", {}), cfg, home=home))
    except ValueError:
        pass
    end = max(frames, 2) - 1
    clients = mesh.find_nodes(cfg, seconds=1.0)
    here = [c for c in clients if c.info.get("node_id") == cfg["node_id"]]
    if not here:
        raise TeachError("this Mac's engine isn't reachable, so it can't run the test render")
    with tempfile.TemporaryDirectory(prefix="macmesh-teach-") as d:
        outputs = {}
        for label, chunk, nodes in (("whole", end + 1, here), ("chunks", TEST_CHUNK, clients)):
            try:
                job = jobspec.validate(plan_app(name, sample, 0, end, chunk=chunk, workdir=str(Path(d) / label),
                                                cfg=cfg, home=home))
            except (ValueError, jobspec.JobError) as e:  # e.g. a {var} with no default: the agent can fix that
                raise TeachError(f"the profile can't plan a render: {e}. Give every placeholder a default in vars.") from None
            sched = Scheduler(job, nodes, log=log, max_attempts=1, reference=toolinfo.fingerprints(cfg, home))
            try:
                sched.run()
                media.run_merge(Path(job["workdir"]), job["merge"], log=log)
            except (JobFailed, RuntimeError, OSError) as e:
                raise TeachError(f"the {label} render failed: {e}") from None
            merged = job["merge"][0] if job["merge"] else None
            target = (merged.get("output") or merged.get("into")) if merged else "chunks"
            outputs[label] = Path(job["workdir"]) / target
            if not outputs[label].exists():
                raise TeachError(f"the {label} render made no output at {target}")
        whole, chunks = _hashes(outputs["whole"]), _hashes(outputs["chunks"])
        if len(whole) != end + 1:
            raise TeachError(f"rendering frames 0-{end} gave {len(whole)} frames, not {end + 1}")
        if whole != chunks:
            first = next((i for i, (a, b) in enumerate(zip(whole, chunks)) if a != b), min(len(whole), len(chunks)))
            raise TeachError(f"the chunks don't join back exactly: {len(chunks)} frames vs {len(whole)}, "
                             f"first difference at frame {first}")
    return f"frames 0-{end} render the same whole and in {-(-(end + 1) // TEST_CHUNK)} chunks"


def _answer(raw: str) -> dict:
    """The agent's reply: a profile, or a request for more help pages."""
    try:
        return ai.extract_json(raw, "profile")
    except ai.AgentError:
        return ai.extract_json(raw, "need_help")


def teach(program: str, name: str, sample: str, cfg: dict, home: Path | None = None,
          agent: str | None = None, log=lambda m: None) -> dict:
    """Write, verify and save a profile for `program`. Returns {"profile", "explanation", "verified", "attempts"}."""
    home = home or config.home()
    program = str(Path(program).expanduser().resolve())
    if not Path(program).is_file():
        raise TeachError(f"there's no program at {program}")
    if not profiles.NAME.match(name or ""):
        raise TeachError("give the app a short name: letters, digits, '.', '_' or '-'")
    if not Path(sample).expanduser().exists():
        raise TeachError(f"there's no sample project at {sample}")
    log("Reading the program's help")
    helps = {" ".join(a): run_help(program, a) for a in (["--help"], ["help"])}
    contents = ai.project_contents([sample])
    dest = home / "apps" / f"{name}.json"
    before = dest.read_text() if dest.exists() else None
    failure, last, asked_more = "", None, False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = PROMPT.format(doc=profiles.__doc__, examples=_examples(), name=name, program=program,
                               max_help=MAX_HELP, help=json.dumps(helps, indent=1), sample=sample,
                               sample_contents=json.dumps(contents, indent=1))
        if last is not None:
            prompt += RETRY.format(profile=json.dumps(last, indent=1), failure=failure)
        log(f"Asking for a profile (attempt {attempt})")
        try:
            answer = _answer(ai.ask(prompt, cfg, agent))
        except ai.AgentError as e:
            raise TeachError(str(e)) from None
        if "need_help" in answer and not asked_more:
            asked_more = True
            for args in answer["need_help"][:MAX_HELP]:
                if isinstance(args, list) and all(isinstance(a, str) for a in args):
                    try:
                        helps[" ".join(args)] = run_help(program, args)
                    except TeachError:
                        continue
            continue
        draft = answer.get("profile")
        try:
            draft = profiles.validate({**(draft or {}), "name": name,
                                       "program": {**(draft or {}).get("program", {}), "path": program}})
        except (profiles.ProfileError, TypeError, AttributeError) as e:
            last, failure = draft, f"the profile isn't valid: {e}"
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(draft, indent=2) + "\n")  # the workers here read it from this folder
        try:
            log("Test render: whole, then in chunks")
            verified = verify(draft, sample, cfg, home, log=log)
        except TeachError as e:
            last, failure = draft, str(e)
            log(f"Test render failed: {e}")
            if before is None:
                dest.unlink(missing_ok=True)
            else:
                dest.write_text(before)
            continue
        return {"profile": draft, "explanation": str(answer.get("explanation", "")), "verified": verified,
                "attempts": attempt, "path": str(dest)}
    raise TeachError(f"no working profile after {MAX_ATTEMPTS} tries. Last problem: {failure}")
