import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";

import type { UsageRow } from "../api";
import { daysBetween } from "../lib/days";
import { METRIC_LABEL, type UsageMetric } from "../lib/usageMetric";
import { cn } from "../lib/utils";
import { fmtNumber, fmtUsd, nanoToUsd } from "./Bits";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "./ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./ui/table";

/*
 * Usage over time: one stacked bar per UTC day, one series per model, hand-drawn SVG (no chart dependency).
 * Rows come from `GET /usage` with `group_by=day,model`; the API buckets by UTC day and has no week or month
 * grouping, so a long range stays daily and scrolls inside the card, with the y axis pinned outside the scroller.
 */

/** Categorical slots; a ninth named series would be indistinguishable, so the tail folds into Other. */
const HUES = 8;
const PLOT_H = 170;
const PAD = { top: 12, right: 10, bottom: 22, left: 62 };
const MIN_SLOT = 22;
/** The surface gap between stacked segments, and the widest a bar gets. */
const GAP = 2;
const BAR = 26;

/** The named models drawn by the last render, by slot: a survivor outranks anything remembered. */
let drawnLast = new Map<string, number>();
/** Every model that has ever been drawn named, so its colour comes back if it does. */
const slots = new Map<string, number>();

/**
 * Slot per model, in sorted drawing order and in the palette's fixed slot order. The models drawn in the
 * previous render keep their slots first (a filter or a different page repaints nothing that survives), then a
 * model that is back after a gap takes its old slot if it is still free, and anything else takes the lowest
 * free slot. At most eight are named, so the drawn models always get distinct slots.
 */
function slotMap(names: string[]) {
  // A render that draws nothing (loading, empty) says nothing about survival: keep the last real drawing.
  if (!names.length) return new Map<string, number>();
  const taken = new Set<number>();
  const out = new Map<string, number>();
  const give = (name: string, slot: number) => {
    out.set(name, slot);
    taken.add(slot);
  };
  for (const name of names) {
    const slot = drawnLast.get(name);
    if (slot !== undefined) give(name, slot);
  }
  for (const name of names) {
    if (out.has(name)) continue;
    const slot = slots.get(name);
    if (slot !== undefined && !taken.has(slot)) give(name, slot);
  }
  for (const name of names) {
    if (out.has(name)) continue;
    let slot = 0;
    while (taken.has(slot)) slot++;
    give(name, slot);
  }
  drawnLast = new Map(out);
  for (const [name, slot] of out) slots.set(name, slot);
  return out;
}

interface Model {
  tokens: number[];
  costs: number[];
  requests: number[];
  tokenTotal: number;
  costTotal: number;
  /** Some request had a known price. A price of zero is known: a free request is not an unpriced one. */
  priced: boolean;
  /** Requests with no price: recorded when no price was configured. */
  unknown: number;
}

interface Series {
  name: string;
  /** Palette slot, or null for the neutral Other. */
  slot: number | null;
  /** One value per day of the range, in the drawn metric. */
  cells: number[];
  /** Requests per day, whatever the metric: they decide whether a day has usage at all. */
  counts: number[];
  /** For Other: the folded models, so the tooltip can name them. */
  members: { name: string; cells: number[]; requests: number[] }[];
}

/**
 * Series and their per-day values. `UsageRowOut` gives `cost_nano_usd` as the priced part and
 * `unknown_cost_count`, so a model whose requests all have an unknown price is reported instead of drawn as a
 * zero bar, while a model priced at zero is drawn and says $0. The eight named models are ranked by total
 * tokens whichever metric is drawn, so a toggle cannot change who is named.
 */
