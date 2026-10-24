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
| `macmesh serve` | Worker daemon, run on every Mac. Advertises itself over Bonjour (`_macmesh._tcp`), accepts signed tasks, keeps the Mac awake with `caffeinate` while working. |
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
macmesh init --slots 2        # prints a join command with a shared token
macmesh serve
```

On the **second** Mac, paste the join command, then serve:

```sh
macmesh init --token <token-from-first-mac>
macmesh serve
```

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
- **Lid closed / Wi-Fi dropped:** the Mac's chunks are re-queued to the
  others within about 10 s. It rejoins automatically when it comes back.
- **Stragglers:** once the queue is empty, an idle Mac starts a duplicate
  of any chunk that is badly overdue. The first copy to finish wins and
  the other is cancelled.
- **Battery:** a Mac on battery below `min_battery` (default 30%) stops
  accepting new chunks (`macmesh config accept_on_battery false` to refuse
  all battery work).
- **Staying awake:** tasks run under `caffeinate -i`, so an *open* Mac
  won't idle-sleep. Closing a MacBook's lid still sleeps it unless it's on power
  with an external display (clamshell mode). macmesh treats that as a dropped
  node and re-queues its work.

## Security model

- Every request is HMAC-signed with the mesh token, with a timestamp and
  body hash. The token itself never goes over the network.
- Workers run only allowlisted programs, as argument lists with no shell, in a
  scratch folder. Task outputs can't escape the coordinator's `workdir`, and
  archives are extracted with path-traversal checks.
- Uploads are content-addressed and re-hashed on arrival. Downloads are
  verified against the worker's SHA-256.
- **Not yet:** encryption. Traffic is plain HTTP on your LAN, so someone on the
  same Wi-Fi could *see* your media while it's transferred. Don't use it on
  untrusted networks until TLS is added (roadmap below). Anyone with the token can
  run allowlisted programs on your Macs, so treat it like a password, and don't
  add `sh`/`python3` to `allowed_programs` unless you mean it.

## Testing

```sh
python3 -m unittest discover -s tests -v
```

The suite starts two in-process workers to stand in for two Macs. It covers
the frame-exact video stitch, frame-range renders with a fake app, batch jobs,
a node dying mid-job, permanent failure after retries, signature tampering,
the program allowlist, and tar traversal.

## Known limitations / roadmap

1. **Same assets everywhere.** Project files and bundles travel with each
   task, but fonts, plugins, LUTs and app versions must match on every Mac,
   or different Macs render different pixels. Next step: workers report
   app version and font/plugin hashes, and the planner refuses mismatched
   nodes.
2. **Media-engine encoders** (`h264-hw`, `hevc-hw`) are already very fast on
   Apple Silicon. Splitting pays off most for software encoders (x264/x265),
   heavy filters, motion-graphics renders and AI passes.
3. **TLS + pairing UI:** a menu bar app in Swift (`Network.framework` with
   `includePeerToPeer = true`) would give encrypted transport and AirDrop-style
   peer-to-peer Wi-Fi (AWDL) without a shared router, plus drag-and-drop and
   per-Mac progress bars. This Python core would become its engine (or be
   ported).
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
