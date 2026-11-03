import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";

import type { UsageRow } from "../api";
import { daysBetween } from "../lib/days";
import { METRIC_LABEL, type UsageMetric } from "../lib/usageMetric";
import { cn } from "../lib/utils";
import { fmtNumber, fmtUsd, nanoToUsd } from "./Bits";
import { Alert, AlertDescription } from "./ui/alert";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "./ui/card";
import { Skeleton } from "./ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./ui/table";

/*
 * Usage over time: one stacked bar per UTC day, one series per model, hand-drawn SVG (no chart dependency).
 * Rows come from `GET /usage` with `group_by=day,model`; the API buckets by UTC day and has no week or month
 * grouping, so a long range stays daily and scrolls inside the card rather than coarsening.
 */

/** The 8 hues a series can take; the tail beyond them folds into a neutral Other. */
const HUES = 8;
const SERIES_FILL = [
  "fill-series-1",
  "fill-series-2",
  "fill-series-3",
  "fill-series-4",
  "fill-series-5",
  "fill-series-6",
  "fill-series-7",
  "fill-series-8",
] as const;
const SERIES_DOT = [
  "bg-series-1",
  "bg-series-2",
  "bg-series-3",
  "bg-series-4",
  "bg-series-5",
  "bg-series-6",
  "bg-series-7",
  "bg-series-8",
] as const;

/** Geometry, in CSS pixels: the SVG is laid out at its natural size, so text never scales. */
const PLOT_H = 170;
const PAD = { top: 12, right: 10, bottom: 22, left: 62 };
const MIN_SLOT = 22;
const SEGMENT_GAP = 2;
const BAR_MAX = 26;

/** The hue each model currently draws with, for the life of the page. */
const hues = new Map<string, number>();

/**
 * Hue per model name. A model keeps the hue it already has, so a filter that drops series does not repaint the
 * survivors and the metric toggle repaints nothing; a newcomer takes its hashed hue when that is free and the
 * lowest free hue otherwise, so two drawn series never share a colour while there are eight or fewer. A hue is
 * not reserved by a model the chart does not draw (folded into Other, or unpriced), which would otherwise hold
 * a colour nobody can see.
 */
function hueMap(names: string[]) {
  const taken = new Set<number>();
  const assigned = new Map<string, number>();
  for (const name of names) {
    const hue = hues.get(name);
    if (hue !== undefined && !taken.has(hue)) {
      assigned.set(name, hue);
      taken.add(hue);
    }
  }
  for (const name of names) {
    if (assigned.has(name)) continue;
    let hash = 0;
    for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
    let hue = hash % HUES;
    for (let step = 0; step < HUES && taken.has(hue); step++) hue = (hue + 1) % HUES;
    assigned.set(name, hue);
    taken.add(hue);
  }
  hues.clear();
  for (const [name, hue] of assigned) hues.set(name, hue);
  return assigned;
}

interface ModelTotals {
  tokens: Map<string, number>;
  costs: Map<string, number>;
  totalTokens: number;
  totalCost: number;
  /** At least one request had a price, so the cost is known for part of it. */
  priced: boolean;
  /** Requests whose cost is unknown: no price was configured when they were recorded. */
  unknown: number;
}

interface Bucket {
  /** Legend label: a model name, or "Other" for the folded tail. */
  name: string;
  /** Index into the series tokens, or null for the neutral Other. */
  hue: number | null;
  values: Map<string, number>;
  /** For Other: the folded models, so the tooltip can name them. */
  members: { name: string; values: Map<string, number> }[];
}

function aggregate(rows: UsageRow[] | undefined) {
  const models = new Map<string, ModelTotals>();
  for (const row of rows ?? []) {
    if (!row.date) continue;
    const name = row.model || "—";
    let model = models.get(name);
    if (!model) {
      model = { tokens: new Map(), costs: new Map(), totalTokens: 0, totalCost: 0, priced: false, unknown: 0 };
      models.set(name, model);
    }
    model.tokens.set(row.date, (model.tokens.get(row.date) ?? 0) + row.total_tokens);
    model.costs.set(row.date, (model.costs.get(row.date) ?? 0) + row.cost_nano_usd);
    model.totalTokens += row.total_tokens;
    model.totalCost += row.cost_nano_usd;
    if (row.cost_nano_usd > 0) model.priced = true;
    model.unknown += row.unknown_cost_count;
  }
  return models;
}