function build(rows: UsageRow[] | undefined, days: string[], metric: UsageMetric) {
  const index = new Map(days.map((day, at) => [day, at]));
  const zeroes = () => days.map(() => 0);
  const models = new Map<string, Model>();
  for (const row of rows ?? []) {
    const at = row.date ? index.get(row.date) : undefined;
    if (at === undefined) continue;
    const name = row.model || "—";
    let model = models.get(name);
    if (!model) {
      model = { tokens: zeroes(), costs: zeroes(), requests: zeroes(), tokenTotal: 0, costTotal: 0, priced: false, unknown: 0 };
      models.set(name, model);
    }
    model.tokens[at] += row.total_tokens;
    model.costs[at] += row.cost_nano_usd;
    model.requests[at] += row.requests;
    model.tokenTotal += row.total_tokens;
    model.costTotal += row.cost_nano_usd;
    model.priced ||= row.requests - row.unknown_cost_count > 0;
    model.unknown += row.unknown_cost_count;
  }

  const names = [...models.keys()].sort((a, b) => a.localeCompare(b));
  const ranked = [...names].sort((a, b) => models.get(b)!.tokenTotal - models.get(a)!.tokenTotal || a.localeCompare(b));
  const top = new Set(ranked.slice(0, HUES));
  const unpriced = metric === "cost" ? names.filter((name) => !models.get(name)!.priced && models.get(name)!.unknown > 0) : [];
  const drawn = names.filter((name) => !unpriced.includes(name));
  const cells = (name: string) => (metric === "tokens" ? models.get(name)!.tokens : models.get(name)!.costs);
  const requests = (name: string) => models.get(name)!.requests;
  const assigned = slotMap(drawn.filter((name) => top.has(name)));
  const series: Series[] = drawn
    .filter((name) => top.has(name))
    .map((name) => ({ name, slot: assigned.get(name) ?? 0, cells: cells(name), counts: requests(name), members: [] }));
  const folded = drawn.filter((name) => !top.has(name));
  if (folded.length) {
    series.push({
      name: "Other",
      slot: null,
      cells: days.map((_, at) => folded.reduce((total, name) => total + cells(name)[at], 0)),
      counts: days.map((_, at) => folded.reduce((total, name) => total + requests(name)[at], 0)),
      members: folded.map((name) => ({ name, cells: cells(name), requests: requests(name) })),
    });
  }
  const totals = days.map((_, at) => series.reduce((sum, s) => sum + s.cells[at], 0));
  // The day's requests over every model, drawn or not: it is what "no usage" must be about.
  const dayRequests = days.map((_, at) => names.reduce((sum, name) => sum + requests(name)[at], 0));
  const unknown = metric === "cost" ? drawn.reduce((sum, name) => sum + models.get(name)!.unknown, 0) : 0;
  return { series, totals, dayRequests, peak: Math.max(0, ...totals), unpriced, unknown };
}

/**
 * Ticks on 1/2/5×10ⁿ, always a whole number: tokens and nano-USD are integers, and a fractional step prints
 * duplicate labels. A peak of zero (no usage, or nothing priced) has no scale at all — just the baseline.
 */
