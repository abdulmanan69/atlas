import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Bot, Download, ExternalLink, FileText, Info, Pause, Play, User } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

import { EmptyState, ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardHeader, Skeleton, Tooltip } from "@/components/ui/primitives";
import { eventLabel, flattenUsage, MODE_BADGE, outcomeLabel, outcomeTone, parseTranscript, STATE_BADGE, TERMINAL_STATES } from "@/features/calls/calls-model";
import { ResponseTimesCard } from "@/features/calls/response-times-card";
import { qk, useAgentNames, useCall, useCallEvents, useOrgId, type CallDetail } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatCost, formatDateTime, formatDuration, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

export default function CallDetailPage() {
  const { callId = "" } = useParams();
  // Keep refreshing while the call is still moving (queued → dialing → in progress → done).
  const call = useCall(callId, { refetchInterval: (data) => (data && !TERMINAL_STATES.has(data.state) ? 5_000 : false) });

  if (call.isError) {
    return (
      <>
        <BackLink />
        <Card>
          <ErrorState error={call.error} onRetry={() => void call.refetch()} />
        </Card>
      </>
    );
  }
  if (!call.data) {
    return (
      <div className="flex flex-col gap-5">
        <Skeleton className="h-4 w-16" />
        <Skeleton className="h-9 w-72" />
        <div className="grid gap-5 lg:grid-cols-3">
          <Skeleton className="h-96 lg:col-span-2" />
          <Skeleton className="h-96" />
        </div>
      </div>
    );
  }
  return <CallView call={call.data} />;
}

function BackLink() {
  return (
    <Link to="/calls" className="mb-4 inline-flex items-center gap-1 text-[12.5px] font-medium text-muted hover:text-ink">
      <ArrowLeft className="size-3.5" /> Calls
    </Link>
  );
}