/**
 * Series and their per-day values for one metric. `UsageRowOut` gives `cost_nano_usd` (the priced part) and
 * `unknown_cost_count`: a model whose requests all have an unknown cost has nothing to draw, so it is reported
 * instead of being drawn as a zero bar. Rows are merged by model *name*, which is what the legend shows.
 */
function buildBuckets(rows: UsageRow[] | undefined, metric: UsageMetric) {
  const models = aggregate(rows);
  const names = [...models.keys()].sort((a, b) => a.localeCompare(b));
  const totalOf = (name: string) => {
    const model = models.get(name)!;
    return metric === "tokens" ? model.totalTokens : model.totalCost;
  };
  const unpriced = metric === "cost" ? names.filter((name) => !models.get(name)!.priced && models.get(name)!.unknown > 0) : [];
  const drawn = names.filter((name) => !unpriced.includes(name));
  const ranked = [...drawn].sort((a, b) => totalOf(b) - totalOf(a) || a.localeCompare(b));
  const kept = new Set(ranked.slice(0, HUES));
  const folded = drawn.filter((name) => !kept.has(name));
  const valuesOf = (name: string) => (metric === "tokens" ? models.get(name)!.tokens : models.get(name)!.costs);

  const named = [...kept].sort((a, b) => a.localeCompare(b));
  const assigned = hueMap(named);
  const buckets: Bucket[] = named.map((name) => ({ name, hue: assigned.get(name) ?? 0, values: valuesOf(name), members: [] }));
  if (folded.length) {
    const values = new Map<string, number>();
    for (const name of folded) for (const [day, value] of valuesOf(name)) values.set(day, (values.get(day) ?? 0) + value);
    buckets.push({ name: "Other", hue: null, values, members: folded.map((name) => ({ name, values: valuesOf(name) })) });
  }
  // Unknown cost is only a cost-mode problem; in token mode those requests have their tokens like any other.
  const unknownRequests = metric === "cost" ? drawn.reduce((sum, name) => sum + models.get(name)!.unknown, 0) : 0;
  return { buckets, unpriced, unknownRequests };
}

/** Round tick values: 0 … max in 4 to 6 steps, on 1/2/2.5/5×10ⁿ. */
function axisFor(peak: number) {
  const top = peak > 0 ? peak : 1;
  const rough = top / 4;
  const exp = 10 ** Math.floor(Math.log10(rough));
  const f = rough / exp;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * exp;
  const max = Math.ceil(top / step) * step;
  const ticks: number[] = [];
  for (let value = 0; value <= max + step / 2; value += step) ticks.push(value);
  return { max, step, ticks };
}

const round1 = (value: number) => (Math.round(value * 10) / 10).toString();

/**
 * Axis labels, short: the tooltip and the table carry the exact helpers' formatting. Money keeps as many
 * decimals as the tick step needs and no more, so the gutter stays narrow.
 */
function tickLabel(value: number, metric: UsageMetric, step: number) {
  if (metric === "tokens") {
    if (value >= 1e9) return `${round1(value / 1e9)}B`;
    if (value >= 1e6) return `${round1(value / 1e6)}M`;
    if (value >= 1e4) return `${round1(value / 1e3)}k`;
    return fmtNumber(Math.round(value));
  }
  const usd = nanoToUsd(value) ?? 0;
  if (usd === 0) return "$0";
  if (usd >= 1000) return `$${round1(usd / 1000)}k`;
  const digits = Math.min(5, Math.max(2, Math.ceil(-Math.log10((nanoToUsd(step) ?? 0) || 1))));
  return `$${usd.toFixed(digits)}`;
}

const exact = (value: number, metric: UsageMetric) => (metric === "tokens" ? `${fmtNumber(value)} tokens` : fmtUsd(value));
/** The same numbers without the unit: a table cell says it in its header. */
const number = (value: number, metric: UsageMetric) => (metric === "tokens" ? fmtNumber(value) : fmtUsd(value));

