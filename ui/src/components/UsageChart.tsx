import { useId, useMemo, useState, type ReactNode } from "react";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";

import type { UsageRow } from "../api";
import { daysBetween } from "../lib/days";
import { buildSeries, paint, type Series } from "../lib/usageSeries";
import { METRIC_LABEL, METRICS, type UsageMetric } from "../lib/usageMetric";
import { fmtCompact, fmtNumber, fmtUsd, nanoToUsd } from "./Bits";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "./ui/card";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "./ui/chart";
import { Donut, RankedBars, type Slice } from "./usage/charts";
import { Toggle } from "./usage/Toggle";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./ui/table";

/*
 * Usage charts (shadcn `chart`, recharts). The series are the table's grouping: `GET /usage` rows with
 * `group_by=day,<dimension>` (a single series for `day`), drawn as stacked bars over UTC days, a share donut
 * or ranked bars. The API buckets by UTC day, so a long range stays daily. This module is only imported by the
 * lazy Usage route, so recharts stays out of the landing chunk.
 */

export type ChartView = "stacked" | "share" | "ranked";
const VIEWS: { value: ChartView; label: string }[] = [
  { value: "stacked", label: "Over time" },
  { value: "share", label: "Share" },
  { value: "ranked", label: "Ranked" },
];

/** Money in the chart's own voice: an amount fmtUsd would round to zero keeps its magnitude in exponent form. */
function money(nano: number) {
  if (nano === 0) return "$0";
  const usd = nanoToUsd(nano) ?? 0;
  return Math.abs(usd) < 5e-7 ? `$${usd.toExponential(1)}` : fmtUsd(nano);
}

/** A short value for axes and tooltips; the data table keeps every digit. */
export function short(value: number, metric: UsageMetric) {
  if (metric === "cost") return money(value);
  return fmtCompact(value);
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
  /** `day` grouping has one series: the share and ranked views say nothing there. */
  singleSeries?: boolean;
  /** First and last UTC day drawn (`YYYY-MM-DD`), both inclusive. Every day in between gets a slot. */
  from: string;
  to: string;
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

export function UsageChart({ rows, labelOf, dimLabel, singleSeries, from, to, metric, onMetricChange, metrics = METRICS, loading, error, onRetry, empty = "No usage in this period.", title = "Usage by day", footer }: UsageChartProps) {
  const days = useMemo(() => daysBetween(from, to), [from, to]);
  const built = useMemo(() => buildSeries(rows, days, metric, labelOf), [rows, days, metric, labelOf]);
  const [pickedView, setView] = useState<ChartView>("stacked");
  const view = singleSeries ? "stacked" : pickedView;
  const [open, setOpen] = useState(false);
  const id = useId();
  const { series } = built;
  const config: ChartConfig = Object.fromEntries(series.map((s, i) => [`s${i}`, { label: s.name, color: paint(s.slot) }]));
  const grandTotal = series.reduce((sum, s) => sum + s.total, 0);

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
        <ul className="m-0 flex list-none flex-wrap gap-x-3 gap-y-1 p-0 text-xs" aria-label="Chart series" data-testid="usage-chart-legend">
          {series.map((s) => (
            <li key={s.name} data-series={s.name} className="flex items-center gap-1.5" title={s.members.length ? s.members.join(", ") : undefined}>
              <span aria-hidden="true" className="size-2.5 shrink-0 rounded-sm" style={{ background: paint(s.slot) }} />
              <span className="[overflow-wrap:anywhere]">{s.name}</span>
            </li>
          ))}
        </ul>
        {series.length === 0 ? (
          <p className="m-0 py-8 text-center text-sm text-muted-foreground" data-testid="usage-chart-empty">
            Nothing to draw: no request in this range has a price.
          </p>
        ) : (
          <div role="img" aria-label={`${METRIC_LABEL[metric]} by ${dimLabel}, ${from} to ${to}`} data-testid="usage-chart-plot" data-view={view}>
            {view === "stacked" && <Stacked series={series} days={days} config={config} metric={metric} />}
            {view === "share" && <Share series={series} metric={metric} total={grandTotal} />}
            {view === "ranked" && <Ranked series={series} metric={metric} />}
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
    <Card data-testid="usage-chart" data-metric={metric} data-view={view}>
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <CardTitle>{title}</CardTitle>
            <CardDescription>
              <span data-testid="usage-chart-range" className="whitespace-nowrap">
                {from} – {to} (UTC)
              </span>{" "}
              · by {dimLabel.toLowerCase()}
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            {!singleSeries && <Toggle label="Chart type" testId="usage-chart-view" value={view} options={VIEWS} onChange={setView} />}
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
function Tip({ active, payload, label, series, metric, day }: { active?: boolean; payload?: { dataKey?: string | number; value?: number; name?: string | number; color?: string; payload?: Record<string, unknown> }[]; label?: string; series: Series[]; metric: UsageMetric; day: boolean }) {
  // Share and ranked views have one value per slice: its name is the slice's, not the metric's.
  const rows = (payload ?? [])
    .filter((item) => Number(item.value) > 0)
    .map((item) => (day ? item : { ...item, dataKey: undefined, name: String(item.payload?.name ?? item.name) }));
  const total = rows.reduce((sum, item) => sum + Number(item.value), 0);
  const other = series.find((s) => s.slot === null);
  const showsOther = rows.some((item) => item.name === "Other" || (item.dataKey !== undefined && series[Number(String(item.dataKey).slice(1))]?.slot === null));
  return (
    <ChartTooltipContent
      testId="usage-chart-tooltip"
      active={active && rows.length > 0}
      payload={rows}
      label={label}
      labelFormatter={(value) => (day ? `${value} (UTC)` : value)}
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

function Stacked({ series, days, config, metric }: { series: Series[]; days: string[]; config: ChartConfig; metric: UsageMetric }) {
  const data = days.map((day, at) => ({ day, ...Object.fromEntries(series.map((s, i) => [`s${i}`, s.cells[at]])) }));
  return (
    <ChartContainer config={config} className="aspect-auto h-[240px] w-full">
      <BarChart data={data} accessibilityLayer margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="day" tickLine={false} axisLine={false} minTickGap={24} tickFormatter={(day: string) => day.slice(5)} />
        <YAxis tickLine={false} axisLine={false} width={56} tickFormatter={(value: number) => axisLabel(value, metric)} />
        <ChartTooltip content={<Tip series={series} metric={metric} day />} />
        {series.map((s, i) => (
          <Bar key={s.name} dataKey={`s${i}`} name={s.name} stackId="usage" fill={`var(--color-s${i})`} isAnimationActive={false} maxBarSize={26} />
        ))}
      </BarChart>
    </ChartContainer>
  );
}

function Share({ series, metric, total }: { series: Series[]; metric: UsageMetric; total: number }) {
  return <Donut slices={slices(series)} format={(value) => exact(value, metric)} centre={{ value: short(total, metric), label: METRIC_LABEL[metric].toLowerCase() }} height={240} />;
}

function Ranked({ series, metric }: { series: Series[]; metric: UsageMetric }) {
  return <RankedBars slices={slices(series)} format={(value) => axisLabel(value, metric)} />;
}

const slices = (series: Series[]): Slice[] => series.map((s) => ({ name: s.name, value: s.total, fill: paint(s.slot), members: s.members }));
