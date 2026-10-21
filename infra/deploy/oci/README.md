# ATLAS on one cloud server (Oracle Cloud)

Everything runs on one Linux server with Docker: the web app, the ATLAS API and worker, the
voice service, the self-hosted models (speech-to-text, language model, voice), Caddy for
HTTPS and coturn as the TURN relay that lets browsers on any network connect their call
audio. Tested on Oracle Cloud: Oracle Linux 9, 4 OCPU AMD (8 vCPU), 32 GB RAM, 100 GB boot
volume, region us-ashburn-1.

| Address | What |
|---|---|
| `https://atlas.<ip-with-dashes>.sslip.io` | web app, ATLAS API under `/api` |
| `https://voice.<ip-with-dashes>.sslip.io` | only the call signalling WebSocket (`/api/v1/ws/public/signaling/...`) |
| `turn.<ip-with-dashes>.sslip.io:3478` | TURN relay (UDP and TCP), relay ports UDP 49152-49252 |

[sslip.io](https://sslip.io) names resolve to the IP written in them, so no DNS setup is
needed, and Caddy gets Let's Encrypt certificates for them automatically. With your own
domain, point two A records at the server and pass `--web-host` / `--voice-host`.

The same script also sets up a server with **only the AI models** behind an HTTPS gateway
(`--role models`), for an ATLAS on another server, and can add that gateway to this
all-in-one server (`--ai-host`) so other ATLAS installations use its models. When to use
which, step by step: [docs/deployment-layouts.md](../../../docs/deployment-layouts.md).

## What is public, what is not

* Published on the host: only Caddy (TCP 80, 443) and coturn (3478 TCP/UDP on the private
  address, UDP 49152-49252, host network). PostgreSQL, Redis, object storage, the voice
  service's API, the STUN responder and the model servers publish no port at all
  (`ports: !reset []` in `docker-compose.prod.yml`); they are reachable only on the Docker
  network.
* On the voice hostname Caddy forwards only the signalling WebSocket. Browsers get the
  one-time session and the TURN credentials from the ATLAS API, never from the voice
  service, whose accounts, agents and admin routes stay internal.
* coturn uses time-limited credentials (`use-auth-secret`, the secret shared with the voice
  service as `TURN_SECRET`), relays UDP only, and refuses private, loopback, link-local
  (cloud metadata) and reserved peers except the voice service's own container address.
* With `--ai-host`, Caddy also serves the AI gateway on that hostname: the model servers'
  OpenAI-compatible APIs, live speech-to-text and a health check, each behind a token or a
  secret path ([docs/deployment-layouts.md](../../../docs/deployment-layouts.md)). Without
  it, no model server is reachable from outside.
* `.env` (mode 600) holds every secret, generated on the server from `/dev/urandom` on the
  first run. Nothing secret is in the repository or printed, except the demo password and
  the AI gateway's token and live speech-to-text address, once, on the run that generates
  them.
* ATLAS runs with `ATLAS_ENVIRONMENT=production`: secure cookies, HSTS, https-only URLs for
  tenant webhooks/actions/models, no API docs, no public sign-up, real phone calls off.

## Before the first deploy

1. **Network** (Oracle console: VCN, subnet, security list or network security group),
   ingress from `0.0.0.0/0`: TCP 80, TCP 443, TCP 3478, UDP 3478, UDP 49152-49252. On the
   server (`firewalld`):

   ```bash
   sudo firewall-cmd --permanent --add-service=http --add-service=https \
     --add-port=3478/tcp --add-port=3478/udp --add-port=49152-49252/udp
   sudo firewall-cmd --reload
   ```

   `deploy.sh` checks `firewalld` on every run and opens what is missing, when `sudo` needs
   no password. A models-only server (`--role models`) needs TCP 80 and 443 only.

2. **Disk**: about 30 GB for images, models and data. Grow the boot volume in the console
   (Boot volume, Edit, size), then on the server `sudo /usr/libexec/oci-growfs -y` and check
   `df -h /`. The script stops when less than 4 GB would stay free.
3. **Docker**: Docker Engine with Compose v2.24 or newer, and git. Add your user to the
   `docker` group (`sudo usermod -aG docker $USER`, then log in again).

## Deploy and update

```bash
git clone https://github.com/abdulmanan69/atlas.git ~/atlas
cd ~/atlas
bash infra/deploy/oci/deploy.sh --public-ip <public IP>
```

The first run takes 15 to 30 minutes (images, two image builds, models). It ends with the
address and, once, the demo sign-in (`demo@example.com` and a generated password, kept in
`.env` as `ATLAS_DEMO_PASSWORD`). Run the same command again to update: it does
`git pull`, rebuilds the web app and the API image, restarts what changed, migrates the
database and keeps `.env`, data and models.

Live transcription (faster replies) needs a Deepgram key once, to save the connection to
the local streaming server. Give it through the environment so it never lands in a file or
the shell history:

```bash
read -rs ATLAS_DEMO_DEEPGRAM_KEY && export ATLAS_DEMO_DEEPGRAM_KEY
bash infra/deploy/oci/deploy.sh
unset ATLAS_DEMO_DEEPGRAM_KEY
```

Without a key the first setup uses Whisper with Pocket TTS. The speed preset is applied
on the first setup only: later runs keep the demo organisation's models, including a
speech-to-text chosen in the web app, and the last lines say which one is in use. Pass
`--preset pocket` or `--preset streaming` to apply one again.

| Option | Meaning |
|---|---|
| `--role all\|models` | `all` (default): everything on this server; `models`: only the AI models behind the AI gateway ([docs/deployment-layouts.md](../../../docs/deployment-layouts.md)). Saved in `.env`; a server keeps its role |
| `--ai-host NAME` | hostname of the AI gateway (models role default: `ai.<ip-with-dashes>.sslip.io`; all-in-one: off unless given). `http://<private IP>` on a private network |
| `--public-ip IP` | the server's public IPv4 (saved in `.env`) |
| `--web-host`, `--voice-host` | your own hostnames instead of sslip.io |
| `--preset pocket\|streaming` | apply this speed preset to the demo organization now (default: only on the first setup) |
| `--no-pull` | deploy the checked-out code as it is |
| `--rebuild` | rebuild the Pocket TTS and live-transcription images |
| `--skip-warm` | do not load the models into memory at the end |

`.env` also sets `COMPOSE_FILE` and `COMPOSE_PROFILES`, so plain `docker compose ps`,
`docker compose logs -f api` and so on work in the repository folder.

## Check it

```bash
curl -sI https://atlas.<ip-with-dashes>.sslip.io/
curl -s https://atlas.<ip-with-dashes>.sslip.io/api/v1/health/ready

# On the server: an automated caller (inside the voice service container) places two
# test calls through the API and the voice service (no TURN needed there).
python3 infra/scripts/e2e_test_call.py --runs 2 --api http://api:8080 --signalling ws://localhost:8000

# From any other computer with uv and a Pocket TTS server for the caller's voice (for
# example the local demo's, http://127.0.0.1:8002): the same caller over the internet,
# through the public addresses and the TURN relay, like a remote browser.
read -rs ATLAS_DEMO_PASSWORD && export ATLAS_DEMO_PASSWORD
uv run --no-project --python 3.12 --with aiortc --with numpy --with websockets \
  python infra/scripts/e2e_test_call.py --runner local --runs 2 \
  --api https://atlas.<ip-with-dashes>.sslip.io --origin https://atlas.<ip-with-dashes>.sslip.io
```

Each run prints the ICE pair; through the relay it reads like
`host <caller LAN IP>:64352 -> relay <public IP>:49252`. Without a Pocket TTS server, give the
caller's lines as WAV files (`--utterance-wav`, see
[docs/testing-voice.md](../../../docs/testing-voice.md)).

