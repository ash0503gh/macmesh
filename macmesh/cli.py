"""macmesh command line."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path

from . import PAIR_SERVICE_TYPE, __version__, config, discovery, jobspec, mesh, pairing, pki, profiles, toolinfo
from .client import NodeClient, NodeError
from .planners import ENCODE_PRESETS, PLANNERS


_log_lock = threading.Lock()


def log(msg: str) -> None:
    with _log_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---- node discovery ----------------------------------------------------
def gather_nodes(cfg: dict, args) -> list[NodeClient]:
    return mesh.find_nodes(cfg, peers=getattr(args, "peer", None) or [],
                           discover=not getattr(args, "no_discovery", False),
                           seconds=getattr(args, "discover_seconds", 2.0), log=log)


def print_nodes(clients: list[NodeClient]) -> None:
    if not clients:
        print("No macmesh nodes found. Is `macmesh serve` running on each Mac, with the same token?")
        return
    print(f"{'NODE':<18} {'MODEL':<18} {'CHIP':<16} {'CORES':>6} {'MEM':>5} {'SCORE':>6} "
          f"{'SLOTS':>5}  POWER         PROGRAMS")
    for c in clients:
        i, hw, pw = c.info, c.info["hardware"], c.info["power"]
        cores = f"{hw['perf_cores']}+{hw['eff_cores']}"
        power = "battery " + (f"{pw['battery_pct']}%" if pw["battery_pct"] is not None else "") if pw["on_battery"] else "AC"
        if not i.get("accepting", True):
            power += f" (not taking work: {i.get('refusal') or 'paused'})"
        elif i.get("paused"):
            power += " (owner busy: tasks paused)"
        model = hw["model"] + (" ❄" if hw["fanless"] else "")
        print(f"{i['node_name'][:18]:<18} {model[:18]:<18} {hw['chip'][:16]:<16} {cores:>6} "
              f"{hw['memory_gb']:>4}G {hw['score']:>6} {i['slots']:>5}  {power:<13} {', '.join(i['programs'])}")
    if any(c.fingerprint for c in clients):
        print("\ncertificates (for `macmesh revoke`):")
        for c in clients:
            sandboxed = "sandboxed" if c.info.get("sandboxed") else "NOT sandboxed"
            print(f"  {c.info['node_name'][:18]:<18} {c.fingerprint}  {sandboxed}")


# ---- running a job -------------------------------------------------------
def describe(job: dict) -> None:
    tasks = job["tasks"]
    print(f"\nJob '{job['name']}': {len(tasks)} task(s) → {job['workdir']}")
    for t in tasks[:3]:
        print(f"  {t['id']}: " + "  &&  ".join(shlex.join(step) for step in t["steps"]))
    if len(tasks) > 3:
        print(f"  … {len(tasks) - 3} more")
    for step in job.get("merge", []):
        print(f"  then merge: {step['op']}")


def confirm(args) -> bool:
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("Not a terminal; pass --yes to run without confirmation.")
        return False
    return input("Run it? [Y/n] ").strip().lower() in ("", "y", "yes")


def execute(job: dict, cfg: dict, args, kind: str = "job") -> int:
    """Run a planned job in this Mac's macmesh engine, so the app shows it live (activity, graphs,
    history) like a job started there or by an agent. Follows it here until it's done."""
    from .control import ControlClient, ControlError
    final = job.pop("final", None)
    job = jobspec.validate(job)
    if getattr(args, "save_job", None):
        jobspec.save(job, args.save_job)
        print(f"job saved to {args.save_job}")
    describe(job)
    if getattr(args, "dry_run", False):
        return 0
    try:
        client = ControlClient()
        status = client.call("status")
    except ControlError as e:
        print(f"{e}\nJobs run in macmesh's engine so the app can show them. Open the macmesh app, or run "
              "`macmesh serve` in another Terminal, then try again.")
        return 1
    if getattr(args, "peer", None) or getattr(args, "no_discovery", False):
        print("note: --peer and --no-discovery are ignored; the engine finds the Macs (static peers go in the config).")
    print()
    for m in status["macs"]:
        print(f"  {m['name']:<28} {m.get('chip') or '':<16} {m['state']:<12} {', '.join(m.get('programs', []))}")
    print()
    if not confirm(args):
        return 1
    options = {"max_attempts": getattr(args, "retries", None), "keep_blobs": getattr(args, "keep_blobs", None),
               "match": getattr(args, "match", None),
               "speculate": None if getattr(args, "no_speculate", None) is None else not args.no_speculate}
    record = client.call("submit", job=job, final=final, kind=kind, options=options)
    log(f"job {record['id']} is running in macmesh's engine (open the app to watch it)")
    seen: set = set()
    try:
        while True:
            j = client.call("job", id=record["id"])
            for e in j.get("events", []):
                if (e["at"], e["text"]) not in seen:
                    seen.add((e["at"], e["text"]))
                    log("  " + e["text"])
            if j["state"] in ("done", "failed", "cancelled"):
                break
            time.sleep(1)
    except KeyboardInterrupt:
        client.call("cancel", id=record["id"])
        log("interrupted; the job was cancelled on every Mac")
        return 130
    if j["state"] != "done":
        log(f"JOB FAILED: {j['error'] or j['state']}")
        return 1
    log(f"output: {j['output']}")
    log(f"done in {j['elapsed']:.1f}s" + (f" · {j['speedup']}× as many tasks at once as one Mac runs" if j.get("speedup") else ""))
    for m in j["macs"]:
        log(f"  {m['name']}: {m['done']} task(s)")
    return 0


