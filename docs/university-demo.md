# Demo on another machine, fully offline

This guide sets up ATLAS on a fresh Windows PC (for example at a university) and runs a
**browser test call with an AI voice agent where speech-to-text, the language model and the
voice all run on that PC**. Once it is set up, the demo needs no internet at all.

One command does the setup: `.\make.ps1 demo` (Linux/macOS: `make demo`). If the internet
there is slow or missing, bring an **offline bundle** made on a machine that already runs the
demo (see [Path B](#path-b-little-or-no-internet-there-offline-bundle)).

## Hardware: what to expect

Measured on the development PC: Intel i5-8500T (6 cores, 2018), 12 GB RAM, no GPU, everything
on one machine (Docker, the models, the browser). **A reply starts about 5 to 6 seconds after
you stop talking** with the default setup (Whisper speech-to-text). Each model alone is faster
(see [Running AI models on your own hardware](local-models.md)), but during a call they
compete for the same 6 cores and 12 GB with Docker and the browser.

| | Works (measured) | Recommended |
|---|---|---|
| RAM | 12 GB, with everything else closed | **16 GB or more** |
| Memory Docker may use | 6 GB (tight) | 8 GB or more |
| CPU | 6 cores | **8 or more cores** (recent Core i5/i7, Ryzen 5/7) |
| Disk | SSD, 25 GB free | SSD, 40 GB free |
| OS | Windows 10/11 64-bit with Docker Desktop (WSL 2) | same |

What makes it faster:

- **Live transcription** (the `private_streaming` preset, below) recognises words while you
  speak instead of afterwards. It removes about 2 seconds from every reply on the PC above.
- More cores and more RAM: the models stop waiting for each other.
- For production speed, use a GPU server or hosted providers. Replies on CPU stay in the
  seconds; hosted stacks answer in under a second.

Docker Desktop gives its virtual machine half of the RAM by default. On a 16 GB PC that is
8 GB, which is enough. On 12 GB, raise it: create `%UserProfile%\.wslconfig` with

```ini
[wsl2]
memory=10GB
```

then quit Docker Desktop, run `wsl --shutdown`, and start Docker Desktop again.

## What needs internet, and when

| What | Size | Internet |
|---|---|---|
| Installers: Docker Desktop, Node.js 22 LTS, pnpm 10, uv, Git | about 0.7 GB | once; bring them on a USB stick |
| API Python packages and web app packages | about 0.5 GB | once, **or** from the bundle (`-WithDeps`) |
| Docker images (database, voice service, model servers) | about 5.6 GB | once, **or** from the bundle |
| Models: `qwen2.5:1.5b`, Whisper base, Pocket TTS, live transcription | about 3.2 GB | once, **or** from the bundle |
| Live transcription only: the Deepgram key check | none | once, when that connection is saved |
| **The demo itself: test calls in the browser** | | **never** |

During a call nothing leaves the PC: the browser talks to the voice service on `localhost`
over WebRTC, and the voice service talks to the model servers inside Docker. A WebRTC test
call on `localhost` was tested to work with the network disconnected.

## Path A: internet available (once)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (WSL 2
   backend), [Node.js 22 LTS](https://nodejs.org/), [uv](https://docs.astral.sh/uv/) and
   [Git](https://git-scm.com/). Then, in a new terminal: `npm install -g pnpm@10`.
   Install pnpm 10 itself, not a newer major: the web app pins pnpm 10, and a newer pnpm
   downloads pnpm 10 on first use, which fails without internet.
2. Start Docker Desktop and wait until it shows *Engine running*.
3. Get the code: `git clone <repository URL> atlas`, or unzip a copy. Do **not** copy
   `apps\web\node_modules` or `services\api\.venv` from another PC: they contain absolute
   paths and links that only work on the PC that made them.
4. In PowerShell, in the repository folder:

   ```powershell
   .\make.ps1 demo
   ```

   If PowerShell says running scripts is disabled, use
   `powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\setup_local_demo.ps1`.

   The first run downloads about 10 GB and builds two images; allow 30 to 60 minutes.
   It ends with the address, the sign-in and the next steps. **Write down the password**: it
   is generated on the first run, saved in `.env` as `ATLAS_DEMO_PASSWORD` and shown once.
5. Run it again whenever you like (for example on the morning of the demo). A second run
   keeps `.env`, re-uses the existing connections and agent, and only restarts what it
   started itself.

Add `-DryRun` to see what it would do without changing anything.

## Path B: little or no internet there (offline bundle)

### On a PC that already runs the demo

1. Run `.\make.ps1 demo` once, so every image and model is present.
2. See what the bundle will hold and how big it is (nothing is written):

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\offline_bundle.ps1 export E:\atlas-bundle -WithDeps -DryRun
   ```

3. Write it to a USB disk formatted **exFAT or NTFS** (not FAT32: it cannot hold files
   over 4 GB), about 10 GB:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\offline_bundle.ps1 export E:\atlas-bundle -WithDeps
   ```

   The bundle holds one `docker save` file per image, one file per model volume, the
   committed source code as a zip (uncommitted changes are not in it), `manifest.tsv` with
   the size and SHA-256 of every file, `SHA256SUMS` and a `README.txt`.

   `-WithDeps` adds a Python 3.12 runtime, the API's Python packages and the web app's
   packages (about 0.5 GB), so nothing has to be downloaded on the demo PC. They only work
   on the same kind of PC (Windows, 64-bit Intel/AMD) with the same pnpm major version;
   leave `-WithDeps` out otherwise and allow internet once for `uv sync` and `pnpm install`.
4. Copy the installers (Docker Desktop, Node.js 22, uv; pnpm 10 is installed with `npm`, so
   either have internet for that one command or bring `pnpm` as a standalone executable)
   onto the same disk.

The bash version takes the same arguments: `bash infra/scripts/offline_bundle.sh export
/media/usb/atlas-bundle --with-deps --dry-run`. Bundles made by either script can be
imported by the other.

### On the demo PC

1. Install Docker Desktop, Node.js 22, pnpm 10 and uv; start Docker Desktop.
2. Unzip `source\atlas-source-<commit>.zip` from the bundle, for example to `C:\atlas`, and
   open PowerShell there.
3. Import (checks every file's SHA-256 first, then loads the images and restores the model
   volumes; anything already present is left alone):

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\offline_bundle.ps1 import E:\atlas-bundle
   ```

4. Set up and start: `.\make.ps1 demo`. Without internet it installs the packages from the
   bundle (or from the caches of an earlier online run), uses the imported images instead of
   building them, and finds the models already in their volumes.

### Moving a configured PC as it is (`-WithData`)

`-WithData` also exports the ATLAS database, the voice service's database and storage, and
`.env`. Use it to move a PC that was set up online, for example with the live
transcription connection, to a PC that will stay offline.

> **The bundle then contains every account, password hash, stored API key and secret.**
> Treat it like a password, delete it after the import, and never share it.

```powershell
# on the configured PC (the database containers are stopped briefly for a consistent copy)
powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\offline_bundle.ps1 export E:\atlas-bundle -WithDeps -WithData
# on the demo PC, before the first .\make.ps1 demo
powershell -NoProfile -ExecutionPolicy Bypass -File infra\scripts\offline_bundle.ps1 import E:\atlas-bundle -WithData
.\make.ps1 demo
```

The databases only work together with the `.env` that created them (it holds the key that
encrypts stored secrets), so the import copies the bundle's `.env` as well. If the demo PC
already has a different `.env`, the import stops and asks for `-WithData -Force`, which keeps
a backup of the old one (`.env.backup-<time>`). Volumes that already hold data are only
replaced with `-Force`, and never while a container uses them (run `.\make.ps1 demo-stop`
first).

## Optional: live transcription (needs a Deepgram key once)

The default preset, **Private self-hosted (Pocket TTS)**, uses Whisper for speech-to-text and
works fully offline. The faster **Private streaming (self-hosted)** preset uses the local
live-transcription server. It speaks Deepgram's protocol, so ATLAS stores it as a Deepgram
connection pointing at `http://local-stt:8000`, and the voice service checks the key with
Deepgram's cloud **once, when the connection is saved**. Calls afterwards send audio only to
the local server. A free Deepgram account is enough; the key is never printed.

```powershell
$env:ATLAS_DEMO_DEEPGRAM_KEY = "<your key>"   # or: .\make.ps1 demo -DeepgramKey <key>
.\make.ps1 demo
```

The script saves the connection only when a key is given **and** the internet is reachable.
Otherwise it says so and uses Pocket TTS with Whisper. Once the connection exists, later runs
use the streaming preset without a key or internet. The last lines of the run always say
which preset is active. Force one with `-Preset pocket` or `-Preset streaming`.

To have live transcription at an offline university: run the setup with the key at home,
then either take that PC, or move it with `-WithData` (above).

Live transcription is English only and less accurate than Whisper on accents, names and
numbers (see [local-models.md](local-models.md#live-transcription-streaming)).

## Run the test call

1. Open <http://localhost:5173> in Chrome or Edge on the same PC and sign in as
   `demo@example.com` with the password from the setup.
2. **Agents → Mortgage lead qualifier → Test call → Start test call.** Allow the microphone
   when the browser asks.
3. The agent greets you first. Answer, then pause: it replies a few seconds after you stop.
4. **End call** when done.

Use a headset if you can: with laptop speakers the microphone also hears the agent.

## Read the response times

Every test call is recorded under **Calls**. Open the newest one and look at the **Response
times** card:

| Figure | Meaning |
|---|---|
| Median response | Half of the replies started faster than this, measured from when you stopped talking to when the agent started speaking |
| p95 response | 19 of 20 replies started faster than this |
| Slowest | The longest wait in this call |
| First response | The wait for the first reply. Without a warm model this one is the slowest |
| Interruptions | Times you spoke over the agent |

Colours compare with phone-quality targets: under 0.8 s is good, over 1.5 s feels slow. On
CPU expect several seconds, so most replies show as slow; that is the honest result of running
everything on one PC. **Show each turn** splits every wait into *Heard* (speech recognition
finishing your words), *Thinking* (the language model's first words), *Speaking* (the voice's
first audio) and *Other*. With Whisper, *Heard* is about 2 seconds of each reply; with live
transcription it is a fraction of a second.

## Before the audience arrives

- Plug in the power (laptops slow down on battery) and choose the *Best performance* power
  mode.
- Close other programs and browser tabs.
- Run `.\make.ps1 demo` again, or at least `.\make.ps1 local-ai-warm`, a few minutes before:
  it loads the models into memory.
- Make one test call yourself. The first reply after the model server starts has to read the
  whole agent prompt; ATLAS warms it while the call connects, and afterwards it is cached.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Docker is not running" | Start Docker Desktop, wait for *Engine running*, run the command again. |
| "Port 5173/8080 is used by …" | Another program has the web app's or the API's port. Close it, or run `.\make.ps1 demo -Restart` to stop whatever holds those two ports. |
| "Port 8000 is used by …" (or 5433, 6380, 9000, 11434, 8001–8003) | A Docker container of the demo needs that port. Close the program named in the message (`netstat -ano \| findstr :8000` shows its process id). |
| The browser does not ask for the microphone, or no sound is picked up | Use `http://localhost:5173` (browsers only allow the microphone on `localhost` or https). Allow it in the site settings (padlock icon), and in Windows Settings → Privacy & security → Microphone. |
| The first reply takes much longer | The models were not warm. Run `.\make.ps1 local-ai-warm`, make one throw-away call, then start the real one. |
| Choppy audio, long pauses, cut-off words | The CPU is saturated. Close other programs, plug in the power, use the streaming preset if you can, and check `docker stats`. |
| "… was stopped for lack of memory" | Docker has too little memory: raise it in `.wslconfig` (above) and close programs. |
| "could not download …" or an image build fails without internet | Import the offline bundle, then run the setup again. |
| Sign-in fails after replacing `.env` | The database still has the old demo password. Put the old `.env` back. For a clean start (deletes all demo data): `.\make.ps1 demo-stop`, `docker compose --profile voice rm -sf postgres voice-db`, `docker volume rm atlas_pg_data atlas_voice_db_data`, then `.\make.ps1 demo`. |
| The test call connects but the agent never answers | Check *Voice AI → AI providers → Test providers*, then the logs below. |
| The browser is on another PC | Not supported offline: keep the browser on the demo PC. Across PCs WebRTC needs a relay server, which needs internet or extra setup. |

Logs: the API, worker and web app write to `%TEMP%\atlas-demo\` (`api.log`, `api.err.log`,
…). Containers: `docker compose --profile voice --profile local-ai logs --tail 50 <service>`.

## Stop everything

```powershell
.\make.ps1 demo-stop
```

This stops the API, worker and web app that the setup started and all demo containers. Data,
models and settings are kept, so the next `.\make.ps1 demo` is quick. Programs the script did
not start (for example your own `pnpm dev`) are left running.

## Reference: what `.\make.ps1 demo` does

1. Checks Docker, Docker Compose, Node.js 22, pnpm 10 and uv; warns under 16 GB RAM, 6 GB free
   RAM, 8 cores, 7 GB of Docker memory or 25 GB free disk.
2. Creates `.env` from `.env.example` if it is missing, and fills every empty secret with a
   random value (never printed). Values already set are kept.
3. Installs the Python and web packages (`uv sync`, `pnpm install`), offline if needed.
4. Starts the database, the voice service and the model servers in Docker (images are built
   only when missing; `-Rebuild` forces it) and waits until each one is healthy.
5. Migrates and seeds the database (demo organisation and `demo@example.com`).
6. Downloads `qwen2.5:1.5b` and Whisper base if they are missing. Pocket TTS and live
   transcription fetch their weights on their first start.
7. Starts the API, worker and web app in the background, with logs in `%TEMP%\atlas-demo` and
   process ids in `.atlas-run\`. Ones it started earlier are restarted. Ones started by
   someone else are re-used if they answer, and only stopped with `-Restart`.
8. Signs in and adds the self-hosted connections *Local LLM (Ollama)*, *Local speech (Whisper)*
   and *Pocket TTS (local-tts)* unless a connection with that name or address exists; adds
   *Live speech (local-stt)* when a Deepgram key is given; applies the preset; checks the
   models; creates the agent *Mortgage lead qualifier* if missing and publishes it.
9. Loads the models into memory and prints the address, the sign-in and the active preset.

| PowerShell | bash | Meaning |
|---|---|---|
| `-DryRun` (or `-WhatIf`) | `--dry-run` | Show the plan, change nothing |
| `-Restart` | `--restart` | Also stop programs holding ports 8080 and 5173 that the script did not start |
| `-Preset auto\|pocket\|streaming` | `--preset …` | Choose the speed preset (default `auto`) |
| `-DeepgramKey <key>` | `--deepgram-key <key>` | Save the live-transcription connection (prefer the `ATLAS_DEMO_DEEPGRAM_KEY` variable) |
| `-SkipWarm` | `--skip-warm` | Do not load the models at the end |
| `-Rebuild` | `--rebuild` | Rebuild the Pocket TTS and live-transcription images (needs internet) |
| `-Stop` / `.\make.ps1 demo-stop` | `--stop` / `make demo-stop` | Stop everything |

On Linux and macOS use `make demo` (pass options as `make demo ARGS="--restart"`). The script
was tested on Windows. On Linux, Docker containers reach the host only through an explicit
host mapping, so call records from the voice service need an `extra_hosts` entry for
`host.docker.internal` and the API listening beyond `127.0.0.1` (`ATLAS_DEMO_API_HOST`).