export function CallView({ call }: { call: CallDetail }) {
  const names = useAgentNames();
  const navigate = useNavigate();
  const state = STATE_BADGE[call.state];
  const mode = MODE_BADGE[call.mode];
  const phone = call.direction === "inbound" ? call.from_phone_e164 : call.to_phone_e164;
  const title = call.lead_name ?? phone ?? (call.direction === "inbound" ? "Unknown caller" : "Call");
  const agentName = call.agent_id ? names.get(call.agent_id) : null;
  const at = call.started_at ?? call.created_at;

  return (
    <>
      <BackLink />
      <header className="flex flex-wrap items-start justify-between gap-4 pb-5">
        <div className="min-w-0">
          <h1 className="title-display text-[28px]">
            {call.lead_id ? (
              <Link to={`/leads/${call.lead_id}`} className="hover:text-accent">
                {title}
              </Link>
            ) : (
              title
            )}
          </h1>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[13.5px] text-muted">
            <span className="inline-flex items-center gap-1.5">
              <Bot className="size-3.5" aria-hidden />
              {call.agent_id ? (
                <Link to={`/agents/${call.agent_id}`} className="hover:text-ink">
                  {agentName ?? "Agent"}
                </Link>
              ) : (
                "No agent"
              )}
              {call.agent_version ? <span className="font-mono text-[11.5px] text-faint">v{call.agent_version}</span> : null}
            </span>
            <span aria-hidden>·</span>
            <time dateTime={at} title={formatRelative(at)}>
              {formatDateTime(at)}
            </time>
            <span aria-hidden>·</span>
            <span className="font-mono">{formatDuration(call.duration_seconds)}</span>
            <span aria-hidden>·</span>
            <span>{call.direction === "inbound" ? "Inbound" : "Outbound"}</span>
          </div>
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            <Badge tone={state.tone}>{state.label}</Badge>
            {call.outcome ? <Badge tone={outcomeTone(call.outcome)}>{outcomeLabel(call.outcome)}</Badge> : null}
            <Tooltip content={mode.description}>
              <span>
                <Badge tone={mode.tone} className="cursor-help">
                  {mode.label}
                </Badge>
              </span>
            </Tooltip>
            {call.opted_out ? <Badge tone="danger">Opted out</Badge> : null}
          </div>
        </div>
        {call.lead_id ? (
          <Button onClick={() => void navigate(`/leads/${call.lead_id}`)}>
            <ExternalLink /> Open lead
          </Button>
        ) : null}
      </header>

      {call.mode === "simulated" ? (
        <div role="note" className="mb-5 flex items-start gap-2 rounded-md border border-line bg-surface-2/60 px-3 py-2.5 text-[13px] text-ink-2">
          <Info className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
          <span>
            <span className="font-medium">Simulated call.</span> No phone call was placed. The outcome, captured values and transcript were generated by the dry run to exercise the agent, the audience and the pacing rules.
          </span>
        </div>
      ) : null}
      {call.state === "failed" && (call.error_code || call.error_message) ? (
        <div role="alert" className="mb-5 rounded-md border border-danger/30 bg-danger-soft px-3 py-2.5 text-[13px] text-danger">
          <span className="font-medium">Failed{call.error_code ? ` · ${call.error_code}` : ""}.</span> {call.error_message ?? "No further detail was reported."}
        </div>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-5 lg:col-span-2">
          <TranscriptCard call={call} />
          {call.state === "completed" || call.latency_summary ? <ResponseTimesCard callId={call.id} summary={call.latency_summary ?? null} simulated={call.mode === "simulated"} /> : null}
          {call.recording_available ? <RecordingCard call={call} /> : null}
        </div>
        <div className="flex flex-col gap-5">
          <ResultCard call={call} />
          <CapturedCard captured={call.captured} />
          <UsageCard call={call} />
          <EventsCard callId={call.id} />
        </div>
      </div>
    </>
  );
}

function TranscriptCard({ call }: { call: CallDetail }) {
  const o = useOrgId();
  const transcript = useQuery({
    queryKey: qk.callTranscript(o, call.id),
    queryFn: () => unwrap(api.GET("/api/v1/calls/{call_id}/transcript", { params: { path: { call_id: call.id } } })),
    enabled: !!o && call.transcript_available,
    staleTime: 5 * 60_000,
  });
  const terminal = TERMINAL_STATES.has(call.state);

  return (
    <Card>
      <CardHeader
        title="Transcript"
        description={call.transcript_available ? (call.mode === "simulated" ? "Synthetic conversation from the dry run" : "Fetched once from the voice service, then kept with the call") : undefined}
      />
      {!call.transcript_available ? (
        <EmptyState
          icon={<FileText />}
          title={terminal ? "No transcript for this call" : "The transcript appears when the call finishes"}
          description={terminal ? (call.state === "completed" ? "The voice service did not return one." : "The person did not pick up, so there was no conversation.") : undefined}
          className="py-12"
        />
      ) : transcript.isError ? (
        <ErrorState error={transcript.error} onRetry={() => void transcript.refetch()} />
      ) : !transcript.data ? (
        <div className="flex flex-col gap-3 p-4">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className={cn("h-9", i % 2 ? "ml-12" : "mr-12")} />
          ))}
        </div>
      ) : (
        <Transcript text={transcript.data.text} />
      )}
    </Card>
  );
}

function Transcript({ text }: { text: string }) {
  const parsed = parseTranscript(text);
  if (parsed.kind === "plain") {
    return <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap px-4 py-4 font-mono text-[12.5px] leading-relaxed text-ink-2">{text}</pre>;
  }
  return (
    <ol className="flex max-h-[60vh] flex-col gap-2.5 overflow-auto px-4 py-4" aria-label="Conversation">
      {parsed.turns.map((t, i) =>
        t.role === "note" ? (
          <li key={i} className="self-center font-mono text-[11.5px] uppercase tracking-wider text-faint">
            {t.text}
          </li>
        ) : (
          <li key={i} className={cn("flex max-w-[85%] gap-2", t.role === "agent" ? "self-start" : "flex-row-reverse self-end")} data-role={t.role}>
            <span
              className={cn(
                "mt-0.5 grid size-6 shrink-0 place-items-center rounded-full",
                t.role === "agent" ? "bg-accent-soft text-accent-soft-ink" : "bg-surface-2 text-muted",
              )}
              aria-hidden
            >
              {t.role === "agent" ? <Bot className="size-3.5" /> : <User className="size-3.5" />}
            </span>
            <div className={cn("min-w-0 rounded-lg px-3 py-2", t.role === "agent" ? "bg-accent-soft/60" : "bg-surface-2")}>
              <p className="text-[11px] font-semibold uppercase tracking-wider text-muted">{t.role === "agent" ? "Agent" : t.speaker}</p>
              <p className="mt-0.5 whitespace-pre-wrap text-[13px] text-ink">{t.text}</p>
            </div>
          </li>
        ),
      )}
    </ol>
  );
}