function axisFor(peak: number) {
  if (peak <= 0) return { max: 1, step: 1, ticks: [0] };
  const rough = peak / 4;
  const exp = 10 ** Math.floor(Math.log10(rough));
  const f = rough / exp;
  const step = Math.max(1, Math.round((f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * exp));
  const max = Math.ceil(peak / step) * step;
  const ticks: number[] = [];
  for (let value = 0; value <= max; value += step) ticks.push(value);
  return { max, step, ticks };
}

const round1 = (value: number) => (Math.round(value * 10) / 10).toString();

/**
 * A short axis label that stays true: money keeps exactly the decimals the step needs, and switches to
 * exponent form where fixed decimals would need more than five (or print the same label twice).
 */
function tickLabel(value: number, metric: UsageMetric, step: number) {
  if (metric === "tokens") {
    if (value >= 1e9) return `${round1(value / 1e9)}B`;
    if (value >= 1e6) return `${round1(value / 1e6)}M`;
    if (value >= 1e4) return `${round1(value / 1e3)}k`;
    return fmtNumber(value);
  }
  if (value === 0) return "$0";
  const usd = nanoToUsd(value) ?? 0;
  if (usd >= 1000) return `$${round1(usd / 1000)}k`;
  const digits = Math.max(0, -Math.floor(Math.log10(nanoToUsd(step) ?? 1)));
  return digits <= 5 ? `$${usd.toFixed(digits)}` : `$${usd.toExponential(1)}`;
}

/**
 * Money in the chart's own voice: the table's currency format, except that an amount it would round to zero
 * (less than half a millionth of a dollar, the last digit it prints) keeps its magnitude in exponent form.
 */
function money(nano: number) {
  if (nano === 0) return "$0";
  const usd = nanoToUsd(nano) ?? 0;
  return Math.abs(usd) < 5e-7 ? `$${usd.toExponential(1)}` : fmtUsd(nano);
}

const exact = (value: number, metric: UsageMetric) => (metric === "tokens" ? `${fmtNumber(value)} tokens` : money(value));
/** The same numbers without the unit: a table cell says it in its header. */
const number = (value: number, metric: UsageMetric) => (metric === "tokens" ? fmtNumber(value) : money(value));

/** A series colour: the palette tokens have light and dark values of their own. */
const paint = (slot: number | null) => (slot === null ? "var(--series-other)" : `var(--series-${slot + 1})`);

/** A bar, with rounded data-ends at the top only (`r` is 0 for every other segment). */
function barPath(x: number, y: number, w: number, h: number, r: number) {
  const radius = Math.max(0, Math.min(r, w / 2, h));
  if (!radius) return `M${x} ${y}h${w}v${h}h${-w}z`;
  return `M${x} ${y + radius}a${radius} ${radius} 0 0 1 ${radius} ${-radius}h${w - 2 * radius}a${radius} ${radius} 0 0 1 ${radius} ${radius}v${h - radius}h${-w}z`;
}

/** The card's content width. A state ref, not a useRef: the chart mounts after its loading placeholder. */
function useWidth(element: HTMLDivElement | null) {
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
  const { series, totals, dayRequests, peak, unpriced, unknown } = useMemo(() => build(rows, days, metric), [rows, days, metric]);
  const axis = axisFor(peak);

  // One shown day and the input that chose it: whichever came last wins, so a keyboard move is not hidden by
  // a mouse that is standing still, and moving the mouse takes the day back. The tab stop follows the focus.
  const [focusedDay, setFocusedDay] = useState<string | null>(null);
  const [shown, setShown] = useState<{ day: string; source: "focus" | "hover" | "pin" } | null>(null);
  const at = useMemo(() => new Map(days.map((day, index) => [day, index])), [days]);
  const shownAt = shown ? at.get(shown.day) ?? null : null;
  const focusedAt = focusedDay ? at.get(focusedDay) ?? null : null;
  const focusedSafe = focusedAt === null ? null : days[focusedAt];
  // A new range drops a selection it no longer holds; the derived indices above already ignore stale dates.
  useEffect(() => {
    setShown((current) => (current && !at.has(current.day) ? null : current));
    setFocusedDay((current) => (current && !at.has(current) ? null : current));
  }, [at]);

  // The room the tooltip has: the plot's own height, measured, so a short plot or a zoomed page is respected.
  const [svgEl, setSvgEl] = useState<SVGSVGElement | null>(null);
  const [room, setRoom] = useState(0);
  useLayoutEffect(() => {
    if (!svgEl) return;
    setRoom(svgEl.getBoundingClientRect().height);
    const observer = new ResizeObserver(() => setRoom(svgEl.getBoundingClientRect().height));
    observer.observe(svgEl);
    return () => observer.disconnect();
  }, [svgEl]);
  // Rows hidden to fit; reset whenever the day, the metric, the card or the plot changes.
  const [tipState, setTipState] = useState({ key: "", hidden: 0 });
  const tipRef = useRef<HTMLDivElement | null>(null);

  const [open, setOpen] = useState(false);
  const [scroll, setScroll] = useState(0);
  const [scroller, setScroller] = useState<HTMLDivElement | null>(null);
  const [box, setBox] = useState<HTMLDivElement | null>(null);
  const nodes = useRef<(SVGRectElement | null)[]>([]);
  const id = useId();
  const width = useWidth(box);

  const column = Math.max(0, width - PAD.left);
  const inner = Math.max(column - PAD.right, days.length * MIN_SLOT);
  const slot = inner / Math.max(1, days.length);
  const bar = Math.min(BAR, Math.max(3, slot - 6));
  const base = PAD.top + PLOT_H;
  const y = (value: number) => base - (value / axis.max) * PLOT_H;
  const ticks = axis.ticks.map((value) => ({ value, y: y(value), label: tickLabel(value, metric, axis.step) }));

  // A range wider than the card opens at its recent end: that is the part a reader came for.
  useEffect(() => {
    if (scroller) scroller.scrollLeft = scroller.scrollWidth;
  }, [scroller, inner]);

  // One pass per day: the stack from the baseline up, with the gap between segments and the top end marked.
  const bars: { x: number; y: number; h: number; slot: number | null; day: string; name: string; top: boolean }[] = [];
  days.forEach((day, at) => {
    let up = 0;
    for (const s of series) {
      if (!s.cells[at]) continue;
      const h = (s.cells[at] / axis.max) * PLOT_H;
      bars.push({ x: slot * at + (slot - bar) / 2, y: base - up - h, h, slot: s.slot, day, name: s.name, top: false });
      up += h;
    }
    if (up) bars[bars.length - 1].top = true;
  });
  for (const b of bars) {
    if (b.top) continue;
    b.y += GAP;
    b.h = Math.max(1, b.h - GAP);
  }

  // The tooltip sits beside the shown slot — on its left in the right half, on its right in the left half,
  // and centred over the slot only when the card is too narrow for either side (a phone).
  const tipWidth = Math.min(260, column - 8);
  const centre = slot * ((shownAt ?? 0) + 0.5) - scroll;
  const fits = (left: number) => left >= 4 && left + tipWidth <= column - 4;
  const leftHalf = centre < column / 2;
  const beside = leftHalf ? centre + slot / 2 + 8 : centre - slot / 2 - 8 - tipWidth;
  const across = leftHalf ? centre - slot / 2 - 8 - tipWidth : centre + slot / 2 + 8;
  const tipLeft = fits(beside) ? beside : fits(across) ? across : Math.min(Math.max(4, centre - tipWidth / 2), Math.max(4, column - tipWidth - 4));

  // Rows by value. How many fit is decided after they are laid out: the effect below drops the smallest into
  // "N more in the data table" until the real tooltip fits the real plot, so no estimate can be wrong.
  const dayRows = shownAt === null ? [] : series.filter((s) => s.counts[shownAt] > 0).sort((a, b) => b.cells[shownAt] - a.cells[shownAt] || a.name.localeCompare(b.name));
  // The key covers everything the tooltip renders — the date and every row's name, value and member line —
  // so a refetch that keeps the row count but changes the text measures again, while an identical refetch does not.
  const tipKey =
    shownAt === null
      ? ""
      : [
          days[shownAt],
          metric,
          Math.round(width),
          Math.round(room),
          ...dayRows.map((s) => `${s.name} ${s.cells[shownAt]} ${s.members.filter((m) => m.requests[shownAt] > 0).map((m) => m.name).join(", ")}`),
        ].join("|");
  const hidden = tipState.key === tipKey ? Math.min(tipState.hidden, dayRows.length) : 0;
  const tipRows = dayRows.slice(0, dayRows.length - hidden);
  const more = hidden;
  useLayoutEffect(() => {
    const element = tipRef.current;
    if (!element || shownAt === null || !room) return;
    // One row at a time, before paint, and never past the last row: header, "more" and total stay visible.
    if (element.getBoundingClientRect().height <= room || hidden >= dayRows.length) return;
    setTipState({ key: tipKey, hidden: hidden + 1 });
  }, [tipKey, hidden, room, shownAt, dayRows.length]);

  const dayLabel = (at: number) => {
    const parts = series.filter((s) => s.cells[at]).map((s) => `${s.name} ${exact(s.cells[at], metric)}`);
    return `${days[at]} UTC: ${parts.length ? `${parts.join(", ")}; total ${exact(totals[at], metric)}` : "no usage"}`;
  };
  const onKeyDown = (event: KeyboardEvent<SVGRectElement>, at: number) => {
    const last = days.length - 1;
    const next = event.key === "ArrowRight" ? Math.min(last, at + 1) : event.key === "ArrowLeft" ? Math.max(0, at - 1) : event.key === "Home" ? 0 : event.key === "End" ? last : null;
    if (next === null) return;
    event.preventDefault();
    nodes.current[next]?.focus();
  };

  let body: ReactNode;
  if (error) {
    body = (
      <div className="flex flex-col items-start gap-2 text-sm text-destructive" data-testid="usage-chart-error">
        <p className="m-0">{error.message}</p>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        )}
      </div>
    );
  } else if (loading) {
    body = <div aria-busy="true" className="h-[170px] w-full animate-pulse rounded-md bg-accent" />;
  } else if (!rows?.length) {
    body = (
      <p className="m-0 py-8 text-center text-sm text-muted-foreground" data-testid="usage-chart-empty">
        {empty}
      </p>
    );
  } else {
    body = (
      <>
        {series.length > 1 && (
          <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs" aria-label="Chart series" data-testid="usage-chart-legend">
            {series.map((s) => (
              <li key={s.name} data-series={s.name} className="inline-flex min-w-0 items-center gap-1.5" title={s.members.length ? s.members.map((m) => m.name).join(", ") : undefined}>
                <span aria-hidden="true" className="size-2.5 shrink-0 rounded-sm" style={{ background: paint(s.slot) }} />
                <span className="max-h-[2lh] overflow-hidden break-all">{s.name}</span>
              </li>
            ))}
          </ul>
        )}
        <div ref={setBox} className="flex w-full">
          {/* The y axis stays put while the bars scroll sideways under it. */}
          <div aria-hidden="true" data-testid="usage-chart-yaxis" className="relative shrink-0" style={{ width: PAD.left }}>
            {ticks.map((t) => (
              <span key={t.value} className="absolute right-2 -translate-y-1/2 text-muted-foreground" style={{ top: t.y, fontSize: 11 }}>
                {t.label}
              </span>
            ))}
          </div>
          <div className="relative min-w-0 flex-1">
            <div
              ref={setScroller}
              className="overflow-x-auto"
              onScroll={(event) => {
                setScroll(event.currentTarget.scrollLeft);
                // A pointer's choice belongs to a place on screen that moved; a keyboard focus does not.
                setShown((current) =>
                  current && current.source !== "focus" ? (focusedSafe ? { day: focusedSafe, source: "focus" } : null) : current,
                );
              }}
            >
              <svg ref={setSvgEl} width={inner + PAD.right} height={base + PAD.bottom} data-max={axis.max} role="group" aria-label={`${METRIC_LABEL[metric]} per UTC day, by model, ${from} to ${to}`} className="block max-w-none">
                {axis.ticks.length > 1 && <path d={ticks.filter((t) => t.value).map((t) => `M0 ${t.y}H${inner}`).join("")} className="stroke-border" strokeDasharray="3 3" />}
                <line x1={0} x2={inner} y1={base} y2={base} className="stroke-border" />
                {shownAt !== null && <rect x={slot * shownAt} y={PAD.top} width={slot} height={PLOT_H} className="fill-muted/60" />}
                {bars.map((b, at) => (
                  <path key={at} data-testid="usage-chart-bar" data-bar-day={b.day} data-bar-series={b.name} d={barPath(b.x, b.y, bar, b.h, b.top ? 3 : 0)} style={{ fill: paint(b.slot) }} />
                ))}
                {days.map((day, index) => (
                  <rect
                    key={day}
                    ref={(element) => {
                      nodes.current[index] = element;
                    }}
                    data-testid="usage-day"
                    data-day={day}
                    role="img"
                    aria-label={dayLabel(index)}
                    tabIndex={focusedAt === index || (focusedAt === null && index === 0) ? 0 : -1}
                    x={slot * index}
                    y={PAD.top}
                    width={slot}
                    height={PLOT_H}
                    fill="transparent"
                    className="cursor-default outline-none"
                    // Whichever input came last decides; a keyboard move clears a hover or a pin by taking over.
                    onFocus={() => {
                      setFocusedDay(day);
                      setShown({ day, source: "focus" });
                    }}
                    onBlur={() => {
                      setFocusedDay((current) => (current === day ? null : current));
                      setShown((current) => (current?.source === "focus" && current.day === day ? null : current));
                    }}
                    // A pen hovers like a mouse; only a finger pins, so one gesture never takes two paths.
                    onPointerEnter={(event: PointerEvent<SVGRectElement>) => event.pointerType !== "touch" && setShown({ day, source: "hover" })}
                    onPointerLeave={(event: PointerEvent<SVGRectElement>) =>
                      event.pointerType !== "touch" &&
                      setShown((current) => (current?.source === "hover" && current.day === day ? (focusedSafe ? { day: focusedSafe, source: "focus" } : null) : current))
                    }
                    onPointerDown={(event: PointerEvent<SVGRectElement>) => {
                      if (event.pointerType !== "touch") return;
                      // A finger pins the day; it does not also give it keyboard focus, or the pin could
                      // never be released by scrolling the plot.
                      event.preventDefault();
                      setShown((current) => (current?.source === "pin" && current.day === day ? null : { day, source: "pin" }));
                    }}
                    // A gesture that became a scroll never comes back here: let the pin go.
                    onPointerCancel={() => setShown((current) => (current?.source === "pin" ? null : current))}
                    onKeyDown={(event) => onKeyDown(event, index)}
                  />
                ))}
                {days.map((day, at) =>
                  (days.length - 1 - at) % Math.max(1, Math.ceil(days.length / 6)) ? null : (
                    <text key={day} x={slot * (at + 0.5)} y={base + 15} textAnchor="middle" fontSize={11} className="fill-muted-foreground">
                      {day.slice(5)}
                    </text>
                  ),
                )}
              </svg>
            </div>
            {shownAt !== null && (
              <div ref={tipRef} role="tooltip" data-testid="usage-chart-tooltip" data-hidden-rows={more} className="pointer-events-none absolute top-0 z-10 rounded-lg border bg-popover p-2 text-xs text-popover-foreground shadow-md" style={{ left: tipLeft, width: tipWidth }}>
                <div className="mb-1 flex justify-between gap-2 font-medium">
                  <span>{days[shownAt]}</span>
                  <span className="font-normal text-muted-foreground">UTC</span>
                </div>
                {dayRows.length === 0 ? (
                  <div className="text-muted-foreground">{dayRequests[shownAt] > 0 ? "No priced usage" : "No usage"}</div>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {tipRows.map((s) => (
                      <li key={s.name} className="flex justify-between gap-2">
                        <span className="flex min-w-0 gap-1.5">
                          {/* The colour links the row to its bar, as the legend does. */}
                          <span aria-hidden="true" data-testid="usage-chart-tooltip-swatch" className="mt-1 size-2 shrink-0 rounded-sm" style={{ background: paint(s.slot) }} />
                          <span className="max-h-[2lh] overflow-hidden break-all">
                            <span data-testid="usage-chart-tooltip-name">{s.name}</span>
                            {s.members.length > 0 && <span className="block text-muted-foreground">{s.members.filter((m) => m.requests[shownAt] > 0).map((m) => m.name).join(", ")}</span>}
                          </span>
                        </span>
                        <span className="shrink-0 tabular-nums">{exact(s.cells[shownAt], metric)}</span>
                      </li>
                    ))}
                    {more > 0 && (
                      <li data-testid="usage-chart-tooltip-more" className="text-muted-foreground">
                        {more} more in the data table
                      </li>
                    )}
                  </ul>
                )}
                {dayRows.length > 1 && (
                  <div className="mt-1 flex justify-between gap-2 border-t pt-1 font-medium">
                    <span>Total</span>
                    <span className="tabular-nums">{exact(totals[shownAt], metric)}</span>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
        {metric === "cost" && (unpriced.length > 0 || unknown > 0) && (
          <p className="text-xs text-muted-foreground" data-testid="usage-chart-unpriced">
            {unpriced.length > 0 && `${unpriced.length} ${unpriced.length === 1 ? "model has" : "models have"} no price and ${unpriced.length === 1 ? "is" : "are"} not included in cost (${unpriced.join(", ")}).`}
            {unknown > 0 && ` ${fmtNumber(unknown)} ${unknown === 1 ? "request has" : "requests have"} no price, so the bars are a lower bound.`}
          </p>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="m-0 text-xs text-muted-foreground">All times UTC</p>
          <Button type="button" variant="ghost" size="sm" aria-expanded={open} aria-controls={`${id}-table`} data-testid="usage-chart-table-toggle" onClick={() => setOpen(!open)}>
            {open ? "Hide data table" : "Show data table"}
          </Button>
        </div>
        {/* The wrapper stays in the document, so the toggle's aria-controls always names it. */}
        <div id={`${id}-table`} data-testid="usage-chart-table" hidden={!open}>
          {open && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="px-2">Day (UTC)</TableHead>
                  {series.map((s) => (
                    <TableHead key={s.name} className="px-2 text-right">
                      {s.name}
                    </TableHead>
                  ))}
                  <TableHead className="px-2 text-right">Total</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {days.map((day, at) => (
                  <TableRow key={day}>
                    <TableCell className="px-2 whitespace-nowrap">{day}</TableCell>
                    {series.map((s) => (
                      <TableCell key={s.name} className="px-2 text-right tabular-nums">
                        {number(s.cells[at], metric)}
                      </TableCell>
                    ))}
                    <TableCell className="px-2 text-right tabular-nums">{number(totals[at], metric)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      </>
    );
  }

  return (
    <Card data-testid="usage-chart" data-metric={metric}>
      <CardHeader className="flex-row flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle>{title}</CardTitle>
          <CardDescription>
            {METRIC_LABEL[metric]} per UTC day,{" "}
            <span data-testid="usage-chart-range" className="whitespace-nowrap">
              {from} to {to}
            </span>
            , by model.
          </CardDescription>
        </div>
        <div role="group" aria-label="Chart metric" data-testid="usage-chart-metric" className="inline-flex shrink-0 gap-0.5 rounded-md border bg-muted p-0.5">
          {(["tokens", "cost"] as UsageMetric[]).map((value) => (
            <Button
              key={value}
              type="button"
              size="sm"
              variant="ghost"
              aria-pressed={metric === value}
              onClick={() => onMetricChange(value)}
              className={cn("h-7 px-2.5 hover:bg-transparent dark:hover:bg-transparent", metric === value && "bg-background text-foreground shadow-xs hover:bg-background dark:bg-background")}
            >
              {METRIC_LABEL[value]}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">{body}</CardContent>
      {footer && <CardFooter className="text-sm">{footer}</CardFooter>}
    </Card>
  );
}