# ---- commands --------------------------------------------------------------
def _apply_init_args(cfg: dict, args) -> None:
    if args.name:
        cfg["node_name"] = args.name
    if args.port:
        cfg["port"] = args.port
    if args.slots:
        cfg["slots"] = args.slots


def cmd_init(args) -> int:
    """Start a new mesh on this Mac: it holds the mesh's certificate authority."""
    p = config.config_path()
    if p.exists() and not args.force:
        cfg = config.load()
        print(f"Already initialised ({p}). Use --force to start a new mesh (other Macs must join again).")
        _apply_init_args(cfg, args)
        config.save(cfg)
        return 0
    cfg = config.defaults()
    _apply_init_args(cfg, args)
    shutil.rmtree(pki.paths()["dir"], ignore_errors=True)
    pki.create_ca(cfg["node_id"])
    pki.self_issue(cfg["node_id"])
    config.save(cfg)
    print(f"config: {p}")
    print(f"node:   {cfg['node_name']} ({cfg['node_id']}) port {cfg['port']}")
    print(f"cert:   {pki.own_fingerprint()}")
    print("\nThis Mac now holds the mesh's certificate authority. To add another Mac, run")
    print("`macmesh invite` here and `macmesh join <code>` there.")
    print("Then run `macmesh serve` on every Mac (this one included).")
    return 0


def cmd_invite(args) -> int:
    cfg = config.load()
    if not pki.paths()["ca_key"].exists():
        print("This Mac doesn't hold the mesh's certificate authority; invite from the Mac you ran `macmesh init` on.")
        return 1
    from .control import ControlClient, ControlError
    try:  # the running engine already listens for new Macs: just ask it for a code
        state = ControlClient().call("invite")
        print(f"\nOn the new Mac, run:\n\n  macmesh join {state['code']}\n")
        print("(or open macmesh there and click Join: you'll approve it in the app here)", flush=True)
        return 0
    except ControlError:
        pass
    code = pairing.new_code()
    port = args.port or int(cfg["port"]) + 1
    invite = pairing.Invite(cfg, code, port)
    ad = discovery.advertise(cfg, PAIR_SERVICE_TYPE, port)
    print(f"\nOn the new Mac, run:\n\n  macmesh join {code}\n")
    print(f"(waiting up to {pairing.EXPIRY / 60:.0f} minutes; if it can't find this Mac, add --host <this Mac's address>:{port})",
          flush=True)
    try:
        name = invite.wait()
    finally:
        if ad:
            ad.close()
    print(f"Paired {name}. It can serve once it runs `macmesh serve`.")
    return 0


def cmd_nearby(args) -> int:
    """Meshes on the network that can take this Mac (their first Mac is running macmesh)."""
    found = discovery.browse_services(args.seconds, PAIR_SERVICE_TYPE)
    if args.json:
        print(json.dumps([{"name": f["name"], "addresses": f["addresses"], "mesh": f["props"].get("mesh", "")}
                          for f in found]))
    elif not found:
        print("No meshes found nearby. Is macmesh open on the first Mac, on this network?")
    else:
        for f in found:
            print(f"{f['name']:<30} {', '.join(f['addresses'])}")
    return 0


def _emit(args, event: str, **fields) -> None:
    if getattr(args, "json", False):
        print(json.dumps({"event": event, **fields}), flush=True)