## How calls connect (WebRTC behind cloud NAT)

Oracle gives the server a private address (here 10.0.0.125) behind a 1:1 NAT to the public
one. Browsers get time-limited TURN credentials from the ATLAS API with every session, and
the audio always takes the relay on this server:

* The browser's relay address on coturn forwards to the voice service's container address
  (the only private peer coturn may reach), or the voice service's own relay address on
  coturn answers the browser. Its TURN client reaches coturn on the private address
  (`extra_hosts`).
* The voice service keeps its container candidates (`SERVER_IP` is the private address).
* The voice service looks up its public address at `stun.l.google.com:19302`. Inside
  Docker that returns the Docker NAT mapping of the public IP, a direct path that ICE ranks
  above every relay path but that dropped the caller's audio on a network with two NAT
  exits (measured: garbled speech, then a call with no greeting). The `stun` service answers
  that name on the Docker network instead, so the voice service never advertises it, and
  no lookup leaves the server.

coturn 4.18 notes: the CLI, DTLS, RFC 5780 and the software attribute are off by default
(the old `no-cli`/`no-dtls` options are rejected), and IPv6 `denied-peer-ip` ranges such as
`::-::ffff` matched public IPv4 peers, so only IPv4 ranges are listed (relays are IPv4).
Compose does not notice a changed inline config; `deploy.sh` recreates coturn when the
rendered `turnserver.conf` changes.

## Operations

| Task | Command (in `~/atlas`) |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f --tail 100 api` (or `voice-runtime`, `caddy`, `coturn`, `local-llm`) |
| Resource use | `docker stats --no-stream` |
| Restart one service | `docker compose restart api` |
| Stop everything | `docker compose down` (data and models stay in volumes) |
| Database backup | `docker compose exec -T postgres pg_dump -U atlas atlas \| gzip > atlas-$(date +%F).sql.gz` |

Keep `.env` with the backups: it holds the key that encrypts stored provider credentials.