/** Custom player: the audio element is never shown; play/pause and a seekable bar use our tokens. */
function RecordingCard({ call }: { call: CallDetail }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);

  useEffect(() => {
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [url]);

  const load = async () => {
    setLoading(true);
    try {
      const blob = await unwrap(api.GET("/api/v1/calls/{call_id}/recording", { params: { path: { call_id: call.id } }, parseAs: "blob" }));
      const next = URL.createObjectURL(blob as Blob);
      setUrl(next);
      return next;
    } catch (e) {
      toast.error(errorMessage(e));
      return null;
    } finally {
      setLoading(false);
    }
  };

  const toggle = async () => {
    const el = audio.current;
    if (!el) return;
    if (!url) {
      const next = await load();
      if (!next) return;
      el.src = next;
    }
    if (el.paused) await el.play().catch(() => toast.error("The browser could not play this recording."));
    else el.pause();
  };

  const seek = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = audio.current;
    if (!el || !duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    el.currentTime = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * duration;
  };

  const progress = duration ? (time / duration) * 100 : 0;
  return (
    <Card>
      <CardHeader title="Recording" description="Streamed through ATLAS; every listen is written to the audit log" />
      <div className="flex items-center gap-3 px-4 py-4">
        <audio
          ref={audio}
          preload="none"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => setDuration(Number.isFinite(e.currentTarget.duration) ? e.currentTarget.duration : (call.duration_seconds ?? 0))}
          onDurationChange={(e) => Number.isFinite(e.currentTarget.duration) && setDuration(e.currentTarget.duration)}
        />
        <Button size="icon" variant="primary" aria-label={playing ? "Pause" : "Play"} loading={loading} onClick={() => void toggle()}>
          {playing ? <Pause /> : <Play />}
        </Button>
        <div className="min-w-0 flex-1">
          <div
            role="slider"
            aria-label="Position"
            aria-valuemin={0}
            aria-valuemax={Math.round(duration)}
            aria-valuenow={Math.round(time)}
            tabIndex={0}
            onClick={seek}
            onKeyDown={(e) => {
              const el = audio.current;
              if (!el) return;
              if (e.key === "ArrowRight") el.currentTime = Math.min(duration, el.currentTime + 5);
              if (e.key === "ArrowLeft") el.currentTime = Math.max(0, el.currentTime - 5);
            }}
            className="group relative h-6 cursor-pointer"
          >
            <div className="absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 overflow-hidden rounded-full bg-surface-2">
              <div className="h-full rounded-full bg-accent transition-[width] duration-150" style={{ width: `${progress}%` }} />
            </div>
          </div>
          <div className="flex justify-between font-mono text-[11.5px] text-muted">
            <span>{formatDuration(time)}</span>
            <span>{formatDuration(duration || call.duration_seconds)}</span>
          </div>
        </div>
        {url ? (
          <Button
            size="icon"
            variant="ghost"
            aria-label="Download recording"
            onClick={() => Object.assign(document.createElement("a"), { href: url, download: `call-${call.id}` }).click()}
          >
            <Download />
          </Button>
        ) : null}
      </div>
    </Card>
  );
}

function Row({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("grid grid-cols-[120px_minmax(0,1fr)] items-start gap-3 px-4 py-2.5", className)}>
      <dt className="text-[12.5px] text-muted">{label}</dt>
      <dd className="min-w-0 text-[13px]">{children}</dd>
    </div>
  );
}