def cmd_join(args) -> int:
    p = config.config_path()
    cfg = config.load() if p.exists() else config.defaults()
    if p.exists() and pki.ready() and not args.force:
        print("This Mac already belongs to a mesh. Use --force to leave it and join another.")
        return 1
    if not args.code and not args.request:
        raise ValueError("give the code from `macmesh invite`, or --request to ask the other Mac to let you in")
    _apply_init_args(cfg, args)
    if args.host:
        addresses = [h if ":" in h.replace("::", "") else f"{h}:{int(cfg['port']) + 1}" for h in args.host.split(",")]
        to = args.host
    else:
        found = discovery.browse_services(4.0, PAIR_SERVICE_TYPE)
        if not found:
            raise ValueError("no mesh found nearby. Open macmesh on the first Mac (same network), or pass --host <address>")
        if len(found) > 1:
            raise ValueError("more than one mesh is nearby: " + ", ".join(f["name"] for f in found) + ". Pick one with --host.")
        addresses, to = found[0]["addresses"], found[0]["name"]
    if args.force:
        shutil.rmtree(pki.paths()["dir"], ignore_errors=True)
    if args.request:
        def show(number: str) -> None:
            _emit(args, "number", number=number, to=to)
            if not args.json:
                print(f"\nOn {to}, check that it shows {number}, then click Allow.\n", flush=True)
        pairing.request_join(cfg, addresses, on_number=show)
    else:
        pairing.join(cfg, args.code, addresses)
    config.save(cfg)
    _emit(args, "joined", to=to, node_id=cfg["node_id"])
    if not args.json:
        print(f"Joined the mesh through {to}.")
        print(f"node:   {cfg['node_name']} ({cfg['node_id']})")
        print("Now run `macmesh serve` here (the app does this for you).")
    return 0


def cmd_leave(args) -> int:
    """Leave the mesh: forget its certificate and token. Apps you taught macmesh stay."""
    p = config.config_path()
    if not p.exists():
        print("This Mac isn't in a mesh.")
        return 0
    shutil.rmtree(pki.paths()["dir"], ignore_errors=True)
    p.unlink()
    print("Left the mesh. Start a new one or join another with the app (or `macmesh init` / `macmesh join`).")
    return 0


def cmd_revoke(args) -> int:
    cfg = config.load()
    fp = args.fingerprint.lower().replace(":", "")
    if len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        raise ValueError("give the full 64-character certificate fingerprint (see `macmesh nodes`)")
    if fp == pki.own_fingerprint():
        raise ValueError("that's this Mac's own certificate")
    if fp not in cfg["revoked"]:
        cfg["revoked"].append(fp)
    config.save(cfg)
    print(f"This Mac now refuses {fp[:16]}…  Run the same command on every other Mac in the mesh,")
    print("and restart `macmesh serve` here.")
    return 0


def cmd_serve(args) -> int:
    from .worker import serve
    serve(config.load(), host=args.host, advertise=not args.no_advertise)
    return 0


def cmd_agents(args) -> int:
    """Set up /macmesh in Claude Code and Antigravity (whichever are installed)."""
    from . import agents
    for line in agents.install(config.load()):
        print(line)
    return 0


def cmd_mcp(args) -> int:
    from .mcp import main as mcp_main
    return mcp_main()


def cmd_nodes(args) -> int:
    print_nodes(gather_nodes(config.load(), args))
    return 0


def cmd_config(args) -> int:
    cfg = config.load()
    if args.key is None:
        shown = dict(cfg, token="<hidden>")
        print(json.dumps(shown, indent=2))
        return 0
    if args.value is None:
        print(json.dumps(cfg.get(args.key), indent=2))
        return 0
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    cfg[args.key] = value
    config.save(cfg)
    print(f"{args.key} = {json.dumps(value)}")
    return 0


def cmd_run(args) -> int:
    return execute(jobspec.load(args.job), config.load(), args)


def cmd_video(args) -> int:
    job = PLANNERS["video"](args.src, args.output, preset=args.preset, vargs=args.vargs or "",
                            vf=args.vf or "", chunk_seconds=args.chunk, workdir=args.workdir or "")
    return execute(job, config.load(), args, kind="video")


def _kv(pairs, flag: str = "--input") -> dict:
    out = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep:
            raise SystemExit(f"{flag} expects name=value, got {p!r}")
        out[k] = v
    return out


