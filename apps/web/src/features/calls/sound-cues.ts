/**
 * Sound cues in agent replies. An expressive voice laughs or sighs where a reply says
 * "[laugh]" or "[sigh]" (the agent's Natural sounds setting), so transcripts show these as
 * small chips ("laughs") instead of raw brackets. Only the sounds the expressive voice server
 * knows are recognised, in the spellings language models write; other bracketed text, such
 * as "[SIMULATED CALL]", stays as written.
 */

export type SpeechPart = { kind: "text"; text: string } | { kind: "cue"; tag: string; label: string };

/** Sound → how a transcript says it. The sounds of infra/docker/expressive-tts. */
const CUE_LABELS = new Map<string, string>([
  ["laugh", "laughs"],
  ["chuckle", "chuckles"],
  ["sigh", "sighs"],
  ["gasp", "gasps"],
  ["cough", "coughs"],
  ["clear throat", "clears throat"],
  ["groan", "groans"],
  ["sniff", "sniffs"],
  ["shush", "shushes"],
]);

/** Other spellings of the same sounds, read the way the expressive voice server reads them. */
const ALIASES = new Map<string, string>([
  ["laughs", "laugh"],
  ["laughing", "laugh"],
  ["laughter", "laugh"],
  ["chuckles", "chuckle"],
  ["chuckling", "chuckle"],
  ["sighs", "sigh"],
  ["sighing", "sigh"],
  ["gasps", "gasp"],
  ["gasping", "gasp"],
  ["coughs", "cough"],
  ["coughing", "cough"],
  ["clears throat", "clear throat"],
  ["clearing throat", "clear throat"],
  ["groans", "groan"],
  ["groaning", "groan"],
  ["sniffs", "sniff"],
  ["sniffing", "sniff"],
  ["sniffle", "sniff"],
  ["sniffles", "sniff"],
  ["shushes", "shush"],
  ["shh", "shush"],
  ["shhh", "shush"],
]);

const BRACKETED = /\[([^[\]\n]{1,30})\]/g;

/** The sound a bracketed text names ("Laughs" → "laugh"), or null when it is not one. */
export function cueTag(inner: string): string | null {
  const name = inner.trim().replace(/\s+/g, " ").toLowerCase();
  const tag = ALIASES.get(name) ?? name;
  return CUE_LABELS.has(tag) ? tag : null;
}

/** "laugh" → "laughs"; a sound this list does not know keeps its own name. */
export function cueLabel(tag: string): string {
  return CUE_LABELS.get(tag) ?? tag;
}

/** Text and sound cues in order. Text without cues is one text part (none when empty). */
export function splitSoundCues(text: string): SpeechPart[] {
  const parts: SpeechPart[] = [];
  let last = 0;
  for (const m of text.matchAll(BRACKETED)) {
    const tag = cueTag(m[1]!);
    if (!tag) continue;
    if (m.index > last) parts.push({ kind: "text", text: text.slice(last, m.index) });
    parts.push({ kind: "cue", tag, label: cueLabel(tag) });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ kind: "text", text: text.slice(last) });
  return parts;
}

/** Sounds named in a badge, most recognisable first. */
const SUMMARY_ORDER = ["laugh", "sigh", "chuckle", "gasp", "clear throat", "cough", "groan", "sniff", "shush"];

/** "Laughs, sighs": what an expressive voice can do, in two sounds at most. */
export function soundSummary(tags: readonly string[]): string {
  const known = SUMMARY_ORDER.filter((t) => tags.includes(t)).slice(0, 2);
  if (!known.length) return "Makes sounds";
  const text = known.map(cueLabel).join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