function ResultCard({ call }: { call: CallDetail }) {
  const state = STATE_BADGE[call.state];
  return (
    <Card>
      <CardHeader title="Result" />
      <dl className="divide-y divide-line">
        <Row label="State">
          <Badge tone={state.tone}>{state.label}</Badge>
        </Row>
        <Row label="Outcome">{call.outcome ? <Badge tone={outcomeTone(call.outcome)}>{outcomeLabel(call.outcome)}</Badge> : <span className="text-faint">Not recorded</span>}</Row>
        <Row label="Opt-out" className={call.opted_out ? "bg-danger-soft/40" : undefined}>
          {call.opted_out ? (
            <span className="font-medium text-danger">The person asked not to be contacted. The lead is now marked do-not-call.</span>
          ) : (
            <span className="text-muted">No opt-out request</span>
          )}
        </Row>
        {call.summary ? (
          <Row label="Summary">
            <p className="whitespace-pre-wrap text-ink-2">{call.summary}</p>
          </Row>
        ) : null}
        <Row label="Started">{formatDateTime(call.started_at)}</Row>
        <Row label="Ended">{formatDateTime(call.ended_at)}</Row>
      </dl>
    </Card>
  );
}

function CapturedCard({ captured }: { captured: Record<string, unknown> }) {
  const entries = Object.entries(captured);
  return (
    <Card>
      <CardHeader title="Captured data" description="Values the agent collected during the conversation" />
      {entries.length === 0 ? (
        <p className="px-4 py-5 text-[13px] text-muted">Nothing was captured.</p>
      ) : (
        <dl className="divide-y divide-line">
          {entries.map(([k, v]) => (
            <Row key={k} label={k}>
              <span className="break-words font-mono text-[12.5px] text-ink">{typeof v === "string" ? v : JSON.stringify(v)}</span>
            </Row>
          ))}
        </dl>
      )}
    </Card>
  );
}

function UsageCard({ call }: { call: CallDetail }) {
  const rows = flattenUsage(call.usage);
  return (
    <Card>
      <CardHeader title="Cost & usage" description="As reported by the voice service" />
      <dl className="divide-y divide-line">
        <Row label="Estimated cost">
          <span className={cn("font-mono", call.estimated_cost === null ? "text-faint" : "text-ink")}>{formatCost(call.estimated_cost)}</span>
        </Row>
        <Row label="Duration">
          <span className="font-mono">{formatDuration(call.duration_seconds)}</span>
        </Row>
        {rows.map((r) => (
          <Row key={r.key} label={r.key}>
            <span className="break-words font-mono text-[12.5px] text-ink-2">{r.value}</span>
          </Row>
        ))}
      </dl>
    </Card>
  );
}

function EventsCard({ callId }: { callId: string }) {
  const events = useCallEvents(callId);
  return (
    <Card>
      <CardHeader title="Events" description="How this record reached its current shape" />
      {events.isError ? (
        <ErrorState error={events.error} onRetry={() => void events.refetch()} />
      ) : !events.data ? (
        <div className="flex flex-col gap-2 p-4">
          <Skeleton className="h-6" />
          <Skeleton className="h-6" />
        </div>
      ) : events.data.length === 0 ? (
        <p className="px-4 py-5 text-[13px] text-muted">No events yet.</p>
      ) : (
        <ol className="relative px-4 py-3">
          <span aria-hidden className="absolute bottom-5 left-[23px] top-5 w-px bg-line" />
          {events.data.map((e) => {
            const details = Object.entries(e.data);
            return (
              <li key={e.id} className="relative flex gap-3 pb-3 last:pb-0">
                <span className="relative z-[1] mt-1 size-3.5 shrink-0 rounded-full border-2 border-accent bg-surface" aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                    <p className="text-[13px] font-medium text-ink">{eventLabel(e.type)}</p>
                    <time dateTime={e.occurred_at} title={formatDateTime(e.occurred_at)} className="text-[11.5px] text-faint">
                      {formatRelative(e.occurred_at)}
                    </time>
                  </div>
                  {details.length ? (
                    <p className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 font-mono text-[11.5px] text-muted">
                      {details.map(([k, v]) => (
                        <span key={k}>
                          {k}={typeof v === "string" ? v : JSON.stringify(v)}
                        </span>
                      ))}
                    </p>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}