def cmd_range(args) -> int:
    start, _, end = args.frames.partition("-")
    cfg = config.load()
    if bool(args.app) == bool(args.tool):
        raise ValueError("give --app NAME (an app profile, see `macmesh apps`) or --tool NAME -- <its arguments>")
    if args.app:
        if args.template:
            raise ValueError("--app takes its commands from the app's profile; drop the arguments after --")
        job = PLANNERS["app"](args.app, args.project, int(start), int(end), chunk=args.chunk, fps=args.fps,
                              vars=_kv(args.var, "--var"), output=args.output or "", workdir=args.workdir or "",
                              cfg=cfg)
    else:
        job = PLANNERS["range"](args.tool, args.project, int(start), int(end), _template(args.template),
                                chunk=args.chunk, out_ext=args.out_ext, merge=args.merge,
                                output=args.output or "", extra_inputs=_kv(args.input),
                                workdir=args.workdir or "", fps=args.fps, vars=_kv(args.var, "--var"))
    return execute(job, cfg, args, kind="app")


def cmd_apps(args) -> int:
    cfg = config.load()
    found = profiles.load()
    if not found:
        print("No app profiles. Add one to ~/.macmesh/apps/ (see macmesh/profiles.py).")
        return 0
    here, fps = toolinfo.programs(cfg), toolinfo.fingerprints(cfg)
    print(f"{'APP':<14} {'ON THIS MAC':<14} {'BUILD':<26} DESCRIPTION")
    for name, p in sorted(found.items()):
        status = "yes" if name in here else "not found"
        build = toolinfo.describe(fps[name]) if name in fps else "-"
        print(f"{name:<14} {status:<14} {build[:26]:<26} {p.get('description', '')}")
        if args.verbose:
            print(f"  profile: {p['source']}\n  program: {here.get(name) or p['program']['path']}")
            for i, step in enumerate((p.get("range") or {}).get("steps", []), 1):
                print(f"  step {i}: {name} {' '.join(step)}")
    return 0


def cmd_batch(args) -> int:
    job = PLANNERS["batch"](args.files, _template(args.template), args.out_ext, name=args.name,
                            workdir=args.workdir or "", extra_inputs=_kv(args.input))
    return execute(job, config.load(), args, kind="batch")


def _template(t: list[str]) -> list[str]:
    if not t:
        raise SystemExit("missing command template after --")
    return t


