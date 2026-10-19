# Website widget ("Add to website")

A tenant can put a "Talk to us" button on its own site that connects visitors to a
published agent with their microphone. It is set up per agent under
**Agents → (agent) → Call settings → Add to website**: switch it on, list the sites that
may show it, save, and paste the snippet before `</body>`:

```html
<script src="https://<atlas-api>/api/v1/public/embed/widget.js" data-agent-key="wk_..." async></script>
```

Optional attributes: `data-label` (button text), `data-position="left"`, `data-color`.

## How it works

1. The script (served by ATLAS, no third-party branding) asks
   `POST /api/v1/public/embed/{key}/sessions`. ATLAS checks that the widget is switched
   on, that the page's origin (Origin, else Referer) is one of the allowed sites, and its
   rate limits (6 sessions per visitor IP and 60 per widget per minute). The agent must be
   published and active.
2. ATLAS records the conversation as a call (direction `inbound`, mode `live`, its
   `created` event carries `source: website` and the site), opens a voice session
   server-side and returns only a one-time signaling address and ICE servers.
3. The visitor's browser connects its audio to the voice service over WebRTC, exactly like
   the editor's browser test calls. The result (outcome, transcript, recording) arrives
   through the normal completion webhook and shows under **Calls**.
4. When the visitor hangs up or closes the page, the widget reports it
   (`.../sessions/{call_id}/end`, also accepted from `navigator.sendBeacon`); a call that
   never connected is closed as canceled.

No voice-service credential, address of its API or product name ever reaches the
browser. The widget key is public by design (it sits in the page); rotating it
(**New key**) stops old snippets at once. Allowed sites follow these rules: `www.` is
optional, `*.example.com` covers the domain and every subdomain, and a port, when given,
must match. A widget with no allowed sites cannot be switched on.

## Deployment requirements

* **The voice service's signaling address must be reachable by visitors.**
  `ATLAS_VOICE_RUNTIME_PUBLIC_URL` is the base of the signaling WebSocket
  (`wss://<public url>/api/v1/ws/public/signaling/<one-time token>`); put the voice service
  behind HTTPS on a public host. Visitors on restrictive networks also need a TURN relay
  configured on the voice service (the ICE servers come from it).
* The voice service re-checks the page's origin when the audio connects. ATLAS keeps the
  widget's sites (plus the web app's origin, for test calls) on the agent's voice session
  token automatically when the widget is saved and on every session.
* The script and session routes must be reachable at `ATLAS_PUBLIC_API_URL` (defaults to
  `ATLAS_WEB_APP_ORIGIN`, which proxies `/api` to the API). They answer cross-origin
  requests themselves (`EmbedCorsMiddleware`), without credentials.
