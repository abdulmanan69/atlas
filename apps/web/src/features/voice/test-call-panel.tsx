import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Keyboard, MessageSquare, MessageSquareX, Mic, MicOff, Phone, PhoneOff, SendHorizontal } from "lucide-react";
import { useEffect, useId, useReducer, useRef, useState, type KeyboardEvent } from "react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Card, CardHeader, Label, Textarea } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { qk, useLeads, useOrgId } from "@/features/queries";
import { readChatEvents } from "@/features/voice/test-chat-stream";
import { TestTranscript } from "@/features/voice/test-transcript";
import { emptyTranscript, transcriptReducer, type ChatEvent, type TestChannel } from "@/features/voice/test-transcript-model";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api";
import { fullName } from "@/lib/format";
import { BrowserCall, type CallState } from "@/lib/webrtc";
import { cn } from "@/lib/utils";

function useLevel(stream: MediaStream | null): number {
  const [level, setLevel] = useState(0);
  useEffect(() => {
    if (!stream || typeof AudioContext === "undefined") return;
    const ctx = new AudioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    let raf = 0;
    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) sum += ((v - 128) / 128) ** 2;
      setLevel(Math.min(1, Math.sqrt(sum / data.length) * 4));
      raf = requestAnimationFrame(tick);
    };
    tick();
    return () => {
      cancelAnimationFrame(raf);
      void ctx.close();
    };
  }, [stream]);
  return level;
}

/** Eight-segment level meter; echoes the stage-track signature. */
function Meter({ level, label, tone }: { level: number; label: string; tone: "accent" | "success" }) {
  const lit = Math.round(level * 8);
  return (
    <div className="flex items-center gap-2" aria-label={label}>
      <span className="w-12 text-[11px] text-muted">{label}</span>
      <div className="flex gap-[3px]" aria-hidden>
        {Array.from({ length: 8 }, (_, i) => (
          <span key={i} className={cn("h-3 w-1.5 rounded-[1.5px] transition-colors duration-75", i < lit ? (tone === "accent" ? "bg-accent" : "bg-success") : "bg-line-strong/70")} />
        ))}
      </div>
    </div>
  );
}

const STATE_TEXT: Record<CallState, string> = {
  idle: "Talk to the published version in your browser.",
  "requesting-mic": "Allow microphone access to start…",
  connecting: "Connecting to the agent…",
  live: "Live. Speak naturally.",
  ended: "Call ended.",
  failed: "The call could not continue.",
};

type ChatState = "idle" | "starting" | "open" | "replying" | "ended" | "failed";

const CHAT_TEXT: Record<ChatState, string> = {
  idle: "Type to the published version: same prompt, steps and actions, no speech.",
  starting: "Starting the chat…",
  open: "Your turn. Type as the caller would talk.",
  replying: "The agent is replying…",
  ended: "Chat ended.",
  failed: "The chat could not start.",
};

const MODES = [
  {
    value: "voice" as const,
    label: (
      <span className="inline-flex items-center gap-1">
        <Mic className="size-3.5" aria-hidden /> Speak
      </span>
    ),
  },
  {
    value: "text" as const,
    label: (
      <span className="inline-flex items-center gap-1">
        <Keyboard className="size-3.5" aria-hidden /> Type
      </span>
    ),
  },
];

/**
 * Tells the API how a test call ended so its record under Calls is closed or synced.
 * Fire-and-forget: the record is completed by the voice service's report anyway.
 */
function reportEnd(agentId: string, callId: string, connected: boolean): Promise<unknown> {
  return api.POST("/api/v1/agents/{agent_id}/test-calls/{call_id}/end", { params: { path: { agent_id: agentId, call_id: callId } }, body: { connected } }).catch(() => undefined);
}

function endChat(agentId: string, callId: string): Promise<unknown> {
  return api.POST("/api/v1/agents/{agent_id}/test-chat/{call_id}/end", { params: { path: { agent_id: agentId, call_id: callId } } }).catch(() => undefined);
}

type StreamRequest = Promise<{ data?: ReadableStream<Uint8Array> | null; error?: unknown; response: Response }>;

