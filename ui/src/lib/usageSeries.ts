import type { UsageRow } from "../api";
import type { UsageMetric } from "./usageMetric";

/** Named series drawn before the tail folds into Other: the palette has eight categorical slots. */
export const HUES = 8;

/** The amount a metric draws from one usage row. */
export function amount(row: UsageRow, metric: UsageMetric): number {
  switch (metric) {
    case "tokens":
      return row.total_tokens;
    case "cost":
      return row.cost_nano_usd;
    case "requests":
      return row.requests;
    case "errors":
      return row.error_count;
  }
}

/** The chart's bucket: hours for a range of at most 48 h, local days beyond. */
export type Bucket = "hour" | "day";

const HOUR_MS = 3_600_000;
/** A slot list is capped where the API's range limit is: 400 days touch at most 401 local dates. */
const MAX_SLOTS = 401;

export const bucketOf = (fromIso: string, toIso: string): Bucket => (Date.parse(toIso) - Date.parse(fromIso) <= 48 * HOUR_MS ? "hour" : "day");

const pad = (n: number) => String(n).padStart(2, "0");
const localDay = (date: Date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;

/**
 * The slots of a range `[fromIso, toIso)` in the browser's zone: local calendar days (`YYYY-MM-DD`) or the
 * instants of each local hour start (`toISOString()`, stepped 1 h on instants so a DST change cannot skip or
 * repeat a slot). Empty for an invalid or inverted range.
 */
export function bucketSlots(fromIso: string, toIso: string, bucket: Bucket): string[] {
  const start = new Date(fromIso);
  const end = Date.parse(toIso);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end) || end <= start.getTime()) return [];
  const out: string[] = [];
  if (bucket === "hour") {
    for (let time = new Date(start.getFullYear(), start.getMonth(), start.getDate(), start.getHours()).getTime(); time < end && out.length < MAX_SLOTS; time += HOUR_MS) out.push(new Date(time).toISOString());
    return out;
  }
  const last = localDay(new Date(end - 1));
  for (let i = 0; out.length < MAX_SLOTS; i++) {
    const day = localDay(new Date(start.getFullYear(), start.getMonth(), start.getDate() + i));
    out.push(day);
    if (day >= last) break;
  }
  return out;
}

/** The slot a usage row falls in: its local `date`, or its `hour` instant normalised to `toISOString()`. */
export function bucketKey(row: UsageRow, bucket: Bucket): string {
  if (bucket === "day") return row.date ?? "";
  return row.hour ? new Date(row.hour).toISOString() : "";
}

export interface Series {
  name: string;
  /** Palette slot 0-7, or null for Other. */
  slot: number | null;
  /** The metric per day (index = day), or a single value in `total` for a non-daily view. */
  cells: number[];
  total: number;
  /** The names folded into Other. */
  members: string[];
  /** A fixed colour (the outcome series); otherwise the palette slot paints it. */
  color?: string;
}

export interface Built {
  days: string[];
  series: Series[];
  /** Per day, over every series. */
  dayTotals: number[];
  /** Series the cost metric cannot draw: every request had no price. */
  unpriced: string[];
  /** Requests counted at $0 in the drawn series. */
  unknown: number;
  /** Requests over every row, drawn or not: what "no usage" has to be about. */
  requests: number;
}

/**
 * One series per name (`labelOf`), one cell per slot (see `bucketSlots`). The top eight by the metric keep a palette slot in rank
 * order; the rest fold into Other. A name whose every cost is unknown is reported, not drawn as zero.
 */
