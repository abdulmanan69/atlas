import { useMutation } from "@tanstack/react-query";
import { Mic, MicOff, Phone, PhoneOff } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Card, CardHeader, Label } from "@/components/ui/primitives";
import { useLeads } from "@/features/queries";
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
    onSuccess: (session) => {
      setDetail(null);
      setMuted(false);
      call.current = new BrowserCall(session, {
        onState: (s, d) => {
          setState(s);
          if (d) setDetail(d);
          if (s === "live") setStarted(Date.now());
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
        {!published ? <p className="text-[12px] text-muted">Publish the agent first. Test calls always use the published version.</p> : null}
        <audio ref={audio} autoPlay hidden />
      </div>
    </Card>
  );
}
