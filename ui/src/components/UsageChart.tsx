import { useId, useMemo, useState, type ReactNode } from "react";
import { Bar, BarChart, BarStack, CartesianGrid, XAxis, YAxis } from "recharts";

import type { UsageRow } from "../api";
import { buildOutcomeSeries, buildSeries, paint, type Bucket, type Series } from "../lib/usageSeries";
import { METRIC_LABEL, METRICS, type UsageMetric } from "../lib/usageMetric";
import { fmtCompact, fmtNumber, fmtUsd, nanoToUsd } from "./Bits";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "./ui/card";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "./ui/chart";
import { Toggle } from "./usage/Toggle";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./ui/table";

/*
 * Usage charts (shadcn `chart`, recharts). The series are the table's grouping: `GET /usage` rows with
 * `group_by=<hour|day>,<dimension>` (a single series for `day`), drawn as stacked bars over the page's slots
 * (local hours for a range of at most 48 h, local days beyond). The Errors metric draws two series instead, successful and
 * failed requests per slot. This module is only imported by the
 * lazy Usage route, so recharts stays out of the landing chunk.
 */

const two = (n: number) => String(n).padStart(2, "0");

/** An hour slot in the browser's zone: `14:00`, or `11-23 14:00` when the date is wanted. */
function hourLabel(slot: string, withDate = false) {
  const date = new Date(slot);
  const time = `${two(date.getHours())}:${two(date.getMinutes())}`;
  return withDate ? `${two(date.getMonth() + 1)}-${two(date.getDate())} ${time}` : time;
}

/** An x tick: `MM-DD` for a day; for an hour `HH:00`, with the date on the first tick and where the day changes. */
function tick(slot: string, at: number, slots: string[], bucket: Bucket) {
  if (bucket === "day") return slot.slice(5);
  // `at` counts the drawn ticks (recharts thins them), so the previous slot comes from the slot list.
  const slotAt = slots.indexOf(slot);
  const changed = at === 0 || slotAt <= 0 || new Date(slot).getDate() !== new Date(slots[slotAt - 1]).getDate();
  return hourLabel(slot, changed);
}

const EMPTY: Set<string> = new Set();

/** A series' colour: its own (Success, Error) or its palette slot. */
const colorOf = (s: Series) => s.color ?? paint(s.slot);

/** Money in the chart's own voice: an amount fmtUsd would round to zero keeps its magnitude in exponent form. */
function money(nano: number) {
  if (nano === 0) return "$0";
  const usd = nanoToUsd(nano) ?? 0;
  return Math.abs(usd) < 5e-7 ? `$${usd.toExponential(1)}` : fmtUsd(nano);
}

const exact = (value: number, metric: UsageMetric) => (metric === "cost" ? money(value) : fmtNumber(value));
const axisLabel = (value: number, metric: UsageMetric) => (metric === "cost" ? (value === 0 ? "$0" : `$${nanoToUsd(value)?.toPrecision(2)}`) : fmtCompact(value));

export interface UsageChartProps {
  /** `GET /usage` rows with `group_by=day,<dimension>`: one UTC day and one name per row. */
  rows: UsageRow[] | undefined;
  /** The series name of a row (the table's dimension); a constant for `day`. */
  labelOf: (row: UsageRow) => string;
  /** The dimension's name, for the legend and the accessible label ("Called name"). */
  dimLabel: string;
  /** Every slot of the range, in order (see `bucketSlots`): local days or ISO hour instants. */
  slots: string[];
  bucket: Bucket;
  /** The range as text, with its time zone: the line under the title. */
  rangeLabel: string;
  metric: UsageMetric;
  onMetricChange: (metric: UsageMetric) => void;
  /** The metrics offered; with one the toggle is hidden (the Cost lens draws cost only). */
  metrics?: UsageMetric[];
  loading?: boolean;
  error?: Error | null;
  onRetry?: () => void;
  /** What a range without usage says; the range differs per page. */
  empty?: string;
  title?: string;
  footer?: ReactNode;
}

