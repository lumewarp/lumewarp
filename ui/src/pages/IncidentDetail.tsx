import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowUpRightIcon, CheckIcon, ChevronRightIcon, CircleAlertIcon } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceArea, XAxis, YAxis } from "recharts";

import { api, type IncidentDetail as Detail } from "../api";
import { Badge, fmtCompact, fmtNumber } from "../components/Bits";
import { KeyPill } from "../components/KeyPill";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from "../components/ui/chart";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "../components/ui/tooltip";
import { attemptsSummary, clock, fillMinutes, fmtDuration, incidentSeconds, stateTone } from "../lib/incidents";
import { useZone, ZoneBadge } from "../zone";
import { StatusBadge } from "./RequestDetail";
import { TargetChips, useNow } from "./Incidents";
import { PageLoading } from "./Access";

type Traffic = Detail["traffic"];
type Minute = Traffic["per_minute"][number];
type MinuteMetric = "requests" | "ttft" | "context";
type MinuteField = "ttft_avg_ms" | "ttft_p95_ms" | "context_avg_tokens" | "context_p95_tokens";

const percent = (share: number) => `${(share * 100).toLocaleString(undefined, { maximumFractionDigits: 0 })}%`;
const fmtMs = (value: number) => (value >= 1000 ? `${(value / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} s` : `${Math.round(value)} ms`);
const seconds = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const perMinute = (rate: number) => (rate > 0 && rate < 0.1 ? "under 0.1" : rate.toLocaleString(undefined, { maximumFractionDigits: 1 }));
const minuteOf = (ms: number) => Math.floor(ms / 60_000) * 60_000;

const MINUTE_METRICS: Record<Exclude<MinuteMetric, "requests">, { avg: MinuteField; p95: MinuteField; format: (value: number) => string; empty: string }> = {
  ttft: { avg: "ttft_avg_ms", p95: "ttft_p95_ms", format: fmtMs, empty: "No streamed requests in this window, so no time to first token." },
  context: { avg: "context_avg_tokens", p95: "context_p95_tokens", format: (value) => fmtCompact(Math.round(value)), empty: "No request reported its prompt tokens in this window." },
};

/** Severity badges: critical in the danger tone, anything else neutral. */
const SEVERITY_TONE: Record<string, "danger" | "warn" | "muted"> = { critical: "danger", warning: "warn" };

/** The banner line of "What users saw", in the tone of the state users saw. */
const BANNER_LINE_CLASS: Record<"danger" | "warn" | "muted", string> = {
  danger: "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive",
  warn: "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border bg-warning-bg px-3 py-2 text-sm text-warning",
  muted: "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border bg-warning-bg px-3 py-2 text-sm text-warning",
};

/** Timeline bars by tone: this incident's members, and related incidents by the state their users saw. */
const BAR_CLASS: Record<"danger" | "warn" | "muted", string> = {
  danger: "absolute inset-y-0 rounded-sm bg-destructive/70",
  warn: "absolute inset-y-0 rounded-sm bg-warning/70",
  muted: "absolute inset-y-0 rounded-sm bg-muted-foreground/50",
};

