const dateFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  hour: "numeric",
  minute: "2-digit",
});
const relFmt = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const numFmt = new Intl.NumberFormat();
const pctFmt = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 });

export const formatDate = (iso: string | null | undefined): string => (iso ? dateFmt.format(new Date(iso)) : "—");
export const formatDateTime = (iso: string | null | undefined): string =>
  iso ? dateTimeFmt.format(new Date(iso)) : "—";
export const formatNumber = (n: number): string => numFmt.format(n);
/** `null` means "not computable" (e.g. no denominator), which is different from 0%. */
export const formatPercent = (ratio: number | null | undefined): string =>
  ratio === null || ratio === undefined ? "—" : pctFmt.format(ratio);

const pad2 = (n: number) => String(n).padStart(2, "0");

/** Call length as `m:ss` (or `h:mm:ss`); `null` = not known, which is different from 0:00. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
}

const costFmt = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });

/** Cost estimates as reported by the voice service; `null` = no estimate, not free. */
export const formatCost = (amount: number | null | undefined): string =>
  amount === null || amount === undefined ? "—" : costFmt.format(amount);

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31_536_000],
  ["month", 2_592_000],
  ["week", 604_800],
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = (new Date(iso).getTime() - now) / 1000;
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return relFmt.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

export function fullName(first: string, last?: string | null): string {
  return last ? `${first} ${last}` : first;
}
