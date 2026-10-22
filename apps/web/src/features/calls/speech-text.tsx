/** A transcript line with its sound cues shown as small muted chips ("laughs"). */
import { Fragment } from "react";

import type { SpeechPart } from "@/features/calls/sound-cues";

export function SoundCue({ label }: { label: string }) {
  return (
    <span data-cue className="mx-0.5 inline-flex h-[18px] items-center rounded-full border border-line bg-surface px-1.5 align-[1px] text-[11px] italic leading-none text-muted" title="A sound the voice made">
      <span className="sr-only">(</span>
      {label}
      <span className="sr-only">)</span>
    </span>
  );
}

export function SpeechText({ parts }: { parts: SpeechPart[] }) {
  return (
    <>
      {parts.map((p, i) => (p.kind === "cue" ? <SoundCue key={i} label={p.label} /> : <Fragment key={i}>{p.text}</Fragment>))}
    </>
  );
}
