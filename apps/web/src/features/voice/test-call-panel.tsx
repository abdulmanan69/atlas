import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Mic, MicOff, Phone, PhoneOff } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Card, CardHeader, Label } from "@/components/ui/primitives";
import { qk, useLeads, useOrgId } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
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

/**
 * Tells the API how a test call ended so its record under Calls is closed or synced.
 * Fire-and-forget: the record is completed by the voice service's report anyway.
 */
function reportEnd(agentId: string, callId: string, connected: boolean): Promise<unknown> {
  return api.POST("/api/v1/agents/{agent_id}/test-calls/{call_id}/end", { params: { path: { agent_id: agentId, call_id: callId } }, body: { connected } }).catch(() => undefined);
}

export function TestCallPanel({ agentId, published }: { agentId: string; published: boolean }) {
  const [state, setState] = useState<CallState>("idle");
  const [detail, setDetail] = useState<string | null>(null);
  const [local, setLocal] = useState<MediaStream | null>(null);
  const [remote, setRemote] = useState<MediaStream | null>(null);
  const [muted, setMuted] = useState(false);
  const [leadId, setLeadId] = useState("");
  const pickerId = useId();
  const [started, setStarted] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const call = useRef<BrowserCall | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  // The test call's record under Calls; reported once when the browser call ends.
  const [callId, setCallId] = useState<string | null>(null);
  const qc = useQueryClient();
  const orgId = useOrgId();
  const leads = useLeads({ page_size: 50, sort: "-updated_at" });
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
      const refreshCalls = () => {
        void qc.invalidateQueries({ queryKey: ["org", orgId, "calls"] });
        void qc.invalidateQueries({ queryKey: qk.call(orgId, session.call_id) });
      };
      refreshCalls();
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
            void reportEnd(agentId, session.call_id, connected).then(refreshCalls);
          }
        },
        onLocalStream: setLocal,
        onRemoteStream: setRemote,
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

  return (
    <Card>
      <CardHeader title="Test call" description="Uses your microphone. Nothing is dialled and no lead is contacted." />
      <div className="flex flex-col gap-4 p-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={pickerId}>Fill variables from</Label>
          <Combobox
            id={pickerId}
            value={leadId || null}
            onValueChange={(v) => setLeadId(v ?? "")}
            disabled={active}
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
          <div className="flex flex-col gap-0.5">
            <Link
              to={`/calls/${callId}`}
              // While the call runs, open the record beside it: leaving this page would end the call.
              {...(active ? { target: "_blank", rel: "noreferrer" } : {})}
              className="inline-flex items-center gap-1.5 self-start text-[12.5px] font-medium text-accent hover:underline"
            >
              <ExternalLink className="size-3.5" aria-hidden /> View call details
              {active ? <span className="sr-only"> (opens in a new tab)</span> : null}
            </Link>
            <p className="text-[12px] text-muted">
              {active
                ? "Recorded under Calls as a test call."
                : started !== null
                  ? "The transcript and response times appear there a few seconds after the call ends."
                  : "It never connected, so there is no conversation to review."}
            </p>
          </div>
        ) : null}
        {!published ? <p className="text-[12px] text-muted">Publish the agent first. Test calls always use the published version.</p> : null}
        <audio ref={audio} autoPlay hidden />
      </div>
    </Card>
  );
}
