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

## Install

On each Mac (Apple silicon, macOS 14 or later), in Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/ash0503gh/macmesh/main/install.sh | sh
```

It downloads the latest release, checks it against its SHA-256, installs `macmesh.app` in
Applications and opens it. Then:

1. **On the first Mac:** choose *Start a mesh*. To add another Mac, go to *Macs*, then *Add a Mac*,
   and a one-time code appears.
2. **On every other Mac:** choose *Join a mesh* and enter that code.

To download by hand instead, get `macmesh-macos-arm64.zip` from
[Releases](https://github.com/ash0503gh/macmesh/releases/latest) and unzip it into Applications.
The app isn't notarized by Apple, so the first time you open it, Control-click it, choose
*Open*, then confirm *Open*. (Or allow it in System Settings, then Privacy & Security.)

What each Mac needs for the work it shares:

- **ffmpeg** for video jobs: `brew install ffmpeg`.
- **The apps' command-line tools** for app jobs, at the same version on every Mac. The FilmCraft and
  EffectCraft profiles expect `filmcraft-cli` and `effectcraft-cli` in `~/.local/bin`. The Grade profile
  expects Grade with `grade-cli` in `~/Applications/Grade.app` (Grade's `make install`). Each Mac's
  *Apps* page shows what it has.

## The app

`app/` is a native macOS app (SwiftUI) on top of the engine:

- **Dashboard window:** the sidebar has five pages.
  - **Jobs:** the running job, with progress, time left, how much faster it is than one Mac, and a map of which Mac did which chunk. Below it, the job history with *Show in Finder*.
  - **Macs:** every Mac on the mesh, with chip, cores, memory, power and what it's doing. *Add a Mac* shows a pairing code.
  - **Apps:** the app profiles, and whether this Mac has each app.
  - **Lending:** lend this Mac or not, *step aside when I need it* or *only while I'm away*, and open at login.
  - **Security:** encryption, this Mac's certificate, and removing a Mac.
- **New job:** a sheet that opens from the toolbar, or when you drop a file on the window or open one with macmesh. macmesh recognises the project, its app and its length, asks for anything missing (an EffectCraft composition, say), lets you pick the Macs, and starts the job. Results land in `~/Movies/macmesh`.
- **Menu bar icon:** what's running, *Lend this Mac*, and *Open macmesh*. It keeps lending going with the window closed.
- **First run:** *Start a mesh* or *Join a mesh* with a code.

The app carries its own Python and engine, so a Mac only needs the app. Build it with:

```sh
app/scripts/build-app.sh      # → app/build/macmesh.app (fetches a pinned, checksummed Python the first time)
app/scripts/package.sh        # → app/build/macmesh-macos-arm64.zip and .sha256, for a GitHub release
```

`Macmesh --snapshot DIR [--dark]` renders every screen with sample data to PNGs, and
`Macmesh --check` reads a running engine the way the app does.

Under the hood, `macmesh serve` listens on `~/.macmesh/control.sock` (only your user can open it). Jobs started there run inside the daemon, so the app, agents and the CLI all see the same jobs (`macmesh/control.py`).

## For AI agents (MCP)

`macmesh mcp` is an MCP server (stdio) with these tools: `get_status`, `list_apps`, `inspect_file`, `start_job`, `get_job`, `list_jobs`, `cancel_job`, `set_lending` and `create_invite`. It talks to the running engine, so jobs an agent starts show up in the app. For Claude Code:

```sh
claude mcp add macmesh -- /Applications/macmesh.app/Contents/Resources/engine/bin/macmesh mcp
```

## What's in this prototype

| Piece | What it does |
|---|---|
| `macmesh serve` | Worker daemon, run on every Mac. Advertises itself over Bonjour (`_macmesh._tcp`), accepts signed tasks over mutual TLS, runs them sandboxed at low priority, steps aside when the Mac's owner needs it, keeps the Mac awake with `caffeinate` while working. |
| `macmesh init` / `invite` / `join` | Start a mesh (this Mac holds its certificate authority), then add Macs with a one-time code, like pairing a Bluetooth device. |
| Discovery | Zero-config over mDNS, the same mechanism AirDrop and AirPlay use for discovery. Static `--peer host:port` works too. |
| Scheduler | **Pull-based**: each Mac takes the next chunk when it has room, so faster Macs naturally do more. Re-queues work when a Mac disappears, retries failures on a *different* Mac, and duplicates straggling chunks at the end of a job (first finisher wins). |
| `macmesh video` | Splits a video at keyframes (lossless), encodes chunks across Macs, stitches with `-c copy`, then adds the original audio back in one pass. Frame-exact: tested to match the source's frame count with no seam errors. |
| `macmesh range` | Frame-range rendering with any app that has a command line: `--app` uses an [app profile](#any-app-profiles), `--tool` takes the tool's arguments. Projects and the media they use are uploaded once per Mac. |
| `macmesh apps` | Lists app profiles, and which apps (and builds) this Mac has. |
| `macmesh batch` | One task per file: transcode, transcribe, upscale, mask, convert. |
| `macmesh plan` | **AI planner** via **Claude Code CLI** or **Antigravity CLI**, headless, no API keys. It reads the request, file metadata, Mac specs and the app profiles, and picks how to split the work. |
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

To start the worker automatically at login, use the app's *Open macmesh at login*, or see
`examples/com.macmesh.worker.plist` for a command-line-only setup.

## Any app: profiles

macmesh's core knows nothing about particular apps. It runs commands on files
and returns files. What it knows about an app lives in an **app profile**: one
JSON file that says how to drive the app's command-line tool. Shipped
examples are in `macmesh/apps/` (FilmCraft, EffectCraft, Grade). Yours go in
`~/.macmesh/apps/`, and a file there replaces a shipped one with the same
name. The format is documented at the top of `macmesh/profiles.py`.

```sh
macmesh apps                     # profiles, and whether this Mac has each app (and which build)
macmesh range --app filmcraft --project Edit.fcproj --frames 0-4799 --chunk 400
macmesh range --app effectcraft --project MyAd.ecproj --frames 0-899 --var comp=Main
macmesh range --app grade --project Interview.gradeproject --frames 0-2999
```

A profile gives:

- **The program:** its path, and the flag that prints its version. Version
  strings are compared across Macs; with no version, the binary itself is.
- **The range steps:** one or more commands run in order in the task's folder,
  usually "relink the project's media to the folder it arrived in", then
  "render this range".
- **Media:** with `"media": "scan"`, macmesh finds the files the project
  refers to (any JSON project: absolute paths and `file://` URLs) and sends
  them with it. On the other Mac they arrive beside the project, in `{indir}`.
