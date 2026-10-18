/**
 * Browser voice call to an agent over WebRTC.
 *
 * Signaling protocol (voice runtime, public embed sessions):
 *   client → server  {type:"offer", payload:{sdp, type:"offer", pc_id}}
 *   client → server  {type:"ice-candidate", payload:{pc_id, candidate|null}}
 *   server → client  {type:"answer", payload:{sdp, type:"answer", pc_id}}
 *   server → client  {type:"error", payload:{message}}
 *   server → client  {type:"call-ended", payload:{reason}}
 * The server's own candidates arrive inside the answer SDP.
 */

export type CallState = "idle" | "requesting-mic" | "connecting" | "live" | "ended" | "failed";

export interface CallSession {
  signaling_url: string;
  ice_servers: { urls: string | string[]; username?: string | null; credential?: string | null }[];
}

export interface CallEvents {
  onState: (state: CallState, detail?: string) => void;
  onLocalStream?: (stream: MediaStream) => void;
  onRemoteStream?: (stream: MediaStream) => void;
}

export interface CallDeps {
  getUserMedia: (c: MediaStreamConstraints) => Promise<MediaStream>;
  createPeer: (config: RTCConfiguration) => RTCPeerConnection;
  createSocket: (url: string) => WebSocket;
  newId: () => string;
}

const defaultDeps = (): CallDeps => ({
  getUserMedia: (c) => navigator.mediaDevices.getUserMedia(c),
  createPeer: (config) => new RTCPeerConnection(config),
  createSocket: (url) => new WebSocket(url),
  newId: () => `pc-${crypto.randomUUID()}`,
});

const SOCKET_TIMEOUT_MS = 10_000;

export class BrowserCall {
  private pc: RTCPeerConnection | null = null;
  private ws: WebSocket | null = null;
  private local: MediaStream | null = null;
  private readonly pcId: string;
  private state: CallState = "idle";
  private readonly deps: CallDeps;

  constructor(
    private readonly session: CallSession,
    private readonly events: CallEvents,
    deps?: Partial<CallDeps>,
  ) {
    this.deps = { ...defaultDeps(), ...deps };
    this.pcId = this.deps.newId();
  }

  get current(): CallState {
    return this.state;
  }

  private set(state: CallState, detail?: string): void {
    if (this.state === "ended" || this.state === "failed") return;
    this.state = state;
    this.events.onState(state, detail);
  }

  private send(type: string, payload: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type, payload }));
  }

  async start(): Promise<void> {
    try {
      this.set("requesting-mic");
      try {
        this.local = await this.deps.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
          video: false,
        });
      } catch {
        this.fail("Microphone access was blocked. Allow the microphone for this site and try again.");
        return;
      }
      this.events.onLocalStream?.(this.local);
      this.set("connecting");

      const pc = this.deps.createPeer({ iceServers: this.session.ice_servers.map((s) => ({ ...s, username: s.username ?? undefined, credential: s.credential ?? undefined })) });
      this.pc = pc;
      for (const track of this.local.getTracks()) pc.addTrack(track, this.local);
      pc.ontrack = (e) => this.events.onRemoteStream?.(e.streams[0] ?? new MediaStream([e.track]));
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "connected") this.set("live");
        if (pc.connectionState === "failed")
          this.fail("The audio connection could not be established. A TURN relay may be needed on this network.");
      };
      pc.onicecandidate = (e) => this.send("ice-candidate", { pc_id: this.pcId, candidate: e.candidate ? e.candidate.toJSON() : null });

      await this.openSocket();
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      this.send("offer", { sdp: offer.sdp, type: "offer", pc_id: this.pcId });
    } catch (err) {
      this.fail(err instanceof Error ? err.message : "The call could not start.");
    }
  }

  private openSocket(): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = this.deps.createSocket(this.session.signaling_url);
      this.ws = ws;
      const timer = setTimeout(() => reject(new Error("The voice service did not respond.")), SOCKET_TIMEOUT_MS);
      ws.onopen = () => {
        clearTimeout(timer);
        resolve();
      };
      ws.onerror = () => {
        clearTimeout(timer);
        reject(new Error("Could not connect to the voice service."));
      };
      ws.onclose = () => {
        if (this.state === "live" || this.state === "connecting") this.end("The call ended.");
      };
      ws.onmessage = (event: MessageEvent<string>) => void this.onMessage(event.data);
    });
  }

  private async onMessage(raw: string): Promise<void> {
    let msg: { type?: string; payload?: { sdp?: string; message?: string; reason?: string } };
    try {
      msg = JSON.parse(raw) as typeof msg;
    } catch {
      return;
    }
    if (msg.type === "answer" && msg.payload?.sdp && this.pc) {
      await this.pc.setRemoteDescription({ type: "answer", sdp: msg.payload.sdp });
    } else if (msg.type === "error") {
      this.fail(msg.payload?.message ?? "The voice service reported an error.");
    } else if (msg.type === "call-ended") {
      this.end(msg.payload?.reason ? `The agent ended the call (${msg.payload.reason}).` : "The agent ended the call.");
    }
  }

  setMuted(muted: boolean): void {
    for (const track of this.local?.getAudioTracks() ?? []) track.enabled = !muted;
  }

  hangup(): void {
    this.end("You ended the call.");
  }

  private fail(message: string): void {
    this.set("failed", message);
    this.cleanup();
  }

  private end(message: string): void {
    this.set("ended", message);
    this.cleanup();
  }

  private cleanup(): void {
    this.ws?.close();
    this.ws = null;
    this.pc?.close();
    this.pc = null;
    for (const track of this.local?.getTracks() ?? []) track.stop();
    this.local = null;
  }
}
