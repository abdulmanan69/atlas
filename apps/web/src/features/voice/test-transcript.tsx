import { ArrowDown, BookOpen, Gauge, Loader2, Zap } from "lucide-react";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

import { Tooltip } from "@/components/ui/primitives";
import { formatMs, type SummaryTone } from "@/features/calls/latency-model";
import { SpeechText } from "@/features/calls/speech-text";
import {
  delayBadge,
  timingBreakdown,
  toolLabel,
  type ReplyTiming,
  type TestChannel,
  type TranscriptItem,
} from "@/features/voice/test-transcript-model";
import { cn } from "@/lib/utils";

const TONE: Record<SummaryTone, string> = {
  good: "text-success",
  warn: "text-warn",
  bad: "text-danger",
  neutral: "text-muted",
};

/** Pixels from the bottom that still count as "following the conversation". */
const STICK_PX = 32;

function Delay({ timing, channel }: { timing: ReplyTiming; channel: TestChannel }) {
  const badge = delayBadge(timing, channel);
  if (!badge) return null;
  const rows = timingBreakdown(timing);
  const explain =
    channel === "text"
      ? "From sending your message to the agent's full reply."
      : badge.label === "Response"
        ? "From when you stopped talking to when the agent started speaking. Aim for under 800 ms; over 1.5 s feels slow."
        : "Time to the first output of this reply.";
  return (
    <Tooltip
      content={
        <span className="flex flex-col gap-0.5 py-0.5">
          <span>{explain}</span>
          {rows.map((r) => (
            <span key={r.label} className="flex justify-between gap-3 font-mono text-[11.5px]">
              <span className="font-sans">{r.label}</span>
              {formatMs(r.ms)}
            </span>
          ))}
        </span>
      }
    >
      <span
        tabIndex={0}
        className={cn("inline-flex items-center gap-1 rounded px-1 text-[11.5px] font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40", TONE[badge.tone])}
        aria-label={`${badge.label} ${formatMs(badge.ms)}${rows.map((r) => `, ${r.label} ${formatMs(r.ms)}`).join("")}`}
      >
        <Gauge className="size-3" aria-hidden />
        {badge.label} <span className="font-mono">{formatMs(badge.ms)}</span>
      </span>
    </Tooltip>
  );
}

function Typing({ label = "Agent is speaking" }: { label?: string }) {
  return (
    <span className="inline-flex gap-1 py-1" role="img" aria-label={label}>
      {[0, 1, 2].map((i) => (
        <span key={i} className="size-1.5 animate-pulse rounded-full bg-muted" style={{ animationDelay: `${i * 150}ms` }} />
      ))}
    </span>
  );
}

function Item({ item, channel }: { item: TranscriptItem; channel: TestChannel }): ReactNode {
  switch (item.kind) {
    case "user":
      return (
        <div className="flex justify-end">
          <p className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-accent px-3 py-2 text-[13px] leading-relaxed text-accent-ink">
            {item.text}
            {item.interim ? <span className={cn("italic opacity-70", item.text && "ml-1")}>{item.interim}</span> : null}
          </p>
        </div>
      );
    case "agent":
      return (
        <div className="flex flex-col items-start gap-0.5">
          <Delay timing={item.timing} channel={channel} />
          <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-bl-md border border-line bg-surface-2 px-3 py-2 text-[13px] leading-relaxed text-ink">
            {item.text ? <SpeechText parts={item.parts} /> : item.speaking ? <Typing /> : null}
            {item.text && item.speaking ? <span className="ml-1.5 text-[10.5px] italic text-muted">speaking…</span> : null}
          </div>
        </div>
      );
    case "tool":
      return (
        <div className="flex items-center gap-2 self-start rounded-md border border-line bg-surface px-2.5 py-1.5 text-[12px] text-ink-2">
          {item.knowledge ? <BookOpen className="size-3.5 text-accent" aria-hidden /> : <Zap className="size-3.5 text-warn" aria-hidden />}
          <span className="font-medium text-ink">{toolLabel(item.name, item.knowledge)}</span>
          {item.status === "running" ? (
            <span className="inline-flex items-center gap-1 text-muted">
              <Loader2 className="size-3 animate-spin" aria-hidden /> running
            </span>
          ) : item.durationMs !== null ? (
            <span className="font-mono text-muted">{formatMs(item.durationMs)}</span>
          ) : (
            <span className="text-muted">done</span>
          )}
        </div>
      );
    case "step":
      return (
        <div className="flex items-center gap-2 py-0.5 text-[11px] font-medium uppercase tracking-wide text-faint" role="separator" aria-label={`Step: ${item.name}`}>
          <span className="h-px flex-1 bg-line" />
          <span className="normal-case tracking-normal text-muted">Step · {item.name}</span>
          <span className="h-px flex-1 bg-line" />
        </div>
      );
    case "notice":
      return <p className={cn("text-center text-[12px]", item.tone === "error" ? "text-danger" : "text-muted")}>{item.text}</p>;
  }
}

/**
 * The live conversation: caller and agent bubbles, each reply's delay (hover for the
 * breakdown), tool cards and step dividers. Follows new messages until the reader
 * scrolls up; then a button jumps back to the latest.
 */
export function TestTranscript({
  items,
  channel,
  empty,
  thinking = false,
  className,
}: {
  items: TranscriptItem[];
  channel: TestChannel;
  empty: ReactNode;
  /** Show the agent composing a reply after the last message. */
  thinking?: boolean;
  className?: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [behind, setBehind] = useState(false);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    if (stick.current) el.scrollTop = el.scrollHeight;
    else setBehind(true);
  }, [items, thinking]);

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_PX;
    if (stick.current) setBehind(false);
  };

  const jump = () => {
    const el = box.current;
    if (!el) return;
    stick.current = true;
    setBehind(false);
    el.scrollTop = el.scrollHeight;
  };

  return (
    <div className={cn("relative", className)}>
      <div
        ref={box}
        onScroll={onScroll}
        role="log"
        aria-live="polite"
        aria-label="Conversation"
        className="flex h-72 flex-col gap-2.5 overflow-y-auto rounded-lg border border-line bg-canvas/60 p-3"
      >
        {items.length === 0 && !thinking ? (
          <div className="m-auto max-w-64 text-center text-[12.5px] text-muted">{empty}</div>
        ) : (
          items.map((item) => <Item key={item.id} item={item} channel={channel} />)
        )}
        {thinking ? (
          <div className="self-start rounded-2xl rounded-bl-md border border-line bg-surface-2 px-3 py-1.5">
            <Typing label="Agent is replying" />
          </div>
        ) : null}
      </div>
      {behind ? (
        <button
          type="button"
          onClick={jump}
          className="absolute bottom-2 left-1/2 inline-flex -translate-x-1/2 items-center gap-1 rounded-full border border-line-strong bg-surface px-2.5 py-1 text-[12px] font-medium text-ink shadow-pop hover:bg-surface-2"
        >
          <ArrowDown className="size-3.5" aria-hidden /> Latest
        </button>
      ) : null}
    </div>
  );
}
