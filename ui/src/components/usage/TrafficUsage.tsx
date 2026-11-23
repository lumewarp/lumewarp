import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useMemo, useState, type ComponentProps } from "react";

import { api, type UsageQuery, type UsageRow } from "../../api";
import { useTeams } from "../../hooks";
import { browserTimeZone } from "../../lib/days";
import { fmtDuration, fmtShare, rangeIso, RangeFields, Stat, type RangeProps } from "../../pages/Usage";
import { ZoneBadge } from "../../zone";
import { fmtCompact, fmtNumber as n } from "../Bits";
import { FilterBar } from "../FilterBar";
import { SelectField } from "../Form";
import { DataTable, type Column } from "../Table";

const LazyTrafficCharts = lazy(() => import("./TrafficCharts").then((m) => ({ default: m.TrafficCharts })));
const TrafficCharts = (props: ComponentProps<typeof LazyTrafficCharts>) => (
  <Suspense fallback={null}>
    <LazyTrafficCharts {...props} />
  </Suspense>
);

/** MCP traffic read by server, method and tool; the first entry is the default (the most detail). */
const MCP_GROUPS = [
  { value: "target,method,tool", label: "Server × method × tool" },
  { value: "target,method", label: "Server × method" },
  { value: "target,tool", label: "Server × tool" },
  { value: "method", label: "Method" },
  { value: "tool", label: "Tool" },
  { value: "target", label: "Server" },
];

/** A2A traffic read by agent and method; the first entry is the default. */
const A2A_GROUPS = [
  { value: "target,method", label: "Agent × method" },
  { value: "method", label: "Method" },
  { value: "target", label: "Agent" },
];

type Row = UsageRow;

/**
 * MCP or A2A traffic: which server or agent, which JSON-RPC method and (for MCP) which tool, with
 * requests, failures and duration — the two protocols carry no model, tokens or cost.
 */
export function TrafficUsage({ kind, range }: { kind: "mcp" | "a2a"; range: RangeProps }) {
  const groups = kind === "mcp" ? MCP_GROUPS : A2A_GROUPS;
  const [groupBy, setGroupBy] = useState(groups[0].value);
  const teams = useTeams();
  const ranged = !!range.from && !!range.to;
  const filters: UsageQuery = {
    ...rangeIso(range),
    tz: browserTimeZone(),
    kind,
    group_by: range.zone.all ? `zone,${groupBy}` : groupBy,
    zone: range.zone.param,
    team_id: range.teamId || undefined,
  };
  const query = useQuery({
    queryKey: ["usage", filters],
    queryFn: () => api.usage(filters),
    enabled: ranged,
  });
  const endFilters: UsageQuery = { ...filters, group_by: "status_class" };
  const ends = useQuery({ queryKey: ["usage", endFilters], queryFn: () => api.usage(endFilters), enabled: ranged, retry: false });
  const total = useMemo(
    () =>
      (query.data ?? []).reduce(
        (acc, row) => ({
          requests: acc.requests + row.requests,
          errors: acc.errors + row.error_count,
          duration: acc.duration + row.total_duration_ms,
        }),
        { requests: 0, errors: 0, duration: 0 },
      ),
    [query.data],
  );
  const dimCols: Record<string, Column<Row>[]> = {
    zone: [
      {
        key: "zone",
        header: "Zone",
        sort: (a, b) => (a.zone ?? "").localeCompare(b.zone ?? ""),
        cell: (row) => {
          const zone = range.zone.zones.find((z) => z.id === row.zone);
          return zone ? <ZoneBadge zone={zone} /> : <span className="text-muted-foreground">{row.zone ?? "—"}</span>;
        },
      },
    ],
    target: [
      {
        key: "target",
        header: kind === "mcp" ? "Server" : "Agent",
        sort: (a, b) => (a.target ?? "").localeCompare(b.target ?? ""),
        cell: (row) => <strong className="[overflow-wrap:anywhere]">{row.target || "—"}</strong>,
      },
    ],
    method: [
      {
        key: "method",
        header: "Method",
        sort: (a, b) => (a.method ?? "").localeCompare(b.method ?? ""),
        cell: (row) => (row.method ? <code className="[overflow-wrap:anywhere]">{row.method}</code> : <span className="text-muted-foreground">—</span>),
      },
    ],
    tool: [
      {
        key: "tool",
        header: "Tool",
        sort: (a, b) => (a.tool ?? "").localeCompare(b.tool ?? ""),
        cell: (row) => (row.tool ? <code className="[overflow-wrap:anywhere]">{row.tool}</code> : <span className="text-muted-foreground">—</span>),
      },
    ],
  };
  const dims = [...(range.zone.all ? ["zone"] : []), ...groupBy.split(",")];
  return (
    <>
      <FilterBar testId="usage-filters" className="2xl:grid-cols-[13rem_13rem_repeat(3,minmax(0,1fr))]">
        <RangeFields range={range} teams={teams.data} />
        <SelectField label="Group by" value={groupBy} onChange={setGroupBy} options={groups} />
      </FilterBar>
      <div className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3">
        <Stat label="Requests" value={fmtCompact(total.requests)} title={n(total.requests)} />
        <Stat label="Errors" value={fmtCompact(total.errors)} title={n(total.errors)} />
        <Stat label="Error share" value={fmtShare(total.errors, total.requests)} />
        <Stat label="Mean duration" value={fmtDuration(total.requests ? total.duration / total.requests : 0)} />
      </div>
      <p className="mb-4 text-sm text-muted-foreground">
        An error is an error outcome or a 4xx/5xx answer from the gateway; a client that hangs up is neither.
      </p>
      {ranged && <TrafficCharts rows={query.data} ends={ends.data} groupBy={groupBy} />}
      <DataTable
        rows={ranged ? query.data : []}
        loading={ranged && query.isPending}
        error={query.error ?? teams.error}
        onRetry={() => void query.refetch()}
        rowKey={(row) => [row.zone, row.kind, row.target, row.method, row.tool, row.team_id].join("|")}
        empty={ranged ? `No ${kind === "mcp" ? "MCP" : "A2A"} traffic in this range.` : "Choose a date range."}
        columns={[
          ...dims.flatMap((dimension) => dimCols[dimension] ?? []),
          { key: "requests", header: "Requests", align: "right", sort: (a, b) => a.requests - b.requests, cell: (row) => n(row.requests) },
          {
            key: "errors",
            header: "Errors",
            align: "right",
            sort: (a, b) => a.error_count - b.error_count,
            cell: (row) =>
              row.error_count ? (
                <span className="inline-flex items-baseline justify-end gap-2 tabular-nums">
                  {n(row.error_count)}
                  <span className="text-xs text-muted-foreground">{fmtShare(row.error_count, row.requests)}</span>
                </span>
              ) : (
                <span className="text-muted-foreground">0</span>
              ),
          },
          {
            key: "duration",
            header: "Mean duration",
            align: "right",
            sort: (a, b) => a.total_duration_ms / a.requests - b.total_duration_ms / b.requests,
            cell: (row) => <span className="tabular-nums">{fmtDuration(row.requests ? row.total_duration_ms / row.requests : 0)}</span>,
          },
        ]}
      />
    </>
  );
}
