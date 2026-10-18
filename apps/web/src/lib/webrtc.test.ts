import { BrowserCall, type CallState } from "@/lib/webrtc";

class FakeSocket {
  static OPEN = 1;
  readyState = 0;
  sent: { type: string; payload: Record<string, unknown> }[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  constructor(readonly url: string) {
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.();
    }, 0);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data) as never);
  }
  close() {
    this.readyState = 3;
  }
  receive(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

class FakePeer {
  connectionState = "new";
  remote: RTCSessionDescriptionInit | null = null;
  closed = false;
  tracks = 0;
  ontrack: unknown = null;
  onconnectionstatechange: (() => void) | null = null;
  onicecandidate: ((e: { candidate: { toJSON(): object } | null }) => void) | null = null;
  constructor(readonly config: RTCConfiguration) {}
  addTrack() {
    this.tracks += 1;
  }
  async createOffer() {
    return { type: "offer", sdp: "v=0 offer" };
  }
  async setLocalDescription() {
    this.onicecandidate?.({ candidate: { toJSON: () => ({ candidate: "cand-1", sdpMid: "0" }) } });
    this.onicecandidate?.({ candidate: null });
  }
  async setRemoteDescription(d: RTCSessionDescriptionInit) {
    this.remote = d;
  }
  close() {
    this.closed = true;
  }
  connect() {
    this.connectionState = "connected";
    this.onconnectionstatechange?.();
  }
}

function setup(mic: "ok" | "denied" = "ok") {
  const states: [CallState, string | undefined][] = [];
  let socket!: FakeSocket;
  let peer!: FakePeer;
  const stop = vi.fn();
  const track = { enabled: true, stop };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
  vi.stubGlobal("WebSocket", { OPEN: 1 });
  const call = new BrowserCall(
    { signaling_url: "ws://voice.test/signal/abc", ice_servers: [{ urls: "stun:stun.example:19302" }] },
    { onState: (s, d) => states.push([s, d]) },
    {
      getUserMedia: () => (mic === "ok" ? Promise.resolve(stream) : Promise.reject(new Error("NotAllowedError"))),
      createPeer: (c) => (peer = new FakePeer(c)) as unknown as RTCPeerConnection,
      createSocket: (url) => (socket = new FakeSocket(url)) as unknown as WebSocket,
      newId: () => "pc-test",
    },
  );
  return { call, states, get socket() { return socket; }, get peer() { return peer; }, track, stop };
}

describe("BrowserCall", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends an offer and trickles candidates using the runtime protocol", async () => {
    const t = setup();
    await t.call.start();
    expect(t.peer.config.iceServers).toEqual([{ urls: "stun:stun.example:19302", username: undefined, credential: undefined }]);
    expect(t.peer.tracks).toBe(1);
    expect(t.socket.url).toBe("ws://voice.test/signal/abc");
    const types = t.socket.sent.map((m) => m.type);
    expect(types).toContain("offer");
    const offer = t.socket.sent.find((m) => m.type === "offer")!;
    expect(offer.payload).toEqual({ sdp: "v=0 offer", type: "offer", pc_id: "pc-test" });
    expect(t.socket.sent.filter((m) => m.type === "ice-candidate").map((m) => m.payload.candidate)).toEqual([
      { candidate: "cand-1", sdpMid: "0" },
      null,
    ]);
  });

  it("applies the answer and goes live when audio connects", async () => {
    const t = setup();
    await t.call.start();
    t.socket.receive({ type: "answer", payload: { sdp: "v=0 answer", type: "answer", pc_id: "pc-test" } });
    await Promise.resolve();
    expect(t.peer.remote).toEqual({ type: "answer", sdp: "v=0 answer" });
    t.peer.connect();
    expect(t.states.map((s) => s[0])).toEqual(["requesting-mic", "connecting", "live"]);
  });

  it("ends cleanly when the agent hangs up, releasing the microphone", async () => {
    const t = setup();
    await t.call.start();
    t.peer.connect();
    t.socket.receive({ type: "call-ended", payload: { reason: "completed" } });
    expect(t.states.at(-1)).toEqual(["ended", "The agent ended the call (completed)."]);
    expect(t.peer.closed).toBe(true);
    expect(t.stop).toHaveBeenCalled();
  });

  it("explains a blocked microphone", async () => {
    const t = setup("denied");
    await t.call.start();
    expect(t.states.at(-1)?.[0]).toBe("failed");
    expect(t.states.at(-1)?.[1]).toMatch(/Microphone access was blocked/);
  });

  it("mutes by disabling the outgoing track", async () => {
    const t = setup();
    await t.call.start();
    t.call.setMuted(true);
    expect(t.track.enabled).toBe(false);
    t.call.setMuted(false);
    expect(t.track.enabled).toBe(true);
  });
});
