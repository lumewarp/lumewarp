import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { CheckIcon, CircleAlertIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type Incident, type IncidentsQuery, type Zone } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { FilterBar } from "../components/FilterBar";
import { SelectField, TextField } from "../components/Form";
import { DataTable, useCursorPaging, type Column } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge as UiBadge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "../components/ui/tooltip";
import { fromLocalInput } from "../lib/days";
import { fmtDuration, fmtStarted, incidentSeconds, reporters, shortAlertname, stateTone } from "../lib/incidents";
import { useZoneFilter } from "../hooks";
import { ZoneBadge } from "../zone";

const STATUSES = [
  { value: "", label: "Any status" },
  { value: "firing", label: "Firing" },
  { value: "resolved", label: "Resolved" },
];

const IMPACTS = [
  { value: "", label: "Any impact" },
  { value: "user_visible", label: "User-visible" },
  { value: "admin_only", label: "Admin only" },
];

type Range = "24h" | "7d" | "30d" | "custom";
const RANGE_HOURS: Record<Exclude<Range, "custom">, number> = { "24h": 24, "7d": 24 * 7, "30d": 24 * 30 };

/** Critical severities stand out; every other value is plain text. */
const SEVERITY_CLASS: Record<string, string> = { critical: "font-semibold text-destructive" };

/** A short local timestamp. */
export const fmtWhen = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });

/** The state users saw (`lumewarp_status`), or a dashed "Admin only" when the alert never reached a banner. */
export function ImpactBadge({ state }: { state: string | null }) {
  if (state) return <Badge tone={stateTone(state)}>{state}</Badge>;
  return <span className="inline-flex w-fit items-center whitespace-nowrap rounded-full border border-dashed px-2 py-0.5 text-xs text-muted-foreground">Admin only</span>;
}

/** Firing or resolved. */
export function FiringBadge({ status }: { status: Incident["status"] }) {
  return <Badge tone={status === "firing" ? "danger" : "ok"}>{status === "firing" ? "Firing" : "Resolved"}</Badge>;
}

/** A clock that ticks every 30 s, so a firing incident's duration counts up. */
export function useNow() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** A local time with the exact timestamp in a tooltip. */
export function When({ iso, now, label }: { iso: string; now: number; label?: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="whitespace-nowrap" tabIndex={0}>
          {label ?? fmtStarted(iso, now)}
        </span>
      </TooltipTrigger>
      <TooltipContent>{new Date(iso).toLocaleString()}</TooltipContent>
    </Tooltip>
  );
}

/** "2 gateways" (with the list in a tooltip) or "3 alerts"; nothing for a single alert. */
export function Reporters({ incident }: { incident: Pick<Incident, "gateways" | "alert_count"> }) {
  const text = reporters(incident);
  if (!text) return null;
  const chip = (
    <UiBadge variant="muted" tabIndex={incident.gateways.length > 1 ? 0 : undefined} data-testid="incident-reporters">
      {text}
    </UiBadge>
  );
  if (incident.gateways.length < 2) return chip;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{chip}</TooltipTrigger>
      <TooltipContent>Reported by {incident.gateways.join(", ")}</TooltipContent>
    </Tooltip>
  );
}

/** Target names as small mono chips; the kind is in a tooltip and read before the name. */
export function TargetChips({ incident }: { incident: Pick<Incident, "targets"> }) {
  return (
    <>
      {incident.targets.map((item) => (
        <Tooltip key={`${item.kind}/${item.name}`}>
          <TooltipTrigger asChild>
            <span tabIndex={0} className="inline-flex items-center rounded-md border bg-muted px-1.5 font-mono text-xs text-foreground [overflow-wrap:anywhere]">
              <span className="sr-only">{item.kind} </span>
              {item.name}
            </span>
          </TooltipTrigger>
          <TooltipContent>{item.kind}</TooltipContent>
        </Tooltip>
      ))}
    </>
  );
}

function ZoneCell({ zone, zones }: { zone: string | null; zones: Zone[] }) {
  if (!zone) return <span className="text-muted-foreground">Every zone</span>;
  const found = zones.find((item) => item.id === zone);
  return found ? <ZoneBadge zone={found} /> : <span className="text-muted-foreground">{zone}</span>;
}