- **Failure markers** (`fail_if`): regular expressions for output that means
  the render failed even though the app exited 0. Some apps render grey
  placeholders for missing media.
- **Sandbox needs:** extra folders the app must read or write.
- **The frame rate,** for apps that take seconds instead of frames
  (`fps_probe`, or `--fps`).

Chunks are ProRes `.mov` files with PCM sound, so they join back losslessly:
pictures and sound come out identical to a render on one Mac. That was tested
for all three example profiles, sandboxed, at 24 and 29.97 fps.

| Placeholder | Becomes |
|---|---|
| `{in:project}`, `{in:NAME}` | Path of that input on the Mac running the task (bundles/folders work) |
| `{out:result}`, `{out:NAME}` | Where the task must write its output (a file, or a folder of frames) |
| `{indir}` | The folder holding the project and its media on that Mac |
| `{start}` `{end}` | This chunk's frames, both inclusive; `{end_excl}` = end + 1; `{count}` `{index}` |
| `{start_s}` `{end_s}` `{fps}` | The same range in seconds (end exclusive). NTSC rates such as 29.97 are exact (30000/1001) |
| `{name}` | A `--var name=value` (or the profile's default) |
| `{stem}` (batch only) | Input file name without its extension |

An app with no profile still works through `--tool` and its own arguments:

```sh
macmesh range --tool myrenderer --project Scene.blend --frames 1-250 --chunk 25 \
  -- -b {in:project} -s {start} -e {end} -o {out:result}
```

**What makes an app offloadable:** a command that renders part of the work
headless from files: a frame range, a file, a batch. GUI-only apps can't be
offloaded, and a running process can't move to another Mac. That's why Grade
gained `grade-cli export` (frame range, relink, refuses offline media). The
example profiles show what each app needed:

- **FilmCraft:** its `exec` export renders placeholders for offline media and
  still exits 0, which the profile's failure marker catches.
- **EffectCraft:** takes seconds, relinks footage in the background unless
  asked to wait, and renders missing footage silently. Its profile relinks,
  then checks for missing footage **and fonts** before rendering.

## AI (Claude Code or Antigravity, no API keys)

**What the AI doesn't do:** splitting a job, deciding which Mac takes which chunk, and joining the
pieces are fixed rules. They're instant and exact, and they adapt to each Mac's measured speed (see
the next section). The AI works on the parts that have no rule yet:

| | In the app | On the command line |
|---|---|---|
| **Plan a free-form request** ("export at 4K, then a 720p preview") | New job > *Describe it* | `macmesh plan "…" files…` |
| **Teach macmesh a new app** from its `--help` | Apps > *Teach an app* | control method `teach` |
| **Explain a failed job** in plain words | Jobs > History > *Explain* | control method `explain` |

- **Which AI:** `agent` is `auto` (Claude Code if it's installed, else Antigravity), `claude` or
  `antigravity` (the app's *AI* page, or `macmesh config agent …`). It runs each one's command-line
  tool with your existing subscription. Claude Code runs with its tools turned off
  (`claude -p … --tools ""`). Antigravity runs read-only (`agy -p … --mode plan --sandbox`). Both run
  from an empty folder and are told to answer from the prompt alone. The AI only answers; macmesh
  checks the answer before anything runs.
- **What it sees:** your request, the names, sizes and formats of the files you add, your Macs'
  specs, the app profiles, and the text of project files (capped at 200,000 characters). Media is
  never sent.
- **Plans:** the AI chooses one of the same planners people use (`app`, `video`, `range`, `batch`)
  or writes a raw job. macmesh refuses inputs outside the files you named and the media they use.
  A plan that only runs app profiles or ffmpeg is **safe** and starts at once (`ai_autorun`, on by
  default). Anything else, including a merge command that would run unsandboxed on this Mac, waits
  for your OK and shows why.
- **Teaching an app:** give the tool, a name and a sample project.
  1. The AI reads the tool's help (which runs in the sandbox) and may ask for up to six more help
     pages.
  2. It writes a profile.
  3. macmesh renders 12 frames of the sample twice, once whole and once in chunks, and keeps the
     profile only if they match frame for frame.
  4. If they don't, the AI gets the failure and tries again, up to three times.

  The profile is saved in `~/.macmesh/apps/`; copy it to your other Macs.
- **Explaining:** the job's error, its last log lines, what was asked and the app's profile go to
  the AI, which answers with what went wrong, what to do, and where to look.

> **Signing in:** the AI tools use their own logins. If one says it isn't signed in, run `claude`
> (or `agy`) in Terminal once and sign in. If Antigravity hangs with no terminal, set
> `MACMESH_AGENT_TTY=1`.

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
    folder, the tool's own folder or `.app` bundle, `~/Library/Fonts` and
    paths the tool's profile allows (`"sandbox": {"read": [...], "write":
    [...]}`, or `false` for a tool that can't work inside it);
  - can't read the owner's private temp and cache folders under
    `/var/folders`, where other apps' temp files live. Only the metadata of
    the folders above its task folder is visible.
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
- the sandbox (no home reads, no private temp reads, no writes outside, no network);
- multi-step tasks, failure markers, exact seconds at NTSC rates, media scanning, app profiles (including a job whose media must be shipped and relinked), and dropping v0.1's GUI-app config entries;
- the control socket and the MCP server (jobs, cancelling, lending);
- the AI, with a fake agent standing in for Claude Code: safe plans running at once, unsafe ones waiting for an OK, plans refused for touching files you didn't name, explanations, and teaching an app whose first profile fails the test render and whose second passes;
- permanent failure after retries, signature tampering, the program allowlist, and tar traversal.

## Known limitations / roadmap

1. **Same assets everywhere.** Projects and their media travel with each
   task, and builds are matched, but fonts, plugins and LUTs must match on
   every Mac, or different Macs render different pixels. (The EffectCraft
   profile refuses to render when fonts are missing; other apps don't check.)
   Next step: workers report font and plugin hashes, and the planner refuses
   mismatched Macs.
2. **Media-engine encoders** (`h264-hw`, `hevc-hw`) are already very fast on
   Apple Silicon. Splitting pays off most for software encoders (x264/x265),
   heavy filters, motion-graphics renders and AI passes.
3. **Peer-to-peer Wi-Fi and sleep notices in the app:** `Network.framework`
   with `includePeerToPeer = true` would give AirDrop-style Wi-Fi (AWDL)
   without a shared router. The app could also catch sleep notifications, so
   a Mac about to sleep hands work back the way shutdown already does; today
   sleep is noticed by the 4 s heartbeat.
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
