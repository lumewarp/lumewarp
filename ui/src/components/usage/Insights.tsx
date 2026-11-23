import { useQuery } from "@tanstack/react-query";

import { api, type UsageQuery, type UsageRow, type UsageTotals } from "../../api";
import { cn } from "../../lib/utils";
import type { UsageLens } from "../../lib/usageMetric";
import { fmtCompact, fmtNumber as n, fmtUsdShort as fmtSpend } from "../Bits";
import { Card } from "../ui/card";
import { fmtMs } from "./format";

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
    <span className={cn("text-xs tabular-nums", bad ? "text-destructive" : "text-muted-foreground")} data-testid="insight-delta" data-direction={diff > 0 ? "up" : "down"}>
      {diff > 0 ? "▲" : "▼"} {text} <span className="text-muted-foreground">vs previous</span>
    </span>
  );
}

const per = (total: number, count: number) => (count ? total / count : null);
const fmtOptional = (nano: number | null) => (nano === null ? "—" : fmtSpend(nano));

function tiles(now: UsageRow | undefined, before: UsageTotals | null, admin: boolean, lens: UsageLens): Tile[] {
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
      { key: "spend", label: "Spend", value: fmtSpend(cost), now: cost, before: before ? before.cost_nano_usd : null },
      { key: "avg-cost", label: "Avg cost / request", value: fmtOptional(avg), now: avg, before: before ? per(before.cost_nano_usd, before.requests) : null },
      { key: "cost-per-mtok", label: "Cost / 1M tokens", value: fmtOptional(perMillion), now: perMillion, before: before ? per(before.cost_nano_usd * 1e6, before.total_tokens) : null },
      { key: "unpriced", label: "Unpriced requests", value: fmtCompact(unpriced), title: n(unpriced), now: unpriced, before: before ? before.unknown_cost_count : null, worse: true },
    ];
  }
  const out: Tile[] = [
    { key: "requests", label: "Requests", value: fmtCompact(requests), title: n(requests), now: requests, before: before ? before.requests : null },
    { key: "tokens", label: "Total tokens", value: fmtCompact(tokens), title: n(tokens), now: tokens, before: before ? before.total_tokens : null },
    { key: "errors", label: "Error rate", value: fmtRate(rate(cur.error_count ?? 0, requests)), now: rate(cur.error_count ?? 0, requests), before: before ? rate(before.error_count, before.requests) : null, worse: true, points: true },
    { key: "p95", label: "p95 latency", value: fmtMs(cur.p95_duration_ms), now: cur.p95_duration_ms ?? null, before: before?.p95_duration_ms ?? null, worse: true },
  ];
  if (admin) out.push({ key: "fallback", label: "Fallback rate", value: fmtRate(rate(cur.fallback_count ?? 0, requests)), now: rate(cur.fallback_count ?? 0, requests), before: before ? rate(before.fallback_count, before.requests) : null, worse: true, points: true });
  return out;
}

/**
 * Totals with the change against the previous equal-length range. `filters` are the
 * page's, without a grouping. Fallback rate is routing detail: only an installation admin gets it.
 */
export function Insights({ filters, admin, ranged, lens }: { filters: UsageQuery; admin: boolean; ranged: boolean; lens: UsageLens }) {
  const totals = useQuery({ queryKey: ["usage-totals", filters], queryFn: () => api.usageTotals(filters), enabled: ranged, retry: false });

  return (
    <section className="mb-4 grid gap-3" aria-label="Insights" data-testid="usage-insights" data-lens={lens}>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(130px,1fr))] content-start gap-3">
        {totals.error ? (
          <p className="col-span-full text-destructive" role="alert">
            {totals.error.message}
          </p>
        ) : (
          tiles(totals.data?.current, totals.data?.previous ?? null, admin, lens).map((tile) => (
            <Card key={tile.key} className="gap-0 p-3.5" data-testid={`insight-${tile.key}`}>
              <div className="text-muted-foreground">{tile.label}</div>
              <div className="text-2xl font-bold whitespace-nowrap tabular-nums" title={tile.title}>
                {totals.isPending ? "…" : tile.value}
              </div>
              {!totals.isPending && <Delta tile={tile} />}
            </Card>
          ))
        )}
      </div>
    </section>
  );
}