export function buildSeries(rows: UsageRow[] | undefined, days: string[], metric: UsageMetric, labelOf: (row: UsageRow) => string, bucket: Bucket = "day"): Built {
  const index = new Map(days.map((day, at) => [day, at]));
  const byName = new Map<string, { cells: number[]; total: number; priced: boolean; unknown: number }>();
  let requests = 0;
  for (const row of rows ?? []) {
    const at = index.get(bucketKey(row, bucket));
    if (at === undefined) continue;
    const name = labelOf(row);
    let entry = byName.get(name);
    if (!entry) {
      entry = { cells: days.map(() => 0), total: 0, priced: false, unknown: 0 };
      byName.set(name, entry);
    }
    const value = amount(row, metric);
    entry.cells[at] += value;
    entry.total += value;
    entry.priced ||= row.requests - row.unknown_cost_count > 0;
    entry.unknown += row.unknown_cost_count;
    requests += row.requests;
  }
  const names = [...byName.keys()];
  const unpriced = metric === "cost" ? names.filter((name) => !byName.get(name)!.priced && byName.get(name)!.unknown > 0).sort() : [];
  const drawn = names.filter((name) => !unpriced.includes(name));
  const ranked = [...drawn].sort((a, b) => byName.get(b)!.total - byName.get(a)!.total || a.localeCompare(b));
  const top = ranked.slice(0, HUES);
  const folded = ranked.slice(HUES);
  const series: Series[] = top.map((name, slot) => ({ name, slot, cells: byName.get(name)!.cells, total: byName.get(name)!.total, members: [] }));
  if (folded.length) {
    series.push({
      name: "Other",
      slot: null,
      cells: days.map((_, at) => folded.reduce((sum, name) => sum + byName.get(name)!.cells[at], 0)),
      total: folded.reduce((sum, name) => sum + byName.get(name)!.total, 0),
      members: folded,
    });
  }
  return {
    days,
    series,
    dayTotals: days.map((_, at) => series.reduce((sum, s) => sum + s.cells[at], 0)),
    unpriced,
    unknown: metric === "cost" ? drawn.reduce((sum, name) => sum + byName.get(name)!.unknown, 0) : 0,
    requests,
  };
}

/**
 * The Errors metric's chart: two series per slot over every row whatever its name, `Success` (requests without an
 * error) and `Error` (`error_count`), in fixed success and destructive colours.
 */
export function buildOutcomeSeries(rows: UsageRow[] | undefined, days: string[], bucket: Bucket = "day"): Built {
  const index = new Map(days.map((day, at) => [day, at]));
  const ok = days.map(() => 0);
  const failed = days.map(() => 0);
  let requests = 0;
  for (const row of rows ?? []) {
    const at = index.get(bucketKey(row, bucket));
    if (at === undefined) continue;
    // error_count never exceeds requests (both come from the same events); the clamp only keeps a bar from going negative.
    ok[at] += Math.max(0, row.requests - row.error_count);
    failed[at] += row.error_count;
    requests += row.requests;
  }
  const sum = (cells: number[]) => cells.reduce((total, value) => total + value, 0);
  const series: Series[] = [
    { name: "Success", slot: 0, cells: ok, total: sum(ok), members: [], color: "var(--success)" },
    { name: "Error", slot: 1, cells: failed, total: sum(failed), members: [], color: "var(--destructive)" },
  ];
  return { days, series, dayTotals: days.map((_, at) => ok[at] + failed[at]), unpriced: [], unknown: 0, requests };
}

/** A series colour: the palette tokens have light and dark values of their own. */
export const paint = (slot: number | null) => (slot === null ? "var(--series-other)" : `var(--series-${slot + 1})`);

/** Chart-share slices of a plain label → value list: the top eight and the rest as Other. */
export function foldShares(items: { name: string; value: number }[]): Series[] {
  const ranked = items.filter((item) => item.value > 0).sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
  const out: Series[] = ranked.slice(0, HUES).map((item, slot) => ({ name: item.name, slot, cells: [item.value], total: item.value, members: [] }));
  const rest = ranked.slice(HUES);
  if (rest.length) out.push({ name: "Other", slot: null, cells: [], total: rest.reduce((s, i) => s + i.value, 0), members: rest.map((i) => i.name) });
  return out;
}
