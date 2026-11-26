import type { Schemas } from "../api";

/** The badge tone of a `lumewarp_status` label; a free-text state the UI does not know stays neutral. */
export type StateTone = "danger" | "warn" | "muted";
export const stateTone = (state: string | null | undefined): StateTone => {
  const value = (state ?? "").toLowerCase();
  if (value === "down") return "danger";
  if (value === "congested" || value === "degraded") return "warn";
  return "muted";
};

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];

/** "5 minutes ago": the largest whole unit, in the reader's language. */
export function relativeTime(iso: string, now = Date.now()): string {
  const seconds = Math.round((Date.parse(iso) - now) / 1000);
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return format.format(Math.trunc(seconds / size), unit);
  }
  return format.format(Math.trunc(seconds), "second");
}

/** A span as "2 h 5 min", "5 min 3 s" or "42 s". */
export function fmtSpan(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3_600);
  const minutes = Math.floor((total % 3_600) / 60);
  const rest = total % 60;
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  if (minutes) return `${minutes} min ${rest} s`;
  return `${rest} s`;
}

/** An incident's duration: the server's value while resolved, counted up from its start while firing. */
export function incidentSeconds(incident: Pick<Schemas["IncidentOut"], "status" | "starts_at" | "duration_seconds">, now = Date.now()): number {
  return incident.status === "firing" ? Math.max(incident.duration_seconds, Math.floor((now - Date.parse(incident.starts_at)) / 1000)) : incident.duration_seconds;
}

/** Worst first: the banner leads with the worst state, and an incident shows its worst member's. */
const STATE_RANK: Record<string, number> = { down: 0, congested: 1, degraded: 2 };
export const stateRank = (state: string | null | undefined) => STATE_RANK[(state ?? "").toLowerCase()] ?? 3;

/** A duration without seconds once it is a minute or longer: "1 h 15 min", "4 min", "42 s". */
export function fmtDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3_600);
  const minutes = Math.floor((total % 3_600) / 60);
  if (days) return hours ? `${days} d ${hours} h` : `${days} d`;
  if (hours) return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  if (minutes) return `${minutes} min`;
  return `${total} s`;
}

const two = (n: number) => String(n).padStart(2, "0");
/** "22:42", local time. */
export const clock = (iso: string) => {
  const date = new Date(iso);
  return `${two(date.getHours())}:${two(date.getMinutes())}`;
};
const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

/** "Today 22:42", "Yesterday 08:10" or "Nov 23 19:47", in the reader's time zone. */
export function fmtStarted(iso: string, now = Date.now()): string {
  const date = new Date(iso);
  const today = new Date(now);
  const yesterday = new Date(now);
  yesterday.setDate(today.getDate() - 1);
  if (sameDay(date, today)) return `Today ${clock(iso)}`;
  if (sameDay(date, yesterday)) return `Yesterday ${clock(iso)}`;
  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(date.getFullYear() === today.getFullYear() ? {} : { year: "numeric" }) });
  return `${day} ${clock(iso)}`;
}

/** The alert name without the shipped rules' `Lumewarp` prefix. */
export const shortAlertname = (name: string) => (name.startsWith("Lumewarp") && name.length > 8 ? name.slice(8) : name);

/** "2 gateways" when several gateways reported the outage, "3 alerts" for several alerts without one, else null. */
export function reporters(incident: Pick<Schemas["IncidentOut"], "gateways" | "alert_count">): string | null {
  if (incident.gateways.length > 1) return `${incident.gateways.length} gateways`;
  if (incident.alert_count > 1) return `${incident.alert_count} alerts`;
  return null;
}

/**
 * The median of a histogram of `{attempts, count}` rows, and its largest bucket; null without requests. Requests
 * refused before any upstream attempt (`attempts` 0) are left out.
 */
export function attemptsSummary(rows: { attempts: number; count: number }[]): { median: number; max: number } | null {
  const sorted = [...rows].filter((row) => row.count > 0 && row.attempts > 0).sort((a, b) => a.attempts - b.attempts);
  const total = sorted.reduce((sum, row) => sum + row.count, 0);
  if (!total) return null;
  let seen = 0;
  let median = sorted[0].attempts;
  for (const row of sorted) {
    seen += row.count;
    if (seen * 2 >= total) {
      median = row.attempts;
      break;
    }
  }
  return { median, max: sorted[sorted.length - 1].attempts };
}

type MinuteRow = Schemas["IncidentMinuteOut"];

/** The longest traffic window is 24 hours, so at most this many minute buckets. */
const MAX_MINUTES = 24 * 60 + 1;

/**
 * One row per minute from `start` to `end` (the traffic window), so a chart's category axis is linear in time. A minute
 * without traffic counts zero and has no TTFT or context value. Rows are keyed by the minute's ISO timestamp.
 */
export function fillMinutes(rows: MinuteRow[], start: string, end: string): MinuteRow[] {
  const floor = (ms: number) => Math.floor(ms / 60_000) * 60_000;
  const byMinute = new Map(rows.map((row) => [floor(Date.parse(row.minute)), row]));
  const from = floor(Date.parse(start));
  const to = Math.min(floor(Date.parse(end)), from + (MAX_MINUTES - 1) * 60_000);
  const filled: MinuteRow[] = [];
  for (let at = from; at <= to; at += 60_000) {
    const row = byMinute.get(at);
    const minute = new Date(at).toISOString();
    filled.push(
      row
        ? { ...row, minute }
        : { minute, requests: 0, failed: 0, fallback: 0, ttft_avg_ms: null, ttft_p95_ms: null, context_avg_tokens: null, context_p95_tokens: null },
    );
  }
  return filled;
}
