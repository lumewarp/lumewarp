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

export interface Series {
  name: string;
  /** Palette slot 0-7, or null for Other. */
  slot: number | null;
  /** The metric per day (index = day), or a single value in `total` for a non-daily view. */
  cells: number[];
  total: number;
  /** The names folded into Other. */
  members: string[];
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
 * One series per name (`labelOf`), one cell per UTC day. The top eight by the metric keep a palette slot in rank
 * order; the rest fold into Other. A name whose every cost is unknown is reported, not drawn as zero.
 */
export function buildSeries(rows: UsageRow[] | undefined, days: string[], metric: UsageMetric, labelOf: (row: UsageRow) => string): Built {
  const index = new Map(days.map((day, at) => [day, at]));
  const byName = new Map<string, { cells: number[]; total: number; priced: boolean; unknown: number }>();
  let requests = 0;
  for (const row of rows ?? []) {
    const at = row.date ? index.get(row.date) : undefined;
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
