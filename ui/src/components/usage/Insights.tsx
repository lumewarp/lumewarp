import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, type UsageQuery, type UsageRow, type UsageTotals } from "../../api";
import { cn } from "../../lib/utils";
import type { UsageLens } from "../../lib/usageMetric";
import { fmtCompact, fmtNumber as n, fmtUsdShort as fmtSpend } from "../Bits";
import { Card } from "../ui/card";
import { fmtMs } from "./format";
import { Sparkline } from "./Sparkline";

type Bucket = "hour" | "day";

const rate = (count: number, requests: number) => (requests ? count / requests : null);
const fmtRate = (value: number | null) => (value === null ? "—" : `${(value * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`);


interface Tile {
  key: string;
  label: string;
  value: string;
  title?: string;
  now: number | null;
  before: number | null;
  /** Up is worse (errors, latency, fallbacks): the delta is drawn as a warning. */
  worse?: boolean;
  /** The delta of a share is in percentage points, not percent. */
  points?: boolean;
  /** One value per bucket over the range; none draws no sparkline. */
  series?: number[] | null;
}

function Delta({ tile }: { tile: Tile }) {
  if (tile.before === null) return <span className="text-xs text-muted-foreground">no earlier data</span>;
  if (tile.now === null) return <span className="text-xs text-muted-foreground">not available</span>;
  const diff = tile.now - tile.before;
  if (diff === 0) return <span className="text-xs text-muted-foreground">no change</span>;
  let text: string;
  if (tile.points) text = `${Math.abs(diff * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })} pt`;
  else if (tile.before === 0) text = "new";
  else text = `${Math.abs((diff / tile.before) * 100).toLocaleString(undefined, { maximumFractionDigits: 0 })}%`;
  const bad = tile.worse && diff > 0;
  return (
    <span className={cn("text-xs tabular-nums", bad ? "text-destructive" : "text-muted-foreground")} title="Change against the previous period of the same length" data-testid="insight-delta" data-direction={diff > 0 ? "up" : "down"}>
      {diff > 0 ? "▲" : "▼"} {text}
    </span>
  );
}

const per = (total: number, count: number) => (count ? total / count : null);
const fmtOptional = (nano: number | null) => (nano === null ? "—" : fmtSpend(nano));

/** Per-bucket sums, zero-filled over the slots. p95 is null unless one row per bucket (a single zone). */
interface Folded {
  requests: number;
  tokens: number;
  cost: number;
  unpriced: number;
  errors: number;
  fallbacks: number;
  p95: number;
}

function fold(rows: UsageRow[], bucket: Bucket, slots: string[], singleZone: boolean): Folded[] {
  const by = new Map<string, Folded>(slots.map((slot) => [slot, { requests: 0, tokens: 0, cost: 0, unpriced: 0, errors: 0, fallbacks: 0, p95: 0 }]));
  for (const row of rows) {
    const key = bucket === "day" ? row.date : row.hour ? new Date(row.hour).toISOString() : null;
    const slot = key ? by.get(key) : undefined;
    if (!slot) continue;
    slot.requests += row.requests;
    slot.tokens += row.total_tokens;
    slot.cost += row.cost_nano_usd;
    slot.unpriced += row.unknown_cost_count;
    slot.errors += row.error_count;
    slot.fallbacks += row.fallback_count;
    if (singleZone) slot.p95 = row.p95_duration_ms ?? 0;
  }
  return slots.map((slot) => by.get(slot)!);
}

const seriesOf = (rows: Folded[], pick: (row: Folded) => number) => rows.map(pick);