export function UsageChart({ rows, labelOf, dimLabel, slots: days, bucket, rangeLabel, metric, onMetricChange, metrics = METRICS, loading, error, onRetry, empty = "No usage in this period.", title = "Usage by day", footer }: UsageChartProps) {
  const outcome = metric === "errors";
  const built = useMemo(() => (outcome ? buildOutcomeSeries(rows, days, bucket) : buildSeries(rows, days, metric, labelOf, bucket)), [outcome, rows, days, metric, labelOf, bucket]);
  const [open, setOpen] = useState(false);
  const id = useId();
  const { series } = built;
  const config: ChartConfig = Object.fromEntries(series.map((s, i) => [`s${i}`, { label: s.name, color: colorOf(s) }]));
  // Hidden series are per set of names: another metric, grouping or range starts with everything shown.
  const namesKey = [metric, ...series.map((s) => s.name)].join("\u0000");
  const [hiddenState, setHiddenState] = useState<{ key: string; names: Set<string> }>({ key: "", names: new Set() });
  const hidden = hiddenState.key === namesKey ? hiddenState.names : EMPTY;
  const toggle = (name: string) => {
    const next = new Set(hidden);
    if (!next.delete(name)) next.add(name);
    setHiddenState({ key: namesKey, names: next });
  };
  const shown = series.filter((s) => !hidden.has(s.name));

  let body: ReactNode;
  if (error) {
    body = (
      <div className="flex flex-col items-start gap-2 text-sm text-destructive" data-testid="usage-chart-error">
        <span role="alert">{error.message}</span>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    );
  } else if (loading) {
    body = <div aria-busy="true" className="h-[220px] w-full animate-pulse rounded-md bg-accent" />;
  } else if (!built.requests) {
    body = (
      <p className="m-0 py-8 text-center text-sm text-muted-foreground" data-testid="usage-chart-empty">
        {empty}
      </p>
    );
  } else {
    body = (
      <div className="grid gap-3">
        <div className="flex flex-wrap items-center gap-x-1 gap-y-1">
          <ul className="m-0 flex list-none flex-wrap gap-x-1 gap-y-1 p-0 text-xs" aria-label="Chart series" data-testid="usage-chart-legend">
            {series.map((s) => {
              const visible = !hidden.has(s.name);
              return (
                <li key={s.name} data-series={s.name} title={s.members.length ? s.members.join(", ") : undefined}>
                  <Button type="button" variant="ghost" size="sm" aria-pressed={visible} className="h-6 gap-1.5 px-1.5 text-xs font-normal" onClick={() => toggle(s.name)}>
                    <span aria-hidden="true" className={`size-2.5 shrink-0 rounded-sm ${visible ? "" : "opacity-30"}`} style={{ background: colorOf(s) }} />
                    <span className={`[overflow-wrap:anywhere] ${visible ? "" : "text-muted-foreground line-through"}`}>{s.name}</span>
                  </Button>
                </li>
              );
            })}
          </ul>
          {hidden.size > 0 && (
            <Button type="button" variant="ghost" size="sm" className="h-6 px-1.5 text-xs" data-testid="usage-chart-show-all" onClick={() => setHiddenState({ key: namesKey, names: new Set() })}>
              Show all
            </Button>
          )}
        </div>
        {series.length === 0 ? (
          <p className="m-0 py-8 text-center text-sm text-muted-foreground" data-testid="usage-chart-empty">
            Nothing to draw: no request in this range has a price.
          </p>
        ) : shown.length === 0 ? (
          <p className="m-0 py-8 text-center text-sm text-muted-foreground" data-testid="usage-chart-hidden">
            All series hidden.
          </p>
        ) : (
          <div role="img" aria-label={`${METRIC_LABEL[metric]} ${outcome ? "and successful requests" : `by ${dimLabel}`}, ${rangeLabel}`} data-testid="usage-chart-plot">
            <Stacked series={series} hidden={hidden} days={days} bucket={bucket} config={config} metric={metric} />
          </div>
        )}
        {metric === "cost" && (built.unpriced.length > 0 || built.unknown > 0) && (
          <p className="m-0 text-xs text-muted-foreground" data-testid="usage-chart-unpriced">
            {built.unpriced.length > 0 && `${built.unpriced.length} ${built.unpriced.length === 1 ? "model has" : "models have"} no price and ${built.unpriced.length === 1 ? "is" : "are"} not included in cost (${built.unpriced.join(", ")}).`}
            {built.unknown > 0 && ` ${fmtNumber(built.unknown)} ${built.unknown === 1 ? "request has" : "requests have"} no price, so the bars are a lower bound.`}
          </p>
        )}
        <div>
          <Button type="button" variant="ghost" size="sm" aria-expanded={open} aria-controls={`${id}-table`} data-testid="usage-chart-table-toggle" onClick={() => setOpen(!open)}>
            {open ? "Hide data table" : "Show data table"}
          </Button>
          {/* The wrapper stays in the document, so the toggle's aria-controls always names it. */}
          <div id={`${id}-table`} data-testid="usage-chart-table" hidden={!open}>
            {open && (
              <div className="max-h-72 overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="px-2">{bucket === "hour" ? "Hour" : "Day"}</TableHead>
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
                        <TableCell className="px-2 whitespace-nowrap">{bucket === "hour" ? hourLabel(day, true) : day}</TableCell>
                        {series.map((s) => (
                          <TableCell key={s.name} className="px-2 text-right tabular-nums">
                            {exact(s.cells[at], metric)}
                          </TableCell>
                        ))}
                        <TableCell className="px-2 text-right tabular-nums">{exact(built.dayTotals[at], metric)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <Card data-testid="usage-chart" data-metric={metric}>
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="flex min-w-0 flex-col gap-1.5">
            <CardTitle>{title}</CardTitle>
            <CardDescription className="text-xs">
              <span data-testid="usage-chart-range" className="whitespace-nowrap">
                {rangeLabel}
              </span>{" "}
              · {outcome ? "success and errors" : `by ${dimLabel.toLowerCase()}`}
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            {metrics.length > 1 && <Toggle label="Chart metric" testId="usage-chart-metric" value={metric} options={metrics.map((value) => ({ value, label: METRIC_LABEL[value] }))} onChange={onMetricChange} />}
          </div>
        </div>
      </CardHeader>
      <CardContent>{body}</CardContent>
      {footer && <CardFooter>{footer}</CardFooter>}
    </Card>
  );
}

/** The tooltip's rows: only the series the day has, the Other members named, and the total. */
function Tip({ active, payload, label, series, metric, bucket }: { active?: boolean; payload?: { dataKey?: string | number; value?: number; name?: string | number; color?: string; payload?: Record<string, unknown> }[]; label?: string; series: Series[]; metric: UsageMetric; bucket: Bucket }) {
  const rows = (payload ?? []).filter((item) => Number(item.value) > 0);
  const total = rows.reduce((sum, item) => sum + Number(item.value), 0);
  const other = series.find((s) => s.slot === null);
  const showsOther = rows.some((item) => item.name === "Other" || (item.dataKey !== undefined && series[Number(String(item.dataKey).slice(1))]?.slot === null));
  return (
    <ChartTooltipContent
      testId="usage-chart-tooltip"
      active={active && rows.length > 0}
      payload={rows}
      label={label}
      labelFormatter={(value) => (bucket === "hour" ? hourLabel(String(value), true) : value)}
      formatter={(value) => exact(Number(value), metric)}
      footer={
        <>
          {rows.length > 1 && (
            <div className="flex justify-between gap-2 border-t pt-1 font-medium">
              <span>Total</span>
              <span className="tabular-nums" data-testid="usage-chart-tooltip-total">
                {exact(total, metric)}
              </span>
            </div>
          )}
          {showsOther && other && (
            <div className="text-muted-foreground [overflow-wrap:anywhere]" data-testid="usage-chart-tooltip-more">
              Other: {other.members.join(", ")}
            </div>
          )}
        </>
      }
    />
  );
}

function Stacked({ series, hidden, days, bucket, config, metric }: { series: Series[]; hidden: Set<string>; days: string[]; bucket: Bucket; config: ChartConfig; metric: UsageMetric }) {
  const data = days.map((day, at) => ({ day, ...Object.fromEntries(series.map((s, i) => [`s${i}`, s.cells[at]])) }));
  return (
    <ChartContainer config={config} className="aspect-auto h-[240px] w-full">
      <BarChart data={data} accessibilityLayer margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="day" tickLine={false} axisLine={false} minTickGap={24} tickFormatter={(slot: string, at: number) => tick(slot, at, days, bucket)} />
        <YAxis tickLine={false} axisLine={false} width={56} tickFormatter={(value: number) => axisLabel(value, metric)} />
        <ChartTooltip content={<Tip series={series} metric={metric} bucket={bucket} />} />
        <BarStack radius={[4, 4, 0, 0]}>
          {series.map((s, i) => (
            <Bar key={s.name} dataKey={`s${i}`} name={s.name} hide={hidden.has(s.name)} fill={`var(--color-s${i})`} isAnimationActive={false} maxBarSize={26} />
          ))}
        </BarStack>
      </BarChart>
    </ChartContainer>
  );
}
