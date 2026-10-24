# macmesh — AirDrop for processing power

Split video exports, motion-graphics renders, AI processing and batch jobs
across the Macs around you, over Wi-Fi, with zero configuration.

```
            macmesh video interview.mov -o final.mp4 --preset hevc
                                   │
                ┌──────────────────┴──────────────────┐
                │ Coordinator (the Mac you typed on)  │
                │  plan → split at keyframes → queue  │
                └──────┬───────────────────────┬──────┘
         Bonjour-discovered, signed HTTP over Wi-Fi
                       ▼                       ▼
            ┌────────────────────┐  ┌────────────────────┐
            │ MacBook Pro        │  │ MacBook Air        │
            │ pulls chunk, runs  │  │ pulls chunk, runs  │
            │ ffmpeg / your app  │  │ ffmpeg / your app  │
            └─────────┬──────────┘  └─────────┬──────────┘
                      └───────────┬───────────┘
                  results back → lossless stitch → final.mp4
```

## What's in this prototype

| Piece | What it does |
|---|---|
| `macmesh serve` | Worker daemon, run on every Mac. Advertises itself over Bonjour (`_macmesh._tcp`), accepts signed tasks over mutual TLS, runs them sandboxed at low priority, steps aside when the Mac's owner needs it, keeps the Mac awake with `caffeinate` while working. |
| `macmesh init` / `invite` / `join` | Start a mesh (this Mac holds its certificate authority), then add Macs with a one-time code, like pairing a Bluetooth device. |
| Discovery | Zero-config over mDNS, the same mechanism AirDrop and AirPlay use for discovery. Static `--peer host:port` works too. |
| Scheduler | **Pull-based**: each Mac takes the next chunk when it has room, so faster Macs naturally do more. Re-queues work when a Mac disappears, retries failures on a *different* Mac, and duplicates straggling chunks at the end of a job (first finisher wins). |
| `macmesh video` | Splits a video at keyframes (lossless), encodes chunks across Macs, stitches with `-c copy`, then adds the original audio back in one pass. Frame-exact: tested to match the source's frame count with no seam errors. |
| `macmesh range` | Frame-range rendering for **FilmCraft / EffectCraft / Grade** (or any CLI renderer). Project bundles are uploaded once per Mac. |
| `macmesh batch` | One task per file: transcode, transcribe, upscale, mask, convert. |
| `macmesh plan` | **AI planner** via **Claude Code CLI** or **Antigravity CLI**, headless, no API keys. It reads the request, file metadata, Mac specs and your apps' CLI docs, and picks how to split the work. |
| `macmesh run` | Runs a raw job spec (JSON). This is what every planner produces; see `macmesh/jobspec.py`. |

## Quick start (two Macs)

On **both** Macs (Python 3.9+, `brew install ffmpeg`):

```sh
git clone <this repo> && cd macmesh
python3 -m pip install --user -e .
```

On the **first** Mac:

```sh
macmesh init --slots 2        # starts a mesh; this Mac holds its certificate authority
macmesh invite                # shows a one-time code such as 7K2Q-M9XD-4TRA and waits
```

On the **second** Mac:

```sh
macmesh join 7K2Q-M9XD-4TRA   # finds the inviting Mac over Bonjour (or add --host <address>)
macmesh serve
```

Then run `macmesh serve` on the first Mac too. Repeat `invite` / `join` for each extra Mac.

From either Mac (in another terminal):

```sh
macmesh nodes                                   # both Macs should appear
macmesh video clip.mov -o clip-hevc.mp4 --preset hevc
```

To start the worker automatically at login, see `examples/com.macmesh.worker.plist`.

## Hooking up FilmCraft, EffectCraft and Grade

Workers only execute programs that are explicitly allowed: entries in
`allowed_programs` (default: `ffmpeg`, `ffprobe`) or the `tools` table.
Point `tools` at your apps' real CLI binaries **on every Mac**. The
`description` and `usage` fields are what the AI planner reads, so make them
accurate:

```sh
macmesh config tools '{
  "effectcraft": {"path": "/Applications/EffectCraft.app/Contents/MacOS/effectcraft",
                  "description": "Motion graphics renderer",
                  "usage": "effectcraft render <project> --comp <name> --start <f> --end <f> --output <dir|file.mov>"},
  "grade":       {"path": "/Applications/Grade.app/Contents/MacOS/grade",
                  "description": "Colour grading + AI subject masking",
                  "usage": "grade export <project> --start <f> --end <f> --output <file.mov>"},
  "filmcraft":   {"path": "/Applications/FilmCraft.app/Contents/MacOS/filmcraft",
                  "description": "Editor timeline export",
                  "usage": "filmcraft render <project> --in <f> --out <f> --output <file.mov>"}
}'
```

Then render a frame range. Everything after `--` is the app's own arguments,
with placeholders:

