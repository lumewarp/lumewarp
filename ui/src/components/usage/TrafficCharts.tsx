import { useState } from "react";

import type { UsageRow } from "../../api";
import { METRIC_LABEL, type UsageMetric } from "../../lib/usageMetric";
import { foldShares, paint } from "../../lib/usageSeries";
import { fmtCompact, fmtNumber as n } from "../Bits";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { Donut, RankedBars, SliceLegend, type Slice } from "./charts";
import { Toggle } from "./Toggle";

const STATUS_FILL: Record<string, string> = { "2xx": "var(--series-3)", "4xx": "var(--series-4)", "5xx": "var(--series-8)" };
const share = (value: number, total: number) => (total ? `${((value / total) * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%` : "—");

/**
 * MCP and A2A traffic as charts: how the requests ended, and the top servers, methods or tools of the table's
 * grouping by requests or errors. Both are read from the same rows as the table.
 */
export function TrafficCharts({ rows, ends, groupBy }: { rows: UsageRow[] | undefined; ends: UsageRow[] | undefined; groupBy: string }) {
  const [metric, setMetric] = useState<UsageMetric>("requests");
  const statusSlices: Slice[] = (ends ?? [])
    .filter((row) => row.requests > 0)
    .map((row) => ({ name: row.status_class ?? "—", value: row.requests, fill: STATUS_FILL[row.status_class ?? ""] ?? "var(--series-other)" }))
    .sort((a, b) => b.value - a.value);
  const statusTotal = statusSlices.reduce((sum, slice) => sum + slice.value, 0);
  // Each row is named by every dimension the table groups by, so a tool or method is read in context.
  const rankName = (row: UsageRow) =>
    groupBy
      .split(",")
      .map((dim) => (dim === "target" ? row.target : dim === "method" ? row.method : row.tool) || "—")
      .join(" · ");
  const ranked = foldShares((rows ?? []).map((row) => ({ name: rankName(row), value: metric === "errors" ? row.error_count : row.requests }))).map(
    (s): Slice => ({ name: s.name, value: s.total, fill: paint(s.slot), members: s.members }),
  );
  if (!statusSlices.length && !ranked.length) return null;
  return (
    <section className="mb-4 grid gap-3 lg:grid-cols-[20rem_minmax(0,1fr)]" aria-label="Charts" data-testid="traffic-charts">
      <Card className="gap-2" data-testid="traffic-status">
        <CardHeader>
          <CardTitle>How requests ended</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-2">
          <Donut slices={statusSlices} format={(value) => `${n(value)} (${share(value, statusTotal)})`} centre={{ value: fmtCompact(statusTotal), label: "requests" }} height={150} />
          <SliceLegend slices={statusSlices} label="Status" />
        </CardContent>
      </Card>
      <Card className="gap-2" data-testid="traffic-ranked">
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
          <CardTitle>Top {groupBy.replace(/,/g, " × ")}</CardTitle>
          <Toggle label="Chart metric" testId="usage-chart-metric" value={metric} onChange={setMetric} options={(["requests", "errors"] as const).map((value) => ({ value, label: METRIC_LABEL[value] }))} />
        </CardHeader>
        <CardContent>
          <RankedBars slices={ranked} format={(value) => fmtCompact(value)} />
        </CardContent>
      </Card>
    </section>
  );
}