/** Only an http(s) link is rendered; the receiver stores `generatorURL` as sent. */
function isHttpUrl(value: string | null | undefined): value is string {
  if (!value) return false;
  try {
    const { protocol } = new URL(value);
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

function Meta({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5" data-testid={testId}>
      <span className="text-xs uppercase tracking-[0.04em] text-muted-foreground">{label}</span>
      {children}
    </span>
  );
}

function Kpi({ label, value, note, testId }: { label: string; value: string; note?: string; testId: string }) {
  return (
    <Card className="gap-1 px-4 py-3" data-testid={testId}>
      <span className="text-xs uppercase tracking-[0.04em] text-muted-foreground">{label}</span>
      <span className="text-2xl font-bold tabular-nums">{value}</span>
      {note && <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{note}</span>}
    </Card>
  );
}

/** The incident window on the filled minute axis, clamped to the traffic window, for the shaded area. */
function windowArea(rows: Minute[], start: string, end: string | null, now: number): { x1: string; x2: string } | null {
  if (!rows.length) return null;
  const first = Date.parse(rows[0].minute);
  const last = Date.parse(rows[rows.length - 1].minute);
  const from = Math.max(first, minuteOf(Date.parse(start)));
  const to = Math.min(last, minuteOf(end ? Date.parse(end) : now));
  if (from > to) return null;
  return { x1: new Date(from).toISOString(), x2: new Date(to).toISOString() };
}

function PerMinute({ rows, metric, area }: { rows: Minute[]; metric: MinuteMetric; area: { x1: string; x2: string } | null }) {
  const shade = area && <ReferenceArea x1={area.x1} x2={area.x2} fill="var(--destructive)" fillOpacity={0.08} stroke="var(--destructive)" strokeOpacity={0.25} ifOverflow="extendDomain" />;
  if (metric === "requests") {
    const config: ChartConfig = {
      ok: { label: "Succeeded", color: "var(--series-3)" },
      fallback: { label: "Via fallback", color: "var(--series-4)" },
      failed: { label: "Failed", color: "var(--series-8)" },
    };
    const data = rows.map((row) => ({ minute: row.minute, ok: Math.max(0, row.requests - row.failed - row.fallback), fallback: row.fallback, failed: row.failed }));
    return (
      <ChartContainer config={config} className="aspect-auto h-56 w-full" data-testid="incident-per-minute">
        <BarChart data={data} accessibilityLayer margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="minute" tickLine={false} axisLine={false} minTickGap={24} tickFormatter={clock} />
          <YAxis tickLine={false} axisLine={false} width={40} tickFormatter={(value: number) => fmtCompact(value)} allowDecimals={false} />
          {shade}
          <ChartTooltip content={<ChartTooltipContent labelFormatter={(value) => clock(String(value))} />} />
          <ChartLegend content={<ChartLegendContent />} />
          <Bar dataKey="ok" stackId="requests" fill="var(--color-ok)" isAnimationActive={false} />
          <Bar dataKey="fallback" stackId="requests" fill="var(--color-fallback)" isAnimationActive={false} />
          <Bar dataKey="failed" stackId="requests" fill="var(--color-failed)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ChartContainer>
    );
  }
  const spec = MINUTE_METRICS[metric];
  const data = rows.map((row) => ({ minute: row.minute, avg: row[spec.avg], p95: row[spec.p95] }));
  if (!data.some((point) => point.avg !== null)) {
    return (
      <p className="m-0 text-sm text-muted-foreground" data-testid="incident-per-minute-empty">
        {spec.empty}
      </p>
    );
  }
  const config: ChartConfig = { avg: { label: "Average", color: "var(--series-1)" }, p95: { label: "p95", color: "var(--series-4)" } };
  return (
    <ChartContainer config={config} className="aspect-auto h-56 w-full" data-testid="incident-per-minute">
      <LineChart data={data} accessibilityLayer margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="minute" tickLine={false} axisLine={false} minTickGap={24} tickFormatter={clock} />
        <YAxis tickLine={false} axisLine={false} width={52} tickFormatter={(value: number) => spec.format(value)} />
        {shade}
        <ChartTooltip content={<ChartTooltipContent labelFormatter={(value) => clock(String(value))} formatter={(value) => spec.format(Number(value))} />} />
        <ChartLegend content={<ChartLegendContent />} />
        <Line dataKey="avg" stroke="var(--color-avg)" strokeWidth={2} dot={false} isAnimationActive={false} />
        <Line dataKey="p95" stroke="var(--color-p95)" strokeWidth={2} dot={false} isAnimationActive={false} />
      </LineChart>
    </ChartContainer>
  );
}

/** What the users who can call an affected name saw in their banner. */
function UserImpact({ row }: { row: Detail }) {
  const zone = useZone();
  if (!row.lumewarp_status) return null;
  const byZone = new Map<string, string[]>();
  for (const item of row.affected) byZone.set(item.zone, [...(byZone.get(item.zone) ?? []), item.name]);
  const zoneOf = (id: string) => zone.zones.find((z) => z.id === id) ?? { id, name: id, color: "gray" };
  return (
    <Card className="gap-4" aria-labelledby="incident-impact" data-testid="incident-impact">
      <CardHeader>
        <CardTitle id="incident-impact">What users saw</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className={BANNER_LINE_CLASS[stateTone(row.lumewarp_status)]}>
          <CircleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          <Badge tone={stateTone(row.lumewarp_status)}>{row.lumewarp_status}</Badge>
          <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{row.lumewarp_summary ?? "No lumewarp_summary annotation: users saw only the state."}</span>
        </div>
        <div className="grid gap-2" data-testid="incident-affected">
          <span className="text-xs uppercase tracking-[0.04em] text-muted-foreground">Names affected</span>
          {row.affected.length === 0 ? (
            <span className="text-sm text-muted-foreground">No callable name is deployed on the targets.</span>
          ) : (
            [...byZone].map(([id, names]) => (
              <div key={id} className="flex flex-wrap items-center gap-1.5">
                {byZone.size > 1 && <ZoneBadge zone={zoneOf(id)} />}
                {names.map((name) => (
                  <span key={name} className="inline-flex items-center rounded-md border bg-muted px-1.5 font-mono text-xs [overflow-wrap:anywhere]">
                    {name}
                  </span>
                ))}
              </div>
            ))
          )}
        </div>
      </CardContent>
    </Card>
  );
}

interface Span {
  key: string;
  label: ReactNode;
  start: number;
  end: number;
  tone: "danger" | "warn" | "muted";
  text: string;
}

/** Member alerts and related incidents on one time axis, the incident window shaded behind them. */
function Timeline({ row, now }: { row: Detail; now: number }) {
  const end = (iso: string | null) => (iso ? Date.parse(iso) : now);
  const tone = stateTone(row.lumewarp_status) === "muted" ? "danger" : stateTone(row.lumewarp_status);
  const spans: Span[] = [
    ...row.alerts.map((alert, index) => ({
      key: alert.id,
      label: <span className="font-mono">{alert.instance ?? `alert ${index + 1}`}</span>,
      start: Date.parse(alert.starts_at),
      end: end(alert.ends_at),
      tone,
      text: `${alert.status === "firing" ? "firing" : "resolved"} · ${fmtDuration((end(alert.ends_at) - Date.parse(alert.starts_at)) / 1000)}`,
    })),
    ...row.related.map((item) => ({
      key: item.id,
      label: (
        <Link to="/incidents/$incidentId" params={{ incidentId: item.id }} className="underline-offset-2 hover:underline [overflow-wrap:anywhere]" data-testid="incident-related-link">
          {item.summary || item.alertname}
        </Link>
      ),
      start: Date.parse(item.starts_at),
      end: end(item.ends_at),
      tone: stateTone(item.lumewarp_status),
      text: `${item.lumewarp_status ?? "admin only"} · ${fmtDuration(incidentSeconds(item, now))}`,
    })),
  ];
  const windowStart = Date.parse(row.starts_at);
  const windowEnd = end(row.ends_at);
  const lo = Math.min(windowStart, ...spans.map((span) => span.start));
  const hi = Math.max(windowEnd, ...spans.map((span) => span.end));
  const pad = Math.max(60_000, (hi - lo) * 0.06);
  const axisStart = lo - pad;
  const axisEnd = hi + pad;
  const pos = (at: number) => ((at - axisStart) / (axisEnd - axisStart)) * 100;
  const width = (from: number, to: number) => Math.max(0.6, pos(to) - pos(from));
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((share) => axisStart + share * (axisEnd - axisStart));
  const shade = { left: `${pos(windowStart)}%`, width: `${width(windowStart, windowEnd)}%` };
  return (
    <Card className="gap-3" data-testid="incident-timeline">
      <CardHeader>
        <CardTitle>Timeline</CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-[minmax(0,12rem)_minmax(0,1fr)] items-center gap-x-4 gap-y-2 text-sm">
        {spans.map((span, index) => (
          <div key={span.key} className="contents">
            {index === row.alerts.length && <div className="col-span-2 mt-1 text-xs uppercase tracking-[0.04em] text-muted-foreground">Related incidents</div>}
            <div className="min-w-0 truncate text-muted-foreground">{span.label}</div>
            <div className="relative h-6">
              <div className="absolute inset-y-0 rounded-sm bg-destructive/5" style={shade} aria-hidden="true" />
              <Tooltip>
                <TooltipTrigger asChild>
                  <div className={BAR_CLASS[span.tone]} style={{ left: `${pos(span.start)}%`, width: `${width(span.start, span.end)}%` }} tabIndex={0} role="img" aria-label={span.text} />
                </TooltipTrigger>
                <TooltipContent>
                  {span.text} · {new Date(span.start).toLocaleString()}
                </TooltipContent>
              </Tooltip>
            </div>
          </div>
        ))}
        <div aria-hidden="true" />
        <div className="flex justify-between text-xs tabular-nums text-muted-foreground" aria-hidden="true">
          {ticks.map((at) => (
            <span key={at}>{clock(new Date(at).toISOString())}</span>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function TrafficSection({ row, now }: { row: Detail; now: number }) {
  const traffic = row.traffic;
  const [metric, setMetric] = useState<MinuteMetric>("requests");
  const codes = traffic.errors_by_code.map((item) => ({ name: item.code || "—", value: item.count })).sort((a, b) => b.value - a.value);
  const minutes = Math.max(1, (Date.parse(traffic.window_end) - Date.parse(traffic.window_start)) / 60_000);
  const attempts = attemptsSummary(traffic.attempts);
  const topCode = traffic.errors_by_code[0];
  const perMinuteRows = traffic.per_minute.length ? fillMinutes(traffic.per_minute, traffic.window_start, traffic.window_end) : [];
  const failedNote = traffic.requests ? `${percent(traffic.failed_count / traffic.requests)} of requests${topCode ? ` · most ${topCode.code}` : ""}` : undefined;
  return (
    <section className="grid gap-4" aria-labelledby="incident-traffic" data-testid="incident-traffic">
      <div className="grid gap-1">
        <h2 id="incident-traffic" className="m-0 text-base font-semibold">
          Traffic Lumewarp saw
        </h2>
        <p className="m-0 text-sm text-muted-foreground" data-testid="incident-window">
          {new Date(traffic.window_start).toLocaleString()} to {new Date(traffic.window_end).toLocaleString()}
        </p>
      </div>
      {traffic.clipped && (
        <Alert variant="warn" data-testid="incident-clipped">
          <AlertDescription>The incident lasted longer than 24 hours; only the newest 24 hours are shown.</AlertDescription>
        </Alert>
      )}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Kpi label="Requests" value={fmtNumber(traffic.requests)} note={`${perMinute(traffic.requests / minutes)} per minute`} testId="incident-requests" />
        <Kpi label="Failed" value={fmtNumber(traffic.failed_count)} note={failedNote} testId="incident-failed-count" />
        <Kpi label="Rescued by fallback" value={`${fmtNumber(traffic.rescued_count)} of ${fmtNumber(traffic.fallback_requests)}`} note={traffic.fallback_requests ? `${percent(traffic.fallback_share)} of requests used a fallback` : "No fallback used"} testId="incident-rescued" />
        <Kpi label="Median attempts" value={attempts ? String(attempts.median) : "—"} note={attempts ? `max ${attempts.max}` : undefined} testId="incident-attempts" />
      </div>
      <Card className="gap-3">
        <CardHeader className="flex-row flex-wrap items-center gap-3">
          <CardTitle>Per minute</CardTitle>
          <Tabs value={metric} onValueChange={(value) => setMetric(value as MinuteMetric)}>
            <TabsList aria-label="Per-minute metric" data-testid="incident-metric">
              <TabsTrigger value="requests">Requests</TabsTrigger>
              <TabsTrigger value="ttft">TTFT</TabsTrigger>
              <TabsTrigger value="context">Context</TabsTrigger>
            </TabsList>
          </Tabs>
        </CardHeader>
        <CardContent>{perMinuteRows.length ? <PerMinute rows={perMinuteRows} metric={metric} area={windowArea(perMinuteRows, row.starts_at, row.ends_at, now)} /> : <p className="m-0 text-sm text-muted-foreground">No requests in this window.</p>}</CardContent>
      </Card>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="gap-2" data-testid="incident-error-codes">
          <CardHeader>
            <CardTitle>Error codes</CardTitle>
          </CardHeader>
          <CardContent>
            {codes.length ? (
              <ul className="m-0 grid list-none gap-2 p-0 text-sm">
                {codes.map((code) => (
                  <li key={code.name} className="grid gap-1">
                    <span className="flex items-center justify-between gap-3">
                      <span className="min-w-0 truncate font-mono text-xs">{code.name}</span>
                      <span className="whitespace-nowrap tabular-nums text-muted-foreground">{fmtNumber(code.value)}</span>
                    </span>
                    <span className="h-1.5 overflow-hidden rounded-full bg-muted">
                      <span className="block h-full rounded-full bg-destructive/70" style={{ width: `${(code.value / codes[0].value) * 100}%` }} />
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="m-0 text-sm text-muted-foreground">No errors.</p>
            )}
          </CardContent>
        </Card>
        <Card className="gap-2" data-testid="incident-teams">
          <CardHeader>
            <CardTitle>Top teams</CardTitle>
          </CardHeader>
          <CardContent>
            {traffic.teams.length ? (
              <ul className="m-0 grid list-none gap-2 p-0 text-sm">
                {traffic.teams.map((team) => (
                  <li key={team.team_id} className="flex items-center justify-between gap-3">
                    <span className="min-w-0 truncate">{team.team_name}</span>
                    <span className="whitespace-nowrap tabular-nums text-muted-foreground">
                      {fmtNumber(team.requests)} req · <span className={team.failed ? "text-destructive" : undefined}>{fmtNumber(team.failed)} failed</span>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="m-0 text-sm text-muted-foreground">No traffic.</p>
            )}
          </CardContent>
        </Card>
        <Card className="gap-2" data-testid="incident-keys">
          <CardHeader>
            <CardTitle>Top keys</CardTitle>
          </CardHeader>
          <CardContent>
            {traffic.keys.length ? (
              <ul className="m-0 grid list-none gap-2 p-0 text-sm">
                {traffic.keys.map((key) => (
                  <li key={key.key_id} className="flex items-center justify-between gap-3">
                    <span className="min-w-0">
                      <KeyPill keyRef={key.key} fallbackId={key.key_id} />
                    </span>
                    <span className="whitespace-nowrap tabular-nums text-muted-foreground">
                      {fmtNumber(key.requests)} req · <span className={key.failed ? "text-destructive" : undefined}>{fmtNumber(key.failed)} failed</span>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="m-0 text-sm text-muted-foreground">No traffic.</p>
            )}
          </CardContent>
        </Card>
      </div>
      <section id="failed-requests" className="grid scroll-mt-20 gap-2" aria-labelledby="incident-failed-title" data-testid="incident-failed">
        <h3 id="incident-failed-title" className="m-0 text-sm font-semibold">
          Failed requests
        </h3>
        <DataTable
          rows={traffic.failed_requests}
          rowKey={(item) => item.request_id}
          empty="No failed requests."
          columns={[
            {
              key: "when",
              header: "Time",
              cell: (item) => (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="whitespace-nowrap tabular-nums text-muted-foreground" tabIndex={0}>
                      {seconds(item.ts)}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>{new Date(item.ts).toLocaleString()}</TooltipContent>
                </Tooltip>
              ),
            },
            { key: "called", header: "Called", cell: (item) => <span className="font-mono text-xs [overflow-wrap:anywhere]">{item.requested || "—"}</span> },
            { key: "served", header: "Served", hideBelow: "md", cell: (item) => <span className="font-mono text-xs [overflow-wrap:anywhere]">{item.model || "—"}</span> },
            { key: "status", header: "Status", cell: (item) => <StatusBadge status={item.status} /> },
            { key: "code", header: "Error code", hideBelow: "sm", cell: (item) => (item.error_code ? <code className="text-xs">{item.error_code}</code> : <span className="text-muted-foreground">—</span>) },
            { key: "team", header: "Team", hideBelow: "lg", cell: (item) => item.key?.team_name ?? <span className="text-muted-foreground">—</span> },
            {
              key: "open",
              header: "",
              align: "right",
              cell: (item) => (
                <Link to="/requests" search={{ request_id: item.request_id }} className="inline-flex items-center text-muted-foreground hover:text-foreground" aria-label={`Open request ${item.request_id}`} data-testid="incident-request-link">
                  <ChevronRightIcon className="size-4" aria-hidden="true" />
                </Link>
              ),
            },
          ]}
        />
      </section>
    </section>
  );
}

export function IncidentDetail() {
  const { incidentId } = useParams({ strict: false }) as { incidentId: string };
  const now = useNow();
  const zone = useZone();
  // Only a firing incident changes; a resolved one is read once.
  const incident = useQuery({ queryKey: ["incident", incidentId], queryFn: () => api.incident(incidentId), refetchInterval: (query) => (query.state.data?.status === "firing" ? 10_000 : false) });
  if (incident.isPending) return <PageLoading />;
  if (incident.isError) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{incident.error.message}</AlertDescription>
      </Alert>
    );
  }
  const row = incident.data;
  const title = row.summary || row.alertname;
  const duration = fmtDuration(incidentSeconds(row, now));
  const zoneRow = row.zone ? zone.zones.find((z) => z.id === row.zone) : undefined;
  const reportedBy = row.gateways.length > 1 ? `${row.gateways.length} gateways` : row.gateways.length === 1 ? row.gateways[0] : `${row.alert_count} alert${row.alert_count === 1 ? "" : "s"}`;
  return (
    <div className="grid gap-5" data-testid="incident-detail">
      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground">
        <Link to="/incidents" className="hover:text-foreground hover:underline">
          Incidents
        </Link>
        <span aria-hidden="true">/</span>
        <span className="min-w-0 truncate text-foreground">{title}</span>
      </nav>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="grid min-w-0 flex-1 gap-3">
          <div className="flex flex-wrap items-center gap-2" data-testid="incident-header">
            {row.status === "firing" ? (
              <Badge tone="danger">
                <span data-testid="incident-duration">Firing for {duration}</span>
              </Badge>
            ) : (
              <Badge tone="ok">
                <CheckIcon aria-hidden="true" />
                <span data-testid="incident-duration">Resolved after {duration}</span>
              </Badge>
            )}
            {row.lumewarp_status && <Badge tone={stateTone(row.lumewarp_status)}>Users saw: {row.lumewarp_status}</Badge>}
            {row.severity && <Badge tone={SEVERITY_TONE[row.severity] ?? "muted"}>{row.severity}</Badge>}
          </div>
          <h1 className="m-0 [overflow-wrap:anywhere]">{title}</h1>
          {row.description && <p className="m-0 max-w-3xl text-muted-foreground [overflow-wrap:anywhere]">{row.description}</p>}
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
            <Meta label="Alert">
              <span className="font-mono [overflow-wrap:anywhere]">{row.alertname}</span>
            </Meta>
            <Meta label="Zone">{row.zone ? zoneRow ? <ZoneBadge zone={zoneRow} /> : row.zone : "Every zone"}</Meta>
            <Meta label="Started">{new Date(row.starts_at).toLocaleString()}</Meta>
            <Meta label="Resolved">{row.ends_at ? new Date(row.ends_at).toLocaleString() : "Still firing"}</Meta>
            <Meta label="Reported by">
              {row.gateways.length > 1 ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="underline decoration-dotted underline-offset-2" tabIndex={0}>
                      {reportedBy}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>{row.gateways.join(", ")}</TooltipContent>
                </Tooltip>
              ) : (
                reportedBy
              )}
            </Meta>
            <Meta label="Targets" testId="incident-targets">
              {row.targets.length ? (
                <TargetChips incident={row} />
              ) : (
                <span className="text-muted-foreground">Unresolved</span>
              )}
            </Meta>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={() => document.getElementById("failed-requests")?.scrollIntoView({ behavior: "smooth" })}>
            View failed requests
          </Button>
          {isHttpUrl(row.generator_url) && (
            <Button asChild variant="outline">
              <a href={row.generator_url} target="_blank" rel="noopener noreferrer">
                <ArrowUpRightIcon aria-hidden="true" />
                Open in Prometheus
              </a>
            </Button>
          )}
        </div>
      </header>
      <UserImpact row={row} />
      <Timeline row={row} now={now} />
      <TrafficSection row={row} now={now} />
    </div>
  );
}