/** Top-rounded bar end: rounded data-ends at the top only, a plain edge at the bottom. */
function topRounded(x: number, y: number, w: number, h: number) {
  const r = Math.max(0, Math.min(3, w / 2, h));
  if (r === 0) return `M${x} ${y}h${w}v${h}h${-w}z`;
  return `M${x} ${y + r}a${r} ${r} 0 0 1 ${r} ${-r}h${w - 2 * r}a${r} ${r} 0 0 1 ${r} ${r}v${h - r}h${-w}z`;
}

/** The card's content width. A state ref, not a useRef: the chart mounts after its loading placeholder. */
function useElementWidth(element: HTMLDivElement | null) {
  const [width, setWidth] = useState(640);
  useEffect(() => {
    if (!element) return;
    setWidth(element.clientWidth);
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return width;
}

const METRICS: UsageMetric[] = ["tokens", "cost"];

/** Tokens or cost, remembered in the browser: a view preference, not a server setting. */
function MetricToggle({ metric, onChange }: { metric: UsageMetric; onChange: (metric: UsageMetric) => void }) {
  return (
    <div role="group" aria-label="Chart metric" data-testid="usage-chart-metric" className="inline-flex shrink-0 items-center gap-0.5 rounded-md border bg-muted p-0.5">
      {METRICS.map((value) => (
        <Button
          key={value}
          type="button"
          size="sm"
          variant="ghost"
          aria-pressed={metric === value}
          onClick={() => onChange(value)}
          className={cn("h-7 px-2.5 hover:bg-transparent dark:hover:bg-transparent", metric === value ? "bg-background text-foreground shadow-xs hover:bg-background dark:bg-background" : "text-muted-foreground")}
        >
          {METRIC_LABEL[value]}
        </Button>
      ))}
    </div>
  );
}

export interface UsageChartProps {
  /** `GET /usage` rows with `group_by=day,model`: one UTC day and one model per row. */
  rows: UsageRow[] | undefined;
  /** First and last UTC day drawn (`YYYY-MM-DD`), both inclusive. Every day in between gets a slot. */
  from: string;
  to: string;
  metric: UsageMetric;
  onMetricChange: (metric: UsageMetric) => void;
  loading?: boolean;
  error?: Error | null;
  onRetry?: () => void;
  /** What a range without usage says; the range differs per page. */
  empty?: string;
  title?: string;
  footer?: ReactNode;
}

export function UsageChart({ rows, from, to, metric, onMetricChange, loading, error, onRetry, empty = "No usage in this period.", title = "Usage by day", footer }: UsageChartProps) {
  const days = useMemo(() => daysBetween(from, to), [from, to]);
  const { buckets, unpriced, unknownRequests } = useMemo(() => buildBuckets(rows, metric), [rows, metric]);
  const peak = useMemo(
    () => days.reduce((max, day) => Math.max(max, buckets.reduce((sum, bucket) => sum + (bucket.values.get(day) ?? 0), 0)), 0),
    [days, buckets],
  );
  const axis = useMemo(() => axisFor(peak), [peak]);

  const [active, setActive] = useState<number | null>(null);
  const [tableOpen, setTableOpen] = useState(false);
  const [scroll, setScroll] = useState(0);
  const [scroller, setScroller] = useState<HTMLDivElement | null>(null);
  const [box, setBox] = useState<HTMLDivElement | null>(null);
  const dayNodes = useRef<(SVGGElement | null)[]>([]);
  const width = useElementWidth(box);
  const id = useId();

  const hasRows = (rows?.length ?? 0) > 0;
  const inner = Math.max(width - PAD.left - PAD.right, days.length * MIN_SLOT);
  const totalWidth = inner + PAD.left + PAD.right;
  // A range wider than the card opens at its recent end: that is the part a reader came for.
  useEffect(() => {
    if (scroller) scroller.scrollLeft = scroller.scrollWidth;
  }, [scroller, totalWidth]);
  const height = PAD.top + PLOT_H + PAD.bottom;
  const slot = inner / Math.max(1, days.length);
  const barWidth = Math.min(BAR_MAX, Math.max(3, slot - 6));
  const base = PAD.top + PLOT_H;
  const y = (value: number) => base - (value / axis.max) * PLOT_H;
  const left = (index: number) => PAD.left + slot * index + (slot - barWidth) / 2;
  // Every n-th day is labelled, counted from the end so the last day always has one.
  const stride = Math.max(1, Math.ceil(days.length / 6));
  const labelled = new Set(days.map((_, index) => index).filter((index) => (days.length - 1 - index) % stride === 0));

  // A long list goes into two columns when the card is wide enough for both; a phone keeps one column and
  // scrolls the list instead of truncating every model name.
  const twoColumns = buckets.length > 4 && width >= 420;
  const tooltipWidth = Math.min(twoColumns ? 336 : 240, width - 8);
  const activeDay = active === null ? undefined : days[active];
  const tooltipRows = activeDay
    ? buckets
        .map((bucket) => ({
          name: bucket.name,
          hue: bucket.hue,
          text: exact(bucket.values.get(activeDay) ?? 0, metric),
          value: bucket.values.get(activeDay) ?? 0,
          members: bucket.members.filter((member) => (member.values.get(activeDay) ?? 0) > 0),
        }))
        .filter((row) => row.value > 0)
    : [];
  const tooltipTotal = tooltipRows.reduce((sum, row) => sum + row.value, 0);
  const tooltipLeft =
    active === null
      ? 0
      : Math.min(Math.max(4, PAD.left + slot * (active + 0.5) - scroll - tooltipWidth / 2), Math.max(4, width - tooltipWidth - 4));

  const dayTotal = (day: string) => buckets.reduce((sum, bucket) => sum + (bucket.values.get(day) ?? 0), 0);
  const dayLabel = (index: number) => {
    const day = days[index];
    const total = dayTotal(day);
    if (total === 0) return `${day} UTC: no usage`;
    const parts = buckets
      .map((bucket) => [bucket.name, bucket.values.get(day) ?? 0] as const)
      .filter(([, value]) => value > 0)
      .map(([name, value]) => `${name} ${exact(value, metric)}`);
    return `${day} UTC: ${exact(total, metric)} in total; ${parts.join(", ")}`;
  };

  const focusDay = (index: number) => dayNodes.current[index]?.focus();
  const onKeyDown = (event: KeyboardEvent<SVGGElement>, index: number) => {
    const last = days.length - 1;
    const next =
      event.key === "ArrowRight" ? Math.min(last, index + 1)
      : event.key === "ArrowLeft" ? Math.max(0, index - 1)
      : event.key === "Home" ? 0
      : event.key === "End" ? last
      : null;
    if (next === null) {
      if (event.key === "Escape") setActive(null);
      return;
    }
    event.preventDefault();
    focusDay(next);
  };

  const name = `${METRIC_LABEL[metric]} per UTC day, by model, ${from} to ${to}`;
  let body: ReactNode;
  if (error) {
    body = (
      <Alert variant="destructive" data-testid="usage-chart-error">
        <AlertDescription className="col-start-2 flex flex-col items-start gap-3">
          <p>{error.message}</p>
          {onRetry && (
            <Button variant="outline" size="sm" onClick={onRetry}>
              Try again
            </Button>
          )}
        </AlertDescription>
      </Alert>
    );
  } else if (loading) {
    body = (
      <div className="flex flex-col gap-2" aria-busy="true">
        <Skeleton className="h-[170px] w-full" />
      </div>
    );
  } else if (!hasRows) {
    body = (
      <p className="m-0 py-8 text-center text-sm text-muted-foreground" data-testid="usage-chart-empty">
        {empty}
      </p>
    );
  } else {
    body = (
      <>
        {buckets.length >= 2 && (
          <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs" aria-label="Chart series" data-testid="usage-chart-legend">
            {buckets.map((bucket) => (
              <li key={bucket.name} data-series={bucket.name} className="inline-flex min-w-0 items-center gap-1.5" title={bucket.members.length ? bucket.members.map((member) => member.name).join(", ") : undefined}>
                <span className={cn("size-2.5 shrink-0 rounded-sm", bucket.hue === null ? "bg-series-other" : SERIES_DOT[bucket.hue])} aria-hidden="true" />
                <span className="truncate text-foreground">{bucket.name}</span>
              </li>
            ))}
          </ul>
        )}
        <div ref={setBox} className="relative w-full">
          <div ref={setScroller} className="overflow-x-auto" onScroll={(event) => setScroll(event.currentTarget.scrollLeft)}>
            <svg width={totalWidth} height={height} role="group" aria-label={name} className="block max-w-none">
              {axis.ticks.map((tick) => (
                <g key={tick}>
                  <line x1={PAD.left} x2={PAD.left + inner} y1={y(tick)} y2={y(tick)} className="stroke-border" strokeWidth={1} strokeDasharray={tick === 0 ? undefined : "3 3"} />
                  <text x={PAD.left - 8} y={y(tick) + 4} textAnchor="end" fontSize={11} className="fill-muted-foreground">
                    {tickLabel(tick, metric, axis.step)}
                  </text>
                </g>
              ))}
              {days.map((day, index) => {
                const segments: { top: number; height: number; hue: number | null }[] = [];
                let stacked = 0;
                for (const bucket of buckets) {
                  const value = bucket.values.get(day) ?? 0;
                  if (value <= 0) continue;
                  const size = (value / axis.max) * PLOT_H;
                  segments.push({ top: base - stacked - size, height: size, hue: bucket.hue });
                  stacked += size;
                }
                const isActive = active === index;
                return (
                  <g
                    key={day}
                    ref={(element) => {
                      dayNodes.current[index] = element;
                    }}
                    data-testid="usage-day"
                    data-day={day}
                    role="img"
                    aria-label={dayLabel(index)}
                    tabIndex={isActive || (active === null && index === 0) ? 0 : -1}
                    className="cursor-default outline-none"
                    onFocus={() => setActive(index)}
                    onBlur={() => setActive((current) => (current === index ? null : current))}
                    onPointerEnter={(event: PointerEvent<SVGGElement>) => {
                      if (event.pointerType !== "touch") setActive(index);
                    }}
                    onPointerLeave={(event: PointerEvent<SVGGElement>) => {
                      if (event.pointerType !== "touch") setActive((current) => (current === index ? null : current));
                    }}
                    onPointerDown={(event: PointerEvent<SVGGElement>) => {
                      if (event.pointerType !== "mouse") setActive((current) => (current === index ? null : index));
                    }}
                    onKeyDown={(event) => onKeyDown(event, index)}
                  >
                    {isActive && <rect x={PAD.left + slot * index} y={PAD.top} width={slot} height={PLOT_H} className="fill-muted/60" />}
                    <rect x={left(index) + barWidth / 2 - 2} y={base - 2} width={4} height={2} className="fill-border" aria-hidden="true" />
                    {segments.map((segment, segmentIndex) => {
                      const top = segmentIndex === segments.length - 1;
                      const gap = top ? 0 : SEGMENT_GAP;
                      const barHeight = Math.max(1, segment.height - gap);
                      const fill = segment.hue === null ? "fill-series-other" : SERIES_FILL[segment.hue];
                      return top ? (
                        <path key={segmentIndex} d={topRounded(left(index), segment.top + gap, barWidth, barHeight)} className={fill} />
                      ) : (
                        <rect key={segmentIndex} x={left(index)} y={segment.top + gap} width={barWidth} height={barHeight} className={fill} />
                      );
                    })}
                    {/* The hit target is the whole slot, taller than the bar: a tap or hover lands anywhere in it. */}
                    <rect x={PAD.left + slot * index} y={PAD.top} width={slot} height={PLOT_H} fill="transparent" />
                  </g>
                );
              })}
              {[...labelled].map((index) => (
                <text key={days[index]} x={PAD.left + slot * (index + 0.5)} y={base + 15} textAnchor="middle" fontSize={11} className="fill-muted-foreground">
                  {days[index].slice(5)}
                </text>
              ))}
            </svg>
          </div>
          {active !== null && activeDay && (
            <div
              role="tooltip"
              data-testid="usage-chart-tooltip"
              className="pointer-events-none absolute top-0 z-10 rounded-lg border bg-popover p-2.5 text-xs text-popover-foreground shadow-md"
              style={{ left: tooltipLeft, width: tooltipWidth }}
            >
              <div className="mb-1.5 flex items-baseline justify-between gap-2">
                <span className="font-medium">{activeDay}</span>
                <span className="text-muted-foreground">UTC</span>
              </div>
              {tooltipRows.length === 0 ? (
                <div className="text-muted-foreground">No usage</div>
              ) : (
                <ul className={cn("flex flex-col gap-1", twoColumns && "grid grid-cols-2 gap-x-4")} style={{ maxHeight: PLOT_H - 56, overflowY: "auto" }}>
                  {tooltipRows.map((row) => (
                    <li key={row.name}>
                      <div className="flex items-center justify-between gap-3">
                        <span className="flex min-w-0 items-center gap-1.5">
                          <span className={cn("size-2 shrink-0 rounded-sm", row.hue === null ? "bg-series-other" : SERIES_DOT[row.hue])} aria-hidden="true" />
                          <span className="truncate">{row.name}</span>
                        </span>
                        <span className="shrink-0 tabular-nums">{row.text}</span>
                      </div>
                      {row.members.length > 0 && <div className="pl-3.5 text-muted-foreground">{row.members.map((member) => member.name).join(", ")}</div>}
                    </li>
                  ))}
                </ul>
              )}
              {tooltipRows.length > 1 && (
                <div className="mt-1.5 flex items-center justify-between gap-3 border-t pt-1.5 font-medium">
                  <span>Total</span>
                  <span className="tabular-nums">{exact(tooltipTotal, metric)}</span>
                </div>
              )}
            </div>
          )}
        </div>
        {metric === "cost" && (unpriced.length > 0 || unknownRequests > 0) && (
          <p className="text-xs text-muted-foreground" data-testid="usage-chart-unpriced">
            {unpriced.length === 1 && `1 model has no price and is not included in cost (${unpriced[0]}).`}
            {unpriced.length > 1 && `${unpriced.length} models have no price and are not included in cost (${unpriced.join(", ")}).`}
            {unknownRequests > 0 && ` ${fmtNumber(unknownRequests)} ${unknownRequests === 1 ? "request has" : "requests have"} no price, so the bars are a lower bound.`}
          </p>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="m-0 text-xs text-muted-foreground">All times UTC</p>
          <Button type="button" variant="ghost" size="sm" aria-expanded={tableOpen} aria-controls={`${id}-table`} data-testid="usage-chart-table-toggle" onClick={() => setTableOpen(!tableOpen)}>
            {tableOpen ? "Hide data table" : "Show data table"}
          </Button>
        </div>
        {tableOpen && (
          <div id={`${id}-table`} data-testid="usage-chart-table">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="px-2">Day (UTC)</TableHead>
                  {buckets.map((bucket) => (
                    <TableHead key={bucket.name} className="px-2 text-right">
                      {bucket.name}
                    </TableHead>
                  ))}
                  <TableHead className="px-2 text-right">Total</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {days.map((day) => (
                  <TableRow key={day}>
                    <TableCell className="px-2 whitespace-nowrap">{day}</TableCell>
                    {buckets.map((bucket) => (
                      <TableCell key={bucket.name} className="px-2 text-right tabular-nums">
                        {number(bucket.values.get(day) ?? 0, metric)}
                      </TableCell>
                    ))}
                    <TableCell className="px-2 text-right tabular-nums">{number(dayTotal(day), metric)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </>
    );
  }

  return (
    <Card data-testid="usage-chart" data-metric={metric}>
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle>{title}</CardTitle>
          <CardDescription>
            {METRIC_LABEL[metric]} per UTC day, {from} to {to}, by model.
          </CardDescription>
        </div>
        <MetricToggle metric={metric} onChange={onMetricChange} />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">{body}</CardContent>
      {footer && <CardFooter className="text-sm">{footer}</CardFooter>}
    </Card>
  );
}