function columns(zones: Zone[], now: number): Column<Incident>[] {
  return [
    {
      key: "state",
      header: "State",
      width: "9rem",
      cell: (row) =>
        row.status === "firing" ? (
          <span className="grid gap-1">
            <FiringBadge status="firing" />
            <span className="whitespace-nowrap text-xs text-destructive">{fmtDuration(incidentSeconds(row, now))}, ongoing</span>
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-success">
            <CheckIcon className="size-3.5" aria-label="Resolved" />
            {fmtDuration(incidentSeconds(row, now))}
          </span>
        ),
    },
    {
      key: "incident",
      header: "Incident",
      cell: (row) => (
        <span className="grid min-w-0 gap-1">
          <Link to="/incidents/$incidentId" params={{ incidentId: row.id }} className="font-semibold underline-offset-2 hover:underline [overflow-wrap:anywhere]" data-testid="incident-link">
            {row.summary || row.alertname}
          </Link>
          <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <span className="font-mono [overflow-wrap:anywhere]">{shortAlertname(row.alertname)}</span>
            {row.targets.length > 0 && <span aria-hidden="true">·</span>}
            <TargetChips incident={row} />
            <Reporters incident={row} />
          </span>
        </span>
      ),
    },
    { key: "impact", header: "User impact", width: "8.5rem", cell: (row) => <ImpactBadge state={row.lumewarp_status} /> },
    { key: "zone", header: "Zone", hideBelow: "md", width: "9rem", cell: (row) => <ZoneCell zone={row.zone} zones={zones} /> },
    { key: "started", header: "Started", hideBelow: "sm", width: "9.5rem", cell: (row) => <When iso={row.starts_at} now={now} /> },
    {
      key: "severity",
      header: "Severity",
      hideBelow: "lg",
      width: "7rem",
      cell: (row) => (row.severity ? <span className={SEVERITY_CLASS[row.severity] ?? "text-muted-foreground"}>{row.severity}</span> : <span className="text-muted-foreground">—</span>),
    },
  ];
}

/**
 * Incidents: alerts Alertmanager sent, grouped per outage (one alertname, zone and target set with overlapping
 * intervals, so a rule firing on every gateway is one row). Firing incidents sit on top, unpaged; resolved ones below,
 * newest first. Lumewarp computes no state of its own.
 */