def cmd_plan(args) -> int:
    from .planners.ai import plan_to_job, plan_with_agent
    cfg = config.load()
    clients = gather_nodes(cfg, args)
    if not clients:
        print("No worker nodes reachable; the planner needs to know what Macs exist.")
        return 1
    log(f"asking {args.agent or cfg['agent']} to plan: {args.request!r}")
    try:
        plan = plan_with_agent(args.request, args.files, [c.info for c in clients], cfg, args.agent)
    except (OSError, ValueError, RuntimeError, TimeoutError) as e:
        log(f"planning failed: {e}")
        return 1
    print(f"\nPlanner: {plan['planner']}\nWhy: {plan.get('explanation', '')}")
    print("Params:", json.dumps(plan["params"], indent=2))
    try:
        job = plan_to_job(plan, args.files, cfg)
    except (ValueError, KeyError, OSError, RuntimeError) as e:
        log(f"plan rejected: {e}")
        return 1
    return execute(job, cfg, args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="macmesh", description="AirDrop for processing power.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def mesh_opts(p, run=True):
        p.add_argument("--peer", action="append", help="worker host:port (repeatable); skips nothing else")
        p.add_argument("--no-discovery", action="store_true", help="don't browse Bonjour")
        p.add_argument("--discover-seconds", type=float, default=2.0)
        if run:
            p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
            p.add_argument("--dry-run", action="store_true", help="plan only, don't run")
            p.add_argument("--save-job", help="write the job spec to this file")
            p.add_argument("--retries", type=int, default=3, help="attempts per task before failing")
            p.add_argument("--no-speculate", action="store_true", help="don't duplicate straggler chunks")
            p.add_argument("--workdir", help="where results are collected")
            p.add_argument("--match", default="version", choices=["version", "hash", "none"],
                           help="only use Macs whose program build matches this Mac's (default: same version, "
                                "or same binary when there's no version)")
            p.add_argument("--keep-blobs", action="store_true",
                           help="leave uploaded inputs on the other Macs for the next job (default: delete)")

    def node_opts(p):
        p.add_argument("--name")
        p.add_argument("--port", type=int)
        p.add_argument("--slots", type=int, help="tasks to run at once (default 1)")
        p.add_argument("--force", action="store_true")

    p = sub.add_parser("init", help="start a new mesh on this Mac (it holds the certificate authority)")
    node_opts(p)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("invite", help="let one more Mac join (shows a one-time code)")
    p.add_argument("--port", type=int, help="pairing port (default: worker port + 1)")
    p.set_defaults(fn=cmd_invite)

    p = sub.add_parser("join", help="join a mesh: ask to be let in (--request), or with a code from `macmesh invite`")
    p.add_argument("code", nargs="?")
    p.add_argument("--request", action="store_true", help="ask the first Mac to let this one in (approve it there)")
    p.add_argument("--host", help="the first Mac's address[:port] (comma-separated to try several)")
    p.add_argument("--json", action="store_true", help="print progress as JSON lines (for the app)")
    node_opts(p)
    p.set_defaults(fn=cmd_join)

    p = sub.add_parser("nearby", help="meshes on this network that can take this Mac")
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_nearby)

    p = sub.add_parser("leave", help="leave the mesh (forget its certificate and token)")
    p.set_defaults(fn=cmd_leave)

    p = sub.add_parser("revoke", help="refuse a Mac's certificate from now on (run on every Mac)")
    p.add_argument("fingerprint")
    p.set_defaults(fn=cmd_revoke)

    p = sub.add_parser("serve", help="run the worker daemon on this Mac")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--no-advertise", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("nodes", help="list Macs on the mesh")
    mesh_opts(p, run=False)
    p.set_defaults(fn=cmd_nodes)

    p = sub.add_parser("config", help="show or set config values (JSON)")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("run", help="run a job spec file")
    p.add_argument("job")
    mesh_opts(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("video", help="encode/filter a video across Macs")
    p.add_argument("src")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--preset", default="h264", choices=sorted(ENCODE_PRESETS))
    p.add_argument("--vargs", help="raw ffmpeg video args (overrides --preset)")
    p.add_argument("--vf", help="ffmpeg video filter, e.g. scale=3840:-2")
    p.add_argument("--chunk", type=float, default=20.0, help="seconds per chunk")
    mesh_opts(p)
    p.set_defaults(fn=cmd_video)

    p = sub.add_parser("range", help="render a frame range with an app (profile) or any command-line renderer",
                       usage="macmesh range --app APP --project P --frames A-B [options]\n"
                             "       macmesh range --tool T --project P --frames A-B [options] -- <tool args>")
    p.add_argument("--app", help="an app profile (see `macmesh apps`)")
    p.add_argument("--tool", help="any allowed program, with its arguments after --")
    p.add_argument("--project", required=True)
    p.add_argument("--frames", required=True, help="START-END, inclusive")
    p.add_argument("--fps", type=float, help="frame rate, for commands that take seconds (else asked of the app)")
    p.add_argument("--var", action="append", help="value for a {name} placeholder, e.g. --var comp=Main")
    p.add_argument("--chunk", type=int, default=100, help="frames per task")
    p.add_argument("--out-ext", default="", help=".mov for movie chunks; omit for frame folders")
    p.add_argument("--merge", default="auto", choices=["auto", "concat", "dirs", "none"])
    p.add_argument("--output", help="final file/folder name inside the workdir")
    p.add_argument("--input", action="append", help="extra asset name=path, use as {in:name}")
    mesh_opts(p)
    p.set_defaults(fn=cmd_range)

    p = sub.add_parser("mcp", help="MCP server for AI agents (stdio), talking to this Mac's running daemon")
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("agents", help="set up /macmesh in Claude Code and Antigravity (skill + MCP server)")
    p.set_defaults(fn=cmd_agents)

    p = sub.add_parser("apps", help="list app profiles and whether this Mac has each app")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_apps)

    p = sub.add_parser("batch", help="one task per file",
                       usage="macmesh batch FILES... --out-ext .EXT [options] -- <program> <args>")
    p.add_argument("files", nargs="+")
    p.add_argument("--out-ext", required=True)
    p.add_argument("--name", default="batch")
    p.add_argument("--input", action="append", help="shared asset name=path, use as {in:name}")
    mesh_opts(p)
    p.set_defaults(fn=cmd_batch)

    p = sub.add_parser("plan", help="let Claude Code / Antigravity plan the split")
    p.add_argument("request", help='what you want, e.g. "export this at 4K HEVC"')
    p.add_argument("files", nargs="*")
    p.add_argument("--agent", choices=["auto", "claude", "antigravity"], help="override config 'agent'")
    mesh_opts(p)
    p.set_defaults(fn=cmd_plan)
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    template: list[str] = []
    if "--" in argv:  # everything after -- is the command template for range/batch
        i = argv.index("--")
        argv, template = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    args.template = template
    try:
        return args.fn(args) or 0
    except (jobspec.JobError, ValueError, FileNotFoundError, pki.PKIError, pairing.PairingError,
            profiles.ProfileError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