```sh
# 1200 frames as PNG folders, 100 frames per chunk, pooled into ./macmesh-out/…/frames
macmesh range --tool effectcraft --project MyAd.ecproj --frames 0-1199 --chunk 100 \
  -- render {in:project} --comp Main --start {start} --end {end} --output {out:result}

# Movie chunks, joined losslessly at the end
macmesh range --tool grade --project Interview.grade --frames 0-4799 --chunk 400 --out-ext .mov \
  -- export {in:project} --start {start} --end {end} --output {out:result}
```

| Placeholder | Becomes |
|---|---|
| `{in:project}`, `{in:NAME}` | Path of that input on the Mac running the task (bundles/folders work) |
| `{out:result}`, `{out:NAME}` | Where the task must write its output (a file, or a folder of frames) |
| `{start}` `{end}` `{count}` `{index}` | This chunk's frame range (inclusive) |
| `{stem}` (batch only) | Input file name without its extension |

For a movie-chunk merge to be lossless, every chunk must start on a keyframe
and use identical codec settings. Intra-frame codecs such as ProRes always
qualify. For long-GOP codecs, your app must start each chunk with a keyframe.

## The AI planner (Claude Code or Antigravity, no API)

```sh
macmesh config agent claude          # or: antigravity
macmesh plan "Render the Main comp, frames 0-899, as PNGs" MyAd.ecproj
macmesh plan "Upscale to 4K and export HEVC for YouTube" interview.mov --agent antigravity
```

How it works:

1. macmesh builds a prompt containing the request, file metadata (`ffprobe` for
   media, listings for bundles), every Mac's specs (chip, cores, fanless,
   battery, installed apps), and your apps' `usage` docs.
2. It runs the agent headlessly:
   - `claude -p … --output-format json --tools "" --no-session-persistence`
     (all tools disabled, so it can only answer)
   - `agy -p …`
   Both commands are editable under `agents` in the config.
3. The agent answers with JSON choosing a **deterministic planner**
   (`video` / `range` / `batch`) and its parameters, or a raw job spec for
   unusual work. The AI decides *how to split*; plain code does the scheduling
   and execution.
4. macmesh validates the plan, refuses inputs outside the files you named,
   shows you every command, and asks before running.

On the first run, use `--dry-run --save-job plan.json` to review a plan and
`macmesh run plan.json` to execute it later.

> **Antigravity note:** some `agy` versions reportedly hang in `-p` mode when
> stdout isn't a terminal. If `macmesh plan --agent antigravity` times out, run
> it with `MACMESH_AGENT_TTY=1`, which wraps the call in `script` to give it a
> pseudo-TTY. Check `agy --help` on your version for its headless/approval flags.

## How the scheduling handles real Macs

- **Fanless MacBook Air:** no fixed 40/60 split. The Air pulls chunks
  when free, so a throttling Air simply returns less often. Measured speed
  (cost units per second) replaces the initial hardware estimate after the
  first chunk.
- **Big vs small chunks:** the strongest Macs take the largest remaining
  chunks, and weaker ones take the smallest.
- **Upload overlap:** each Mac is sent one extra chunk, so its next
  upload happens while it computes the current one.
- **Lid closed / Wi-Fi dropped:** status calls time out after 2 s, and a Mac
  that misses two in a row has its chunk re-queued (about 4 s). A transfer
  that stalls for 10 s counts as dead too. The Mac rejoins automatically when
  it comes back.
- **Shutdown / logout:** the worker catches SIGTERM and hands its chunks back
  at once, so nothing waits for a timeout.
- **The owner needs their Mac (owner first):** each worker watches CPU use by
  everything except macmesh's own tasks, total GPU use while someone is at the
  keyboard, and keyboard/mouse idle time. When the owner is busy (by default:
  over 50% CPU or 60% GPU) it stops taking work and **pauses** running tasks
  (SIGSTOP). If the owner is calm again for 10 s, the tasks resume. If not,
  after 30 s the tasks are **handed back** and run on another Mac. With
  `macmesh config owner '{"mode": "idle"}'` a Mac only works while nobody has
  touched it for 2 minutes. Tasks also run under a macOS QoS clamp
  (`priority`, default `utility`), so macOS favours the owner's apps anyway.
