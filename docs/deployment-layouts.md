# Deployment layouts

ATLAS has three parts with very different needs ([ADR 0007](decisions/0007-separate-ai-models.md)):

| Part | What runs | Needs |
|---|---|---|
| **App** (the CRM) | web app, ATLAS API, worker, PostgreSQL, Redis | little CPU; always on; holds the tenants' data |
| **Voice** (call engine) | voice service with its database, Redis and storage, TURN relay | moderate CPU; real time; public signalling and TURN ports |
| **Models** (AI) | language model (Ollama), Whisper, Pocket TTS, live speech-to-text | most of the compute; a GPU makes replies about 25 times faster ([perf/llm-turns.md](perf/llm-turns.md)) |

All three run from one code base and the same Compose files. On one server they find each
other by Docker name on a private network. When the models run on another server, ATLAS
reaches them through the **AI gateway**: one HTTPS address on the models server, with a
token.

## The four layouts

| Layout | Servers | When | Set up with |
|---|---|---|---|
| All in one | 1 | demos, small teams | `deploy.sh` ([infra/deploy/oci/README.md](../infra/deploy/oci/README.md)) |
| App + voice · models | 2 | a GPU server for the models, in the same region as the app | `deploy.sh` on the app server, `deploy.sh --role models` on the models server ([below](#models-on-their-own-server)) |
| App · voice + models | 2 | **recommended with a GPU**: the voice service sits next to the models, so no model request crosses the network | not yet: needs the voice service on its own server (ADR 0007, phase B) |
| App · voice · models | 3 | large deployments | not yet (phase B) |

An all-in-one server can also lend its models to other ATLAS installations: add the gateway
to it with `--ai-host` ([below](#lend-an-all-in-one-servers-models)).

### Which one

* **One server** is the simplest and the fastest per request: every model request stays
  inside the server. Choose it until the models need more compute than the server has.
* **Models on their own server** is for a GPU (or a much larger CPU server) that the app
  server does not have. Every model request of a call then crosses the network once: the
  language model once per reply, Pocket TTS once per sentence, live speech-to-text as one
  WebSocket for the whole call. Within one cloud region that adds about a millisecond per
  request; between continents it adds 70 to 150 ms each time, which callers hear. Put the
  models server in the app server's region.
* **One models server for several ATLAS installations**: each one connects with the
  gateway's address and token. Calls of every installation share the models' capacity
  (the language model answers one request at a time on CPU, `OLLAMA_NUM_PARALLEL=1`).

## The AI gateway

Caddy on the models server, the only thing that server publishes (TCP 80 and 443):

| Path | Goes to | Protocol |
|---|---|---|
| `/llm/v1/*` | language model, OpenAI-compatible API only (Ollama's `/api/*` management is not reachable) | HTTP, streamed |
| `/whisper/v1/*` | Whisper speech-to-text (the same server also has the Kokoro voice) | HTTP |
| `/tts/v1/*` | Pocket TTS, its voices and voice cloning | HTTP, streamed |
| `/live-stt/<secret>/v1/listen` | live speech-to-text, Deepgram protocol | WebSocket |
| `/health` | which services answer, their versions and models | HTTP |

* Every path except live speech-to-text needs `Authorization: Bearer <token>`
  (`ATLAS_AI_TOKEN` in the models server's `.env`, 256 bits). Without it the answer is
  `401 {"error":"unauthorized"}`; an unknown path is `404 {"error":"not_found"}`; a model
  server that is down is `502 {"error":"Bad Gateway"}`.
* Live speech-to-text carries a second secret in its path (`ATLAS_AI_LIVE_STT_SECRET`),
  because the voice service sends its Deepgram key in the `Authorization` header there.
* The gateway removes the `Authorization` header before a request goes on: the model
  servers never see the token or the Deepgram key.
* Request bodies are limited to 12 MB (voice samples for cloning are at most 10 MB). There
  is no timeout on streamed replies or WebSockets (calls last up to 20 minutes), and a
  WebSocket stays open for up to 20 minutes across a configuration reload.
* No access log: the live speech-to-text path carries a secret.
* HTTPS with an automatic Let's Encrypt certificate, HTTP/1.1 and HTTP/2.

The files: [`infra/deploy/oci/caddy/ai-gateway.caddy`](../infra/deploy/oci/caddy/ai-gateway.caddy)
(the paths and checks), [`infra/docker/ai-gateway/app.py`](../infra/docker/ai-gateway/app.py)
(the health service), and the Compose files `docker-compose.models.yml` and
`docker-compose.ai-gateway.yml` in `infra/deploy/oci/`.

`/health` asks each model server, with a 3-second budget each, and answers (shortened):

```json
{
  "status": "ok",
  "checked_at": "2026-10-22T10:15:02Z",
  "services": {
    "llm": {"up": true, "state": "up", "latency_ms": 4, "version": "0.40.6",
            "models": ["qwen2.5:1.5b"], "loaded": ["qwen2.5:1.5b"]},
    "whisper": {"up": true, "state": "up", "latency_ms": 3, "version": "v0.8.2",
                "models": ["Systran/faster-whisper-base"], "voice_models": ["speaches-ai/Kokoro-82M-v1.0-ONNX"]},
    "tts": {"up": true, "state": "up", "latency_ms": 2, "version": "1.0.0", "models": ["pocket-tts"],
            "voices": 20, "default_voice": "alba", "cloning": "off"},
    "live_stt": {"up": true, "state": "up", "latency_ms": 2, "version": "...", "model": "parakeet-en-110m",
                 "streaming": false, "language": "en", "sessions": 0, "max_sessions": 4}
  }
}
```

`status` is `ok` (all up), `degraded` (some up; HTTP 200) or `down` (none; HTTP 503). A
service that does not answer has `"up": false` and an `error` (`timeout`, `refused`,
`unreachable`, `HTTP <code>`, `invalid response`); one still loading its model has
`"state": "loading"`. `loaded` lists the language models in memory now: any other one
loads on its first request. `cloning` is `off`, `enabling`, `on`, `failed`, or `unknown`
for a Pocket TTS server without voice-cloning settings. Nothing secret is in the answer.

## Models on their own server

### What you need

* A Linux server with Docker Engine, Compose v2.24 or newer and git, as in
  [infra/deploy/oci/README.md](../infra/deploy/oci/README.md) ("Before the first deploy",
  steps 2 and 3). Tested setup: Oracle Linux 9, x86_64.
* Memory: the four model servers are limited to 6.5 GB together (language model 2.5 GB,
  Whisper 1.75 GB, Pocket TTS 1.5 GB, live speech-to-text 0.75 GB); take 8 GB or more.
  CPU: the language model uses `LOCAL_LLM_THREADS` (default 4) threads; read
  [local-models.md](local-models.md) for the speed settings. Disk: about 25 GB (images
  15 GB, of which the language model server's is 9.4 GB; models 2 to 4 GB; room for
  builds), as the script stops when less than 4 GB would stay free.
* These files run the models on CPU. A GPU needs the NVIDIA container toolkit and a GPU
  device for `local-llm`; `deploy.sh` does not set that up yet.

### 1. Network

In the cloud's firewall (Oracle: the subnet's security list or a network security group),
allow ingress **TCP 80 and TCP 443 only** (plus SSH, as you have it). Port 80 is needed for
the Let's Encrypt certificate and redirects to 443. The model servers publish no port.

`deploy.sh` opens the same two in `firewalld` on the server, and nothing else, when
`firewalld` runs and `sudo` needs no password; otherwise it says what to open:

```bash
sudo firewall-cmd --permanent --add-service=http --add-service=https
sudo firewall-cmd --reload
```

### 2. Hostname

By default the gateway is `ai.<ip-with-dashes>.sslip.io` (for 203.0.113.7:
`ai.203-0-113-7.sslip.io`), which resolves to the server without any DNS setup. With your
own domain, point an A record at the server and pass `--ai-host ai.example.com`.

### 3. Deploy

```bash
git clone https://github.com/abdulmanan69/atlas.git ~/atlas
cd ~/atlas
bash infra/deploy/oci/deploy.sh --role models --public-ip <public IP>
```

The first run takes about 15 to 30 minutes (images, two image builds, about 1.5 GB of
models).
It starts only the four model servers, the health service and Caddy: no PostgreSQL, Redis,
ATLAS API, web app or voice service. `.env` keeps `ATLAS_ROLE=models`, so later runs need
no options; a server keeps its role (set up a new server for another one).

### 4. Keep the output

The last lines show the gateway address, the **token** and the **live speech-to-text
address**. The token and the secret in that address are shown only on the run that
generates them: put them in your password manager now. They stay in `.env` (mode 600) as
`ATLAS_AI_TOKEN` and `ATLAS_AI_LIVE_STT_SECRET`.

```
ATLAS models host is deployed.
  AI gateway:  https://ai.203-0-113-7.sslip.io   (for ATLAS on other hosts; docs/deployment-layouts.md)
  Token:       Hq3...   (generated now, saved in .env as ATLAS_AI_TOKEN; shown only this once)
  Live STT:    https://ai.203-0-113-7.sslip.io/live-stt/9f2c...   (Deepgram base URL; ...)
```

### 5. Check it from another computer

```bash
read -rs TOKEN && export TOKEN                      # paste the token
curl -s https://ai.203-0-113-7.sslip.io/health      # 401: the token is required
curl -s -H "Authorization: Bearer $TOKEN" https://ai.203-0-113-7.sslip.io/health
curl -s -H "Authorization: Bearer $TOKEN" https://ai.203-0-113-7.sslip.io/llm/v1/models
```

### 6. Connect ATLAS

Give the ATLAS installation three things:

| What | Value |
|---|---|
| Gateway address | `https://ai.203-0-113-7.sslip.io` |
| Token | the token from step 4 |
| Live speech-to-text address | `https://ai.203-0-113-7.sslip.io/live-stt/<secret>` (optional) |

Until ATLAS registers models servers in one step (ADR 0007, "AI servers"), add the
connections by hand in **Voice AI → AI providers**, each **Self-hosted models** with the
token as its API key:

| Connection | Base URL | Model |
|---|---|---|
| Language model | `https://<gateway>/llm/v1` | `qwen2.5:1.5b` |
| Whisper | `https://<gateway>/whisper/v1` | `Systran/faster-whisper-base` |
| Pocket TTS | `https://<gateway>/tts/v1` | `pocket-tts`, voice `alba` |

For live speech-to-text add a **Deepgram** connection with the live speech-to-text address
as its base URL and a real Deepgram API key: the voice service checks every Deepgram key
with Deepgram's cloud when the connection is saved, whatever the base URL
([local-models.md](local-models.md), "Live transcription"). No audio goes to Deepgram; the
key is not used by the gateway.

### 7. Update

Run the same command again (`bash infra/deploy/oci/deploy.sh`): it does `git pull`,
pulls updated images, rebuilds the health service, restarts what changed and keeps `.env`,
the models and the certificate. `--skip-warm`, `--rebuild` and `--no-pull` work as on an all-in-one server.

## Lend an all-in-one server's models

An all-in-one server can also serve other ATLAS installations, on a third hostname:

```bash
bash infra/deploy/oci/deploy.sh --ai-host ai.<ip-with-dashes>.sslip.io
```

This generates the token and the live speech-to-text secret (shown once), adds
`docker-compose.ai-gateway.yml` to `COMPOSE_FILE` and the gateway to the same Caddy, next
to the web app and the voice hostname. Adding it restarts Caddy once (a few seconds; calls
in progress are cut), so do it outside calling hours. The server's own calls keep using the
model servers by Docker name. The other installations' calls share the same CPU with this
server's own calls.

To turn the gateway off again, delete the `ATLAS_AI_HOST=` line from `.env` and run
`deploy.sh`: it removes the gateway file from `COMPOSE_FILE` and the health service. The
token stays in `.env` and is used again (not shown again) if you turn the gateway back on.

## On a private network

When the ATLAS servers and the models server share a private network (one cloud network,
peered networks, or a VPN such as WireGuard), the gateway can serve plain HTTP on the
models server's private address instead of a public hostname:

```bash
bash infra/deploy/oci/deploy.sh --role models --ai-host http://10.0.0.5
```

* In the cloud's firewall allow TCP 80 **only from the ATLAS servers' private addresses**
  (or the private network's range), and do not open 80 or 443 to the internet: Docker
  publishes the port on every interface of the server.
* The token still travels with every request, unencrypted on that network: use this only
  on a network you control.
* In production, ATLAS itself sends requests to a models server (the language model
  warm-up at call start) only over https to a public address, so with plain HTTP those are
  skipped: the first reply of a call can be slower. Calls work.

## Rotate the token

Rotate when the token may have leaked, or when an installation should lose access. The
gateway accepts a second token, `ATLAS_AI_TOKEN_NEXT`, so the switch can be made without
failed requests. On the models server:

```bash
cd ~/atlas
printf 'ATLAS_AI_TOKEN_NEXT=%s\n' "$(head -c 32 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_')" >> .env
grep '^ATLAS_AI_TOKEN_NEXT=' .env          # the new token: copy it
bash infra/deploy/oci/deploy.sh --no-pull --skip-warm
```

1. Both tokens now work. In every ATLAS installation that uses this server, replace the
   token with the new one.
2. Make the new token the only one: in `.env`, set `ATLAS_AI_TOKEN` to the new value and
   delete the `ATLAS_AI_TOKEN_NEXT` line, then run `deploy.sh` again. The old token is
   refused from then on.

After a leak, skip the waiting: set `ATLAS_AI_TOKEN` to a new value at once (or delete the
line and `deploy.sh` generates and shows a new one), deploy, then update ATLAS; its model
requests fail in between. Each change of a token restarts Caddy (a few seconds; on an
all-in-one server the web app and calls in progress too).

The live speech-to-text secret is replaced the same way without the second slot: delete
`ATLAS_AI_LIVE_STT_SECRET` from `.env`, run `deploy.sh` (it shows the new address once),
then change the Deepgram connection's base URL in ATLAS.

## Privacy

Calls that use a models server on another host send the conversation's text and the
caller's audio to that server: over HTTPS, or in plain HTTP on your private network. Run
it with the same care as the app server: the same hosting provider and region rules,
restricted SSH, updates, and access only for the people who run ATLAS. The gateway keeps
no access log. Cloned voices are stored on the models server, as on an all-in-one server
([local-models.md](local-models.md), "Voice cloning"). Every ATLAS installation that has
the token can use every model and cloned voice on that server: share a models server only
between installations of the same operator.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401 {"error":"unauthorized"}` | token missing or wrong (`Authorization: Bearer <token>`, exactly) |
| `404 {"error":"not_found"}` | the path is not one of the five above (Ollama's `/api/*` is never reachable) |
| `502 {"error":"Bad Gateway"}` | that model server is not running: `docker compose ps`, `docker compose logs --tail 80 local-llm` |
| `/health` says `degraded` | the service named `"up": false` is down or still loading |
| no answer on 443 | TCP 80/443 closed in the cloud's firewall, or the certificate is not issued yet: `docker compose logs caddy` |
| live speech-to-text refused with 401 | the secret in the address is wrong (a wrong secret looks like any unknown path without a token) |