export function Incidents() {
  const zone = useZoneFilter();
  const [status, setStatus] = useState("");
  const [impact, setImpact] = useState("");
  const [search, setSearch] = useState("");
  const q = useDebounced(search.trim(), 300);
  const [range, setRange] = useState<Range>("7d");
  // The range's start is taken when the range is picked, so the query key stays stable between renders.
  const [since, setSince] = useState(() => new Date(Date.now() - RANGE_HOURS["7d"] * 3_600_000).toISOString());
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const now = useNow();

  const pickRange = (value: Range) => {
    setRange(value);
    if (value !== "custom") setSince(new Date(Date.now() - RANGE_HOURS[value] * 3_600_000).toISOString());
  };

  const common: IncidentsQuery = {
    zone: zone.param,
    impact: impact === "user_visible" || impact === "admin_only" ? impact : undefined,
    q: q || undefined,
  };
  const timeWindow = range === "custom" ? { from: fromLocalInput(from) || undefined, to: fromLocalInput(to) || undefined } : { from: since };
  const resolvedFilters = { ...common, ...timeWindow, status: "resolved" as const };
  // A cursor belongs to one filter set: any change starts at the first page.
  const { cursor, paging } = useCursorPaging(JSON.stringify(resolvedFilters));

  // Firing incidents ignore the time range: an outage that started long ago is still happening now.
  const firingQuery: IncidentsQuery = { ...common, status: "firing", limit: 500 };
  const firing = useQuery({ queryKey: ["incidents", firingQuery], queryFn: () => api.incidents(firingQuery), refetchInterval: 10_000, enabled: status !== "resolved" });
  const resolvedQuery: IncidentsQuery = { ...resolvedFilters, cursor };
  const resolved = useQuery({ queryKey: ["incidents", resolvedQuery], queryFn: () => api.incidents(resolvedQuery), enabled: status !== "firing" });
  const next = resolved.data?.next_cursor ?? undefined;
  const firingRows = status !== "resolved" ? (firing.data?.items ?? []) : [];
  // The firing list is capped; a cursor means there are more than it holds.
  const firingCount = `${firingRows.length}${firing.data?.next_cursor ? "+" : ""}`;
  const cols = columns(zone.zones, now);

  return (
    <>
      <PageHeader
        title="Incidents"
        hint="Alertmanager alerts grouped per outage; only those with a lumewarp_status label reach users' banners."
        actions={
          firingRows.length > 0 ? (
            <Badge tone="danger">
              <span className="size-1.5 rounded-full bg-current" aria-hidden="true" />
              <span data-testid="incident-firing-count">{firingCount} firing</span>
            </Badge>
          ) : undefined
        }
      />
      <FilterBar testId="incident-filters" className="lg:grid-cols-[auto_repeat(3,minmax(0,10rem))_minmax(0,1fr)]">
        <div className="col-span-2 grid gap-1.5 sm:col-span-3 lg:col-span-1">
          <span className="text-sm leading-snug font-semibold" id="incident-range-label">
            Time range
          </span>
          <Tabs value={range} onValueChange={(value) => pickRange(value as Range)}>
            <TabsList aria-labelledby="incident-range-label" data-testid="incident-range">
              <TabsTrigger value="24h">24 h</TabsTrigger>
              <TabsTrigger value="7d">7 days</TabsTrigger>
              <TabsTrigger value="30d">30 days</TabsTrigger>
              <TabsTrigger value="custom">Custom</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
        <SelectField label="Zone" value={zone.value} onChange={zone.set} options={zone.options} hint="Also lists incidents without a zone." />
        <SelectField label="Status" value={status} onChange={setStatus} options={STATUSES} />
        <SelectField label="Impact" value={impact} onChange={setImpact} options={IMPACTS} hint="User-visible incidents carry a lumewarp_status and reach the status banner of users who can call an affected name." />
        <div className="col-span-2 sm:col-span-3 lg:col-span-1">
          <TextField label="Search" value={search} onChange={setSearch} placeholder="Search alert, model, router or upstream" />
        </div>
        {range === "custom" && (
          <>
            <TextField label="From" type="datetime-local" value={from} onChange={setFrom} hint="Resolved incidents that started at or after this time, in your browser's time zone." />
            <TextField label="To" type="datetime-local" value={to} onChange={setTo} hint="Resolved incidents that started before this time." />
          </>
        )}
      </FilterBar>

      {status !== "resolved" && firingRows.length > 0 && (
        <section className="mb-5 grid gap-2" aria-labelledby="incidents-firing" data-testid="incidents-firing">
          <h2 id="incidents-firing" className="m-0 px-1 text-sm font-semibold uppercase tracking-[0.04em] text-destructive">
            Firing now
          </h2>
          <DataTable rows={firingRows} rowKey={(row) => row.id} columns={cols} pageSize={50} />
        </section>
      )}
      {status !== "resolved" && firing.isError && (
        <Alert variant="destructive" className="mb-5" data-testid="incidents-firing-error">
          <CircleAlertIcon />
          <AlertDescription className="flex flex-wrap items-center gap-3">
            <span>Could not load firing incidents: {firing.error.message}</span>
            <Button variant="outline" size="sm" onClick={() => void firing.refetch()}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {status === "firing" && firing.isSuccess && firingRows.length === 0 && <p className="text-sm text-muted-foreground">Nothing is firing.</p>}

      {status !== "firing" && (
        <section className="grid gap-2" aria-labelledby="incidents-resolved" data-testid="incidents-resolved">
          <h2 id="incidents-resolved" className="m-0 px-1 text-sm font-semibold uppercase tracking-[0.04em] text-muted-foreground">
            Resolved
          </h2>
          <DataTable
            rows={resolved.data?.items}
            loading={resolved.isPending}
            error={resolved.error}
            onRetry={() => void resolved.refetch()}
            rowKey={(row) => row.id}
            empty="No resolved incidents match."
            paging={paging(next, resolved.isFetching)}
            columns={cols}
          />
        </section>
      )}
    </>
  );
}