/** Read one streamed chat turn; returns its last event (`done` or `error`), if any. */
async function consume(request: StreamRequest, onEvent: (event: ChatEvent) => void, signal: AbortSignal): Promise<ChatEvent | null> {
  const body = await unwrap(request);
  let last: ChatEvent | null = null;
  if (!body) return last;
  for await (const event of readChatEvents(body, signal)) {
    if (signal.aborted) break;
    onEvent(event);
    last = event;
  }
  return last;
}

function RecordLink({ callId, active, note }: { callId: string; active: boolean; note: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <Link
        to={`/calls/${callId}`}
        // While the test runs, open the record beside it: leaving this page would end it.
        {...(active ? { target: "_blank", rel: "noreferrer" } : {})}
        className="inline-flex items-center gap-1.5 self-start text-[12.5px] font-medium text-accent hover:underline"
      >
        <ExternalLink className="size-3.5" aria-hidden /> View call details
        {active ? <span className="sr-only"> (opens in a new tab)</span> : null}
      </Link>
      <p className="text-[12px] text-muted">{note}</p>
    </div>
  );
}

export function TestCallPanel({ agentId, published }: { agentId: string; published: boolean }) {
  const [mode, setMode] = useState<TestChannel>("voice");
  const [leadId, setLeadId] = useState("");
  const pickerId = useId();
  const qc = useQueryClient();
  const orgId = useOrgId();
  const leads = useLeads({ page_size: 50, sort: "-updated_at" });
  const refreshCalls = (id: string) => {
    void qc.invalidateQueries({ queryKey: ["org", orgId, "calls"] });
    void qc.invalidateQueries({ queryKey: qk.call(orgId, id) });
  };

  // ---------------------------------------------------------------- Speak
  const [state, setState] = useState<CallState>("idle");
  const [detail, setDetail] = useState<string | null>(null);
  const [local, setLocal] = useState<MediaStream | null>(null);
  const [remote, setRemote] = useState<MediaStream | null>(null);
  const [muted, setMuted] = useState(false);
  const [started, setStarted] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const call = useRef<BrowserCall | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  // The test call's record under Calls; reported once when the browser call ends.
  const [callId, setCallId] = useState<string | null>(null);
  const [voice, dispatchVoice] = useReducer(transcriptReducer, "voice", emptyTranscript);
  const micLevel = useLevel(state === "live" && !muted ? local : null);
  const agentLevel = useLevel(state === "live" ? remote : null);

  useEffect(() => {
    if (audio.current) audio.current.srcObject = remote;
  }, [remote]);
  useEffect(() => {
    if (state !== "live") return;
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, [state]);
  useEffect(() => () => call.current?.hangup(), []);

  const start = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/agents/{agent_id}/test-session", { params: { path: { agent_id: agentId } }, body: { lead_id: leadId || null } })),
    onMutate: () => setCallId(null),
    onSuccess: (session) => {
      setDetail(null);
      setMuted(false);
      setStarted(null);
      setCallId(session.call_id);
      dispatchVoice({ type: "reset", channel: "voice" });
      refreshCalls(session.call_id);
      let connected = false;
      let reported = false;
      call.current = new BrowserCall(session, {
        onState: (s, d) => {
          setState(s);
          if (d) setDetail(d);
          if (s === "live") {
            connected = true;
            setStarted(Date.now());
          }
          if ((s === "ended" || s === "failed") && !reported) {
            reported = true;
            void reportEnd(agentId, session.call_id, connected).then(() => refreshCalls(session.call_id));
          }
        },
        onLocalStream: setLocal,
        onRemoteStream: setRemote,
        onFeedback: (message) => dispatchVoice({ type: "voice", message, at: performance.now() }),
      });
      void call.current.start();
    },
    onError: (e) => {
      setState("failed");
      setDetail(errorMessage(e));
    },
  });

  const active = state === "requesting-mic" || state === "connecting" || state === "live";
  const elapsed = started && state === "live" ? Math.floor((now - started) / 1000) : 0;

  // ---------------------------------------------------------------- Type
  const [chat, setChat] = useState<ChatState>("idle");
  const [chatDetail, setChatDetail] = useState<string | null>(null);
  const [chatCallId, setChatCallId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [typed, dispatchChat] = useReducer(transcriptReducer, "text", emptyTranscript);
  const composer = useRef<HTMLTextAreaElement>(null);
  const turn = useRef<AbortController | null>(null);
  // For leaving the page mid-chat: end it so its record completes.
  const live = useRef<{ callId: string | null; open: boolean }>({ callId: null, open: false });
  const chatActive = chat === "starting" || chat === "open" || chat === "replying";
  live.current = { callId: chatCallId, open: chatActive };

  useEffect(
    () => () => {
      turn.current?.abort();
      if (live.current.open && live.current.callId) void endChat(agentId, live.current.callId);
    },
    [agentId],
  );

  const runTurn = async (open: () => StreamRequest, starting: boolean) => {
    const controller = new AbortController();
    turn.current = controller;
    try {
      const last = await consume(open(), (event) => {
        if (event.type === "started" && event.call_id) {
          setChatCallId(event.call_id);
          refreshCalls(event.call_id);
        }
        dispatchChat({ type: "chat", event });
      }, controller.signal);
      if (controller.signal.aborted) return;
      setChat(last?.type === "done" && last.ended ? "ended" : "open");
      if (!(last?.type === "done" && last.ended)) requestAnimationFrame(() => composer.current?.focus());
    } catch (e) {
      if (controller.signal.aborted) return;
      if (starting) {
        setChat("failed");
        setChatDetail(errorMessage(e));
        return;
      }
      const ended = e instanceof ApiError && e.code === "test_chat_ended";
      dispatchChat({ type: "chat", event: { type: "error", at_ms: 0, message: errorMessage(e) } });
      setChat(ended ? "ended" : "open");
    } finally {
      if (turn.current === controller) turn.current = null;
    }
  };

  const startChat = () => {
    dispatchChat({ type: "reset", channel: "text" });
    setChatCallId(null);
    setChatDetail(null);
    setDraft("");
    setChat("starting");
    void runTurn(
      () =>
        api.POST("/api/v1/agents/{agent_id}/test-chat", {
          params: { path: { agent_id: agentId } },
          body: { lead_id: leadId || null },
          parseAs: "stream",
        }),
      true,
    );
  };

  const send = () => {
    const text = draft.trim();
    if (!text || chat !== "open" || !chatCallId) return;
    dispatchChat({ type: "typed", text });
    setDraft("");
    setChat("replying");
    void runTurn(
      () =>
        api.POST("/api/v1/agents/{agent_id}/test-chat/{call_id}/messages", {
          params: { path: { agent_id: agentId, call_id: chatCallId } },
          body: { text },
          parseAs: "stream",
        }),
      false,
    );
  };

  const stopChat = () => {
    if (!chatCallId) return;
    turn.current?.abort();
    setChat("ended");
    void endChat(agentId, chatCallId).then(() => refreshCalls(chatCallId));
  };

  const onComposerKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  // ---------------------------------------------------------------- layout
  const busy = active || chatActive;
  const voiceShown = state !== "idle" || voice.items.length > 0;

  return (
    <Card>
      <CardHeader
        title="Test agent"
        description={mode === "voice" ? "Uses your microphone. Nothing is dialled and no lead is contacted." : "Type instead of speaking. No lead is contacted."}
        action={<Segmented label="How to test" size="sm" value={mode} options={MODES.map((o) => ({ ...o, disabled: busy && o.value !== mode }))} onChange={setMode} />}
      />
      <div className="flex flex-col gap-4 p-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={pickerId}>Fill variables from</Label>
          <Combobox
            id={pickerId}
            value={leadId || null}
            onValueChange={(v) => setLeadId(v ?? "")}
            disabled={busy}
            placeholder="Sample lead (Alex Sample)"
            searchPlaceholder="Search recent leads"
            emptyText="No recent leads match."
            allowClear
            options={(leads.data?.items ?? []).map((l) => ({
              value: l.id,
              label: fullName(l.first_name, l.last_name),
              description: l.company ?? undefined,
            }))}
          />
        </div>

        {mode === "voice" ? (
          <>
            <div className={cn("rounded-lg border px-4 py-4", state === "live" ? "border-success/40 bg-success-soft/40" : "border-line bg-surface-2/50")}>
              <div className="flex items-center justify-between gap-3">
                <p role="status" className={cn("text-[13px] font-medium", state === "failed" ? "text-danger" : "text-ink")}>
                  {STATE_TEXT[state]}
                </p>
                {state === "live" ? (
                  <span className="font-mono text-[13px] text-ink-2">
                    {String(Math.floor(elapsed / 60)).padStart(2, "0")}:{String(elapsed % 60).padStart(2, "0")}
                  </span>
                ) : null}
              </div>
              {detail && state !== "live" ? <p className="mt-1 text-[12.5px] text-muted">{detail}</p> : null}
              {state === "live" ? (
                <div className="mt-3 flex flex-col gap-1.5">
                  <Meter level={muted ? 0 : micLevel} label="You" tone="accent" />
                  <Meter level={agentLevel} label="Agent" tone="success" />
                </div>
              ) : null}
            </div>

            {voiceShown ? (
              <TestTranscript
                items={voice.items}
                channel="voice"
                empty={active ? "Listening. What you say and each reply, with its response time, appear here." : "Nothing was said on this call."}
              />
            ) : null}

            <div className="flex gap-2">
              {active ? (
                <>
                  <Button
                    onClick={() => {
                      call.current?.setMuted(!muted);
                      setMuted(!muted);
                    }}
                    aria-pressed={muted}
                    disabled={state !== "live"}
                  >
                    {muted ? <MicOff /> : <Mic />} {muted ? "Unmute" : "Mute"}
                  </Button>
                  <Button variant="danger" className="flex-1" onClick={() => call.current?.hangup()}>
                    <PhoneOff /> End call
                  </Button>
                </>
              ) : (
                <Button variant="primary" className="flex-1" disabled={!published} loading={start.isPending} onClick={() => start.mutate()}>
                  <Phone /> {state === "ended" || state === "failed" ? "Call again" : "Start test call"}
                </Button>
              )}
            </div>
            {callId && state !== "idle" ? (
              <RecordLink
                callId={callId}
                active={active}
                note={
                  active
                    ? "Recorded under Calls as a test call."
                    : started !== null
                      ? "The transcript and response times appear there a few seconds after the call ends."
                      : "It never connected, so there is no conversation to review."
                }
              />
            ) : null}
          </>
        ) : (
          <>
            <p role="status" className={cn("text-[13px] font-medium", chat === "failed" ? "text-danger" : "text-ink")}>
              {CHAT_TEXT[chat]}
            </p>
            {chatDetail && chat === "failed" ? <p className="-mt-3 text-[12.5px] text-muted">{chatDetail}</p> : null}

            <TestTranscript
              items={typed.items}
              channel="text"
              thinking={chat === "starting" || chat === "replying"}
              empty={
                <>
                  <MessageSquare className="mx-auto mb-1.5 size-5 text-faint" aria-hidden />
                  Start a chat to type to the agent. Each reply shows how long it took.
                </>
              }
            />

            {chat === "open" || chat === "replying" ? (
              <div className="flex flex-col gap-2">
                <div className="flex items-end gap-2">
                  <Textarea
                    ref={composer}
                    aria-label="Message to the agent"
                    placeholder="Type as the caller would talk. Enter sends, Shift+Enter adds a line."
                    rows={2}
                    maxLength={2000}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={onComposerKey}
                    className="min-h-0 flex-1 resize-none"
                  />
                  <Button variant="primary" size="icon" aria-label="Send" disabled={chat !== "open" || !draft.trim()} loading={chat === "replying"} onClick={send}>
                    <SendHorizontal />
                  </Button>
                </div>
                <Button variant="ghost" size="sm" className="self-start" disabled={chat === "replying"} onClick={stopChat}>
                  <MessageSquareX /> End chat
                </Button>
              </div>
            ) : (
              <Button variant="primary" disabled={!published || chat === "starting"} loading={chat === "starting"} onClick={startChat}>
                <MessageSquare /> {chat === "ended" || chat === "failed" ? "New test chat" : "Start test chat"}
              </Button>
            )}
            {chatCallId ? (
              <RecordLink
                callId={chatCallId}
                active={chatActive}
                note={chatActive ? "Recorded under Calls as a test chat." : "The transcript appears there a few seconds after the chat ends."}
              />
            ) : null}
          </>
        )}
        {!published ? <p className="text-[12px] text-muted">Publish the agent first. Tests always use the published version.</p> : null}
        <audio ref={audio} autoPlay hidden />
      </div>
    </Card>
  );
}