- **Different software on the other Mac:** a chunk only goes to Macs that have
  the program *and* the same build of it as the Mac you typed on: the same
  version string when the tool reports one (`version_args` in its `tools`
  entry; ffmpeg's is built in), otherwise the same binary (SHA-256).
  `--match hash` always compares binaries; `--match none` turns the check off.
  A Mac without the program is skipped, and the job stops with a clear message
  if no Mac can run a task.
- **Stragglers:** once the queue is empty, an idle Mac starts a duplicate
  of any chunk that is badly overdue. The first copy to finish wins and
  the other is cancelled.
- **Battery and disk:** a Mac on battery below `min_battery` (default 30%), or
  with less than `min_free_gb` (default 5) of free disk, stops accepting new
  chunks (`macmesh config accept_on_battery false` to refuse all battery work).
- **Staying awake:** tasks run under `caffeinate -i`, so an *open* Mac
  won't idle-sleep. Closing a MacBook's lid still sleeps it unless it's on power
  with an external display (clamshell mode). macmesh treats that as a dropped
  node and re-queues its work.

## Security model

- **Encryption and identity:** all traffic is mutual TLS (1.2+, P-256 keys).
  Each Mac has its own key, which never leaves it, and a certificate signed by
  the mesh's CA on the first Mac. Workers and coordinators accept only
  certificates from that CA. `macmesh revoke <fingerprint>` (fingerprints are
  listed by `macmesh nodes`) cuts one Mac off; run it on every Mac.
- **Pairing:** `join` sends its signing request over TLS. Both directions
  carry an HMAC keyed by the one-time code over the inviter's certificate
  fingerprint, so a machine in the middle can't pair or impersonate without
  the code. The code is 60 random bits, single-use, expires in 10 minutes and
  allows 3 wrong tries.
- Every request is also HMAC-signed with the mesh token, with a timestamp and
  body hash.
- **Protecting the Mac that lends its power:** workers run only allowlisted
  programs, as argument lists with no shell, inside a macOS sandbox
  (`sandbox-exec`). A sandboxed task:
  - has no IP networking;
  - can write only to its own task folder;
  - can't read or even list the owner's home folder, apart from its task
    folder, the tool itself, `~/Library/Fonts` and paths the tool's config
    allows (`"sandbox": {"read": [...], "write": [...]}`, or `false` for a tool
    that can't work inside it).
- **Protecting the Mac that sends work:** task outputs can't escape the
  coordinator's `workdir`, archives are extracted with path-traversal checks,
  and downloads are verified against the worker's SHA-256.
- **Your media on other Macs:** uploads are content-addressed and re-hashed on
  arrival. After each job, the coordinator deletes what it uploaded
  (`--keep-blobs` to keep it for the next job), and workers delete anything
  unused for `blob_ttl` (default 30 minutes). Task folders are deleted as soon
  as results are fetched.
- **Still on you:** keep FileVault on, because cached media is not separately
  encrypted on disk. Tasks run as your user, not a separate low-privilege
  account. There are no memory limits per task. Don't add `sh`/`python3` to
  `allowed_programs` unless you mean it: the sandbox limits a shell, but an
  allowlisted interpreter can still run anything inside it.

## Testing

```sh
python3 -m unittest discover -s tests -v
```

The suite starts in-process workers to stand in for Macs. It covers:
- the frame-exact video stitch, frame-range renders with a fake app, and batch jobs;
- a node dying mid-job, a node going silent (detected within seconds), shutdown handing work back, and a busy owner getting their Mac back (pause, resume, yield);
- build matching, and blob cleanup and expiry;
- pairing (and a wrong code), mutual TLS jobs, and refusal of plain HTTP, foreign CAs and revoked certificates;
- the sandbox (no home reads, no writes outside, no network);
- permanent failure after retries, signature tampering, the program allowlist, and tar traversal.

## Known limitations / roadmap

1. **Same assets everywhere.** Project files and bundles travel with each
   task, but fonts, plugins, LUTs and app versions must match on every Mac,
   or different Macs render different pixels. Next step: workers report
   app version and font/plugin hashes, and the planner refuses mismatched
   nodes.
2. **Media-engine encoders** (`h264-hw`, `hevc-hw`) are already very fast on
   Apple Silicon. Splitting pays off most for software encoders (x264/x265),
   heavy filters, motion-graphics renders and AI passes.
3. **Menu bar app:** a Swift app (`Network.framework` with
   `includePeerToPeer = true`) would give AirDrop-style peer-to-peer Wi-Fi
   (AWDL) without a shared router, plus drag-and-drop and per-Mac progress
   bars. It could also catch sleep notifications, so a Mac about to sleep
   hands work back the way shutdown already does; today sleep is noticed by
   the 4 s heartbeat. This Python core would become its engine (or be ported).
4. **Real thermal state:** read `ProcessInfo.thermalState` (via the Swift
   app or PyObjC) so a throttling Air gets smaller chunks proactively instead
   of reactively.
5. **LLM inference across Macs** (one model split over two machines) is a
   different problem. Use `exo` or MLX distributed for that. macmesh covers
   *many independent jobs*, such as per-file transcription or per-range
   masking.
6. **Prior art to beat:** Apple Compressor already distributes encodes
   across Macs ("shared computers"), and DaVinci Resolve Studio has remote
   rendering. macmesh's differences are AI-planned splitting, your own apps,
   and arbitrary batch work.