function tiles(now: UsageRow | undefined, before: UsageTotals | null, admin: boolean, lens: UsageLens, buckets: Folded[], singleZone: boolean): Tile[] {
  const cur = now ?? ({ requests: 0, total_tokens: 0, cost_nano_usd: 0, unknown_cost_count: 0, error_count: 0, fallback_count: 0, p95_duration_ms: null } as Partial<UsageRow>);
  const requests = cur.requests ?? 0;
  const tokens = cur.total_tokens ?? 0;
  const cost = cur.cost_nano_usd ?? 0;
  if (lens === "cost") {
    // Spend up is not bad news, so these deltas stay neutral; unpriced requests are a gap in the data.
    const avg = per(cost, requests);
    const perMillion = per(cost * 1e6, tokens);
    const unpriced = cur.unknown_cost_count ?? 0;
    return [
      { key: "spend", label: "Spend", series: seriesOf(buckets, (b) => b.cost), value: fmtSpend(cost), now: cost, before: before ? before.cost_nano_usd : null },
      { key: "avg-cost", label: "Avg cost / request", series: seriesOf(buckets, (b) => (b.requests ? b.cost / b.requests : 0)), value: fmtOptional(avg), now: avg, before: before ? per(before.cost_nano_usd, before.requests) : null },
      { key: "cost-per-mtok", label: "Cost / 1M tokens", series: seriesOf(buckets, (b) => (b.tokens ? (b.cost * 1e6) / b.tokens : 0)), value: fmtOptional(perMillion), now: perMillion, before: before ? per(before.cost_nano_usd * 1e6, before.total_tokens) : null },
      { key: "unpriced", label: "Unpriced requests", series: seriesOf(buckets, (b) => b.unpriced), value: fmtCompact(unpriced), title: n(unpriced), now: unpriced, before: before ? before.unknown_cost_count : null, worse: true },
    ];
  }
  const out: Tile[] = [
    { key: "requests", label: "Requests", series: seriesOf(buckets, (b) => b.requests), value: fmtCompact(requests), title: n(requests), now: requests, before: before ? before.requests : null },
    { key: "tokens", label: "Total tokens", series: seriesOf(buckets, (b) => b.tokens), value: fmtCompact(tokens), title: n(tokens), now: tokens, before: before ? before.total_tokens : null },
    { key: "errors", label: "Error rate", series: seriesOf(buckets, (b) => (b.requests ? b.errors / b.requests : 0)), value: fmtRate(rate(cur.error_count ?? 0, requests)), now: rate(cur.error_count ?? 0, requests), before: before ? rate(before.error_count, before.requests) : null, worse: true, points: true },
    { key: "p95", label: "p95 latency", series: singleZone ? seriesOf(buckets, (b) => b.p95) : null, value: fmtMs(cur.p95_duration_ms), now: cur.p95_duration_ms ?? null, before: before?.p95_duration_ms ?? null, worse: true },
  ];
  if (admin) out.push({ key: "fallback", label: "Fallback rate", series: singleZone ? seriesOf(buckets, (b) => (b.requests ? b.fallbacks / b.requests : 0)) : null, value: fmtRate(rate(cur.fallback_count ?? 0, requests)), now: rate(cur.fallback_count ?? 0, requests), before: before ? rate(before.fallback_count, before.requests) : null, worse: true, points: true });
  return out;
}

/**
 * Totals with the change against the previous equal-length range. `filters` are the
 * page's, without a grouping. Fallback rate is routing detail: only an installation admin gets it.
 */
export function Insights({
  filters,
  admin,
  ranged,
  lens,
  bucket,
  slots,
  singleZone,
}: {
  filters: UsageQuery;
  admin: boolean;
  ranged: boolean;
  lens: UsageLens;
  /** The sparkline's bucket; `slots` are its ordered keys (local days, or hour starts as ISO instants). */
  bucket: Bucket;
  slots: string[];
  /** One zone in view: p95 and fallback rate have one row per bucket, so they get a sparkline. */
  singleZone: boolean;
}) {
  const totals = useQuery({ queryKey: ["usage-totals", filters], queryFn: () => api.usageTotals(filters), enabled: ranged, retry: false });
  const trend = useQuery({ queryKey: ["usage-trend", filters, bucket], queryFn: () => api.usage({ ...filters, group_by: bucket }), enabled: ranged, retry: false });
  const keys = slots;
  const buckets = useMemo(() => fold(trend.data ?? [], bucket, keys, singleZone), [trend.data, bucket, keys, singleZone]);

  return (
    <section className="mb-4 grid gap-3" aria-label="Insights" data-testid="usage-insights" data-lens={lens}>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(130px,1fr))] content-start gap-3">
        {totals.error ? (
          <p className="col-span-full text-destructive" role="alert">
            {totals.error.message}
          </p>
        ) : (
          tiles(totals.data?.current, totals.data?.previous ?? null, admin, lens, buckets, singleZone).map((tile) => (
            <Card key={tile.key} className="relative min-h-36 gap-0 overflow-hidden p-3.5" data-testid={`insight-${tile.key}`}>
              {tile.series && !trend.isPending && !trend.error && keys.length > 0 && <Sparkline values={tile.series} />}
              <div className="relative text-muted-foreground">{tile.label}</div>
              <div className="relative text-2xl font-bold whitespace-nowrap tabular-nums" title={tile.title}>
                {totals.isPending ? "…" : tile.value}
              </div>
              {!totals.isPending && (
                <div className="relative">
                  <Delta tile={tile} />
                </div>
              )}
            </Card>
          ))
        )}
      </div>
    </section>
  );
}
