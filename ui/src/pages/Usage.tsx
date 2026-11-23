import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { lazy, Suspense, useEffect, useMemo, useState, type ComponentProps } from "react";
import { api, type UsageQuery, type UsageRow } from "../api";
import { Badge, fmtCompact, fmtNumber as n, fmtUsd, PageHeader, TypeBadge } from "../components/Bits";
import { FilterBar } from "../components/FilterBar";
import { SelectField, TextField } from "../components/Form";
import { CalledBreakdown, CalledCell } from "../components/usage/Called";
import { fmtMs } from "../components/usage/format";
import { Toggle } from "../components/usage/Toggle";
import { Card } from "../components/ui/card";
import { ComboboxField } from "../components/ui/combobox-field";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { DataTable, type Column } from "../components/Table";
import { dayStart, isoDay, shiftDay } from "../lib/days";
import { MODEL_TYPES, type ModelType } from "../lib/modelType";
import type { RequestsSearch } from "../lib/requests";
import { USAGE_METRICS, useUsageLens, useUsageMetric, type UsageLens } from "../lib/usageMetric";
import { useCan, useMe, useModels, useNames, useTeamDirectory, useTeams, useZoneFilter } from "../hooks";
import { ZoneBadge } from "../zone";
import { ENDPOINTS, endpointLabel } from "../lib/endpoints";
import { KeyPill } from "../components/KeyPill";
import { Leaderboard } from "../components/Leaderboard";

// The catalog picker is a lazy chunk: this page ships in the entry chunk.
const LazyCatalogFilter = lazy(() => import("../components/CatalogPicker").then((m) => ({ default: m.CatalogFilter })));
// The charts (recharts) are lazy chunks too: the landing JavaScript carries no chart library.
const LazyUsageChart = lazy(() => import("../components/UsageChart").then((m) => ({ default: m.UsageChart })));
const LazyInsights = lazy(() => import("../components/usage/Insights").then((m) => ({ default: m.Insights })));
const LazyTrafficCharts = lazy(() => import("../components/usage/TrafficCharts").then((m) => ({ default: m.TrafficCharts })));
const UsageChart = (props: ComponentProps<typeof LazyUsageChart>) => (
  <Suspense fallback={<div aria-busy="true" className="h-[220px] w-full animate-pulse rounded-md bg-accent" />}>
    <LazyUsageChart {...props} />
  </Suspense>
);
const Insights = (props: ComponentProps<typeof LazyInsights>) => (
  <Suspense fallback={null}>
    <LazyInsights {...props} />
  </Suspense>
);
const TrafficCharts = (props: ComponentProps<typeof LazyTrafficCharts>) => (
  <Suspense fallback={null}>
    <LazyTrafficCharts {...props} />
  </Suspense>
);
const CatalogFilter = (props: ComponentProps<typeof LazyCatalogFilter>) => (
  <Suspense fallback={null}>
    <LazyCatalogFilter {...props} />
  </Suspense>
);

/** `value`, once it has stopped changing for `ms`: typed filters do not query on every keystroke. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** Called name is the default for everyone: the name the client asked for, with renames merged. */
const GROUPS = [
  { value: "called", label: "Called name" },
  { value: "model", label: "Served model" },
  { value: "called,model", label: "Called × served" },
  { value: "requested", label: "As typed" },
  { value: "upstream_model", label: "Upstream model" },
  { value: "alias", label: "Alias" },
  { value: "router", label: "Router" },
  { value: "endpoint", label: "Endpoint" },
  { value: "model_type", label: "Model type" },
  { value: "status_class", label: "Status" },
  { value: "team", label: "Team" },
  { value: "owner", label: "Owner" },
  { value: "key", label: "API key" },
  { value: "day", label: "Day" },
];

/** What everyone but an installation admin may group by: no served model, upstream, alias or router. */
const MEMBER_GROUPS = [
  { value: "called", label: "Called name" },
  { value: "requested", label: "As typed" },
  { value: "endpoint", label: "Endpoint" },
  { value: "model_type", label: "Model type" },
  { value: "status_class", label: "Status" },
  { value: "team", label: "Team" },
  { value: "key", label: "API key" },
  { value: "day", label: "Day" },
];
const OWNER_GROUP = { value: "owner", label: "Owner" };

/** The series dimension of a grouping: its first non-day dimension, `day` when it has none. */
const seriesDim = (groupBy: string) => groupBy.split(",").find((dim) => dim !== "day") ?? "day";

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

/** Audio is stored in milliseconds and read in seconds. */
const fmtSeconds = (ms: number) => (ms / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 });

const today = new Date();
const daysAgo = (count: number) => new Date(today.getTime() - count * 86_400_000);

type Row = UsageRow;

/** A summary tile; `title` carries the exact value when `value` is shortened. */
export function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <Card className="gap-0 p-3.5">
      <div className="text-muted-foreground">{label}</div>
      <div className="text-2xl font-bold tabular-nums" title={title}>
        {value}
      </div>
    </Card>
  );
}

/** The date range and team every tab shares; `to` is inclusive on screen. */
interface RangeProps {
  from: string;
  to: string;
  teamId: string;
  onFrom: (value: string) => void;
  onTo: (value: string) => void;
  onTeam: (value: string) => void;
  /** The zone filter shared by the tabs: `param` is the `?zone=` value (absent = all zones). */
  zone: ReturnType<typeof useZoneFilter>;
}

function RangeFields({ range, teams }: { range: RangeProps; teams: { id: string; name: string }[] | undefined }) {
  return (
    <>
      <TextField label="From" type="date" value={range.from} onChange={range.onFrom} />
      <TextField label="To" type="date" value={range.to} onChange={range.onTo} hint="Inclusive." />
      <ComboboxField
        label="Team"
        value={range.teamId}
        onChange={range.onTeam}
        options={[{ value: "", label: "All teams" }, ...(teams ?? []).map((team) => ({ value: team.id, label: team.name }))]}
      />
      <SelectField label="Zone" value={range.zone.value} onChange={range.zone.set} options={range.zone.options} />
    </>
  );
}

/** A mean duration as a human number: milliseconds under a second, then seconds, then minutes. */
function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "0 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} s`;
  return `${(ms / 60_000).toLocaleString(undefined, { maximumFractionDigits: 1 })} min`;
}

/** Errors over requests, as a percentage; a group with no request shows none. */
function fmtShare(errors: number, requests: number): string {
  if (!requests) return "—";
  const percent = (errors / requests) * 100;
  return `${percent.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
}

/*
 * Alias and router filters: a select whose values are tagged, so a catalog name can never be mistaken for a
 * mode. `any`, `none` and `has` are modes; `id:<uuid>` is one catalog entry and is sent as alias_id/router_id
 * (a name can be literally "none" or "__has__").
 */
const ANY = "";
const NONE = "mode:none";
const HAS = "mode:has";
const ID = "id:";
const byId = (e: { id: string }) => `${ID}${e.id}`;

function provenanceFilter(kind: "alias" | "router", value: string): Partial<UsageQuery> {
  if (value === ANY) return {};
  if (value === NONE) return kind === "alias" ? { alias_missing: true } : { router_missing: true };
  if (value === HAS) return kind === "alias" ? { alias_missing: false } : { router_missing: false };
  if (value.startsWith(ID)) return kind === "alias" ? { alias_id: value.slice(ID.length) } : { router_id: value.slice(ID.length) };
  return {};
}

/** One name, "—" for none (empty string), or the mixed list (null). */
function Provenance({ single, list }: { single: string | null | undefined; list: { name: string }[] | string[] | undefined }) {
  if (single === "") return <span className="text-muted-foreground">—</span>;
  if (single) return <Badge tone="info">{single}</Badge>;
  const names = (list ?? []).map((x) => (typeof x === "string" ? x : x.name));
  return (
    <span className="flex flex-wrap items-center gap-1" data-testid="mixed">
      {names.map((name) => (
        <Badge key={name} tone="info">
          {name}
        </Badge>
      ))}
      <span className="text-xs text-muted-foreground">mixed</span>
    </span>
  );
}

function UpstreamModel({ row }: { row: Row }) {
  if (row.upstream_model === "") return <span className="text-muted-foreground">—</span>;
  if (row.upstream_model) return <code>{row.upstream_model}</code>;
  return (
    <span className="flex flex-col" data-testid="mixed">
      {(row.upstream_models ?? []).map((name) => (
        <code key={name}>{name}</code>
      ))}
      <span className="text-xs text-muted-foreground">mixed</span>
    </span>
  );
}

function ServedBy({ row }: { row: Row }) {
  const names = row.served_by ?? [];
  if (!names.length) return <span className="text-muted-foreground">—</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {names.map((x) => (
        <Badge key={x.id ?? x.name}>{x.name}</Badge>
      ))}
    </span>
  );
}

/**
 * Usage is three talks: the LLM spend (models, tokens, cost), and the MCP and A2A traffic a key made
 * (servers, agents, methods, tools) where tokens and cost do not exist. The range and team carry across tabs.
 */
export function Usage() {
  const [from, setFrom] = useState(isoDay(daysAgo(29)));
  const [to, setTo] = useState(isoDay(today));
  const [teamId, setTeamId] = useState("");
  const [tab, setTab] = useState<"llm" | "mcp" | "a2a">("llm");
  const zone = useZoneFilter();
  const range: RangeProps = { from, to, teamId, onFrom: setFrom, onTo: setTo, onTeam: setTeamId, zone };
  // Cost or usage is the LLM tab's top-level view: it sits beside the tabs and reshapes the whole tab.
  const [lens, setLens] = useUsageLens();
  return (
    <>
      <PageHeader title="Usage" hint="Rows are grouped by the name the client called; Served model shows what answered, and a fallback bills the model that served." />
      <Tabs value={tab} onValueChange={(value) => setTab(value as typeof tab)}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <TabsList variant="line">
            <TabsTrigger value="llm">LLM</TabsTrigger>
            <TabsTrigger value="mcp">MCP</TabsTrigger>
            <TabsTrigger value="a2a">A2A</TabsTrigger>
          </TabsList>
          {tab === "llm" && (
            <Toggle
              label="View"
              testId="usage-lens"
              value={lens}
              onChange={setLens}
              options={[
                { value: "usage", label: "Usage" },
                { value: "cost", label: "Cost" },
              ]}
            />
          )}
        </div>
        <TabsContent value="llm">
          <LlmUsage range={range} lens={lens} />
        </TabsContent>
        <TabsContent value="mcp">
          <TrafficUsage kind="mcp" range={range} />
        </TabsContent>
        <TabsContent value="a2a">
          <TrafficUsage kind="a2a" range={range} />
        </TabsContent>
      </Tabs>
    </>
  );
}

function LlmUsage({ range, lens }: { range: RangeProps; lens: UsageLens }) {
  const [pickedGroup, setGroupBy] = useState("");
  const [model, setModel] = useState(ANY);
  const [upstreamText, setUpstreamText] = useState("");
  const upstreamModel = useDebounced(upstreamText.trim(), 300);
  const [alias, setAlias] = useState(ANY);
  const [router, setRouter] = useState(ANY);
  const [endpoint, setEndpoint] = useState(ANY);
  const [modelType, setModelType] = useState<ModelType | "">("");
  const perms = useCan();
  const routing = perms.canReadRouting;
  const me = useMe();
  const teams = useTeams();
  // Upstream suggestions come from the routing details, which only an installation admin
  // may read; member and service-account names are the team-admin view. A plain member
  // never requests either.
  const models = useModels(perms.canReadRouting);
  // Only the teams this caller administers: a mixed-role user (admin of A, member of B)
  // must not ask B for its member or service-account lists. The hook's query key is built
  // from the same set, so changing the role changes the cache entry too.
  const administered = (teams.data ?? []).filter((team) => perms.teamAdmin(team.id));
  const directoryEnabled = administered.length > 0;
  // Owner is offered where the caller sees other people: the selected team's admin, or any team's.
  const ownerOffered = range.teamId ? perms.teamAdmin(range.teamId) : directoryEnabled;
  const groupOptions = routing ? GROUPS : ownerOffered ? [...MEMBER_GROUPS, OWNER_GROUP] : MEMBER_GROUPS;
  const groupBy = groupOptions.some((option) => option.value === pickedGroup) ? pickedGroup : groupOptions[0].value;
  const directory = useTeamDirectory(administered, directoryEnabled);
  const teamName = useNames(teams.data);
  const memberName = useNames(directory.data?.members.map((member) => ({ id: member.user_id, name: member.email })));
  const accountName = useNames(directory.data?.serviceAccounts);
  // The caller's own row resolves from /users/me even in a team they only belong to.
  const ownName = (id: string) =>
    me.data?.user_id === id
      ? (me.data?.email ?? id.slice(0, 8))
      : directoryEnabled
        ? memberName(id)
        : id.slice(0, 8);

  // `to` is exclusive on the server; include the chosen end day.
  const rangeQuery = {
    from: dayStart(range.from),
    to: dayStart(shiftDay(range.to, 1)),
  };
  const filters: UsageQuery = {
    ...rangeQuery,
    // An MCP or A2A event carries no model, tokens or cost: the LLM tab asks for LLM only.
    kind: "llm",
    // With "All zones" each row names its zone.
    group_by: range.zone.all ? `zone,${groupBy}` : groupBy,
    zone: range.zone.param,
    team_id: range.teamId || undefined,
    endpoint: endpoint || undefined,
    model_type: modelType || undefined,
    // Routing filters answer 403 for everyone but an installation admin: never sent.
    ...(routing
      ? { model: model || undefined, upstream_model: upstreamModel || undefined, ...provenanceFilter("alias", alias), ...provenanceFilter("router", router) }
      : {}),
  };
  const query = useQuery({
    queryKey: ["usage", filters],
    queryFn: () => api.usage(filters),
    enabled: !!range.from && !!range.to,
  });
  // The chart follows the table's grouping: same filters, one series per name of its first non-day dimension.
  const chartDim = seriesDim(groupBy);
  const chartFilters: UsageQuery = { ...filters, group_by: chartDim === "day" ? "day" : `day,${chartDim}` };
  const chart = useQuery({
    queryKey: ["usage", chartFilters],
    queryFn: () => api.usage(chartFilters),
    enabled: !!range.from && !!range.to,
    // A refusal or an outage is shown in the card, with its own retry: it must not stall the page.
    retry: false,
  });
  const [usageMetric, setUsageMetric] = useUsageMetric();
  const costLens = lens === "cost";
  // A half-filled range is not an error: the query stays disabled and the chart says what is missing.
  const ranged = !!range.from && !!range.to;

  const total = useMemo(
    () =>
      (query.data ?? []).reduce(
        (acc, row) => ({
          requests: acc.requests + row.requests,
          audio: acc.audio + row.audio_ms,
          prompt: acc.prompt + row.prompt_tokens,
          completion: acc.completion + row.completion_tokens,
          tokens: acc.tokens + row.total_tokens,
          cost: acc.cost + row.cost_nano_usd,
          unpriced: acc.unpriced + row.unknown_cost_count,
        }),
        { requests: 0, audio: 0, prompt: 0, completion: 0, tokens: 0, cost: 0, unpriced: 0 },
      ),
    [query.data],
  );

  // Suggestions only: the filter takes any string, as old usage keeps the upstream name it was served under.
  const upstreamSuggestions = [...new Set((models.data ?? []).flatMap((m) => (m.deployments ?? []).map((d) => d.upstream_model)))].sort();
  const provenanceModes = (kind: "alias" | "router") => [
    { value: ANY, label: "Any" },
    { value: NONE, label: kind === "alias" ? "None (no alias)" : "None (no router)" },
    { value: HAS, label: kind === "alias" ? "Via any alias" : "Via any router" },
  ];

  const dims = [...(range.zone.all ? ["zone"] : []), ...groupBy.split(",")];
  const zoneOf = (name: string | null | undefined) => {
    const zone = range.zone.zones.find((z) => z.id === name);
    return zone ? <ZoneBadge zone={zone} /> : <span className="text-muted-foreground">{name ?? "—"}</span>;
  };
  const ownerLabel = (kind: string | null | undefined, id: string | null | undefined) => {
    if (!id) return "—";
    return kind === "service_account" ? accountName(id) : ownName(id);
  };
  /** The chart's series name of a row: the same text the table shows for the dimension. */
  const seriesLabel = (row: Row): string => {
    switch (chartDim) {
      case "called":
        return row.called || "—";
      case "model":
        return row.model || "—";
      case "requested":
        return row.requested || "—";
      case "upstream_model":
        return row.upstream_model || "—";
      case "alias":
        return row.alias || "No alias";
      case "router":
        return row.router || "No router";
      case "endpoint":
        return row.endpoint ? endpointLabel(row.endpoint) : "—";
      case "model_type":
        return row.model_type ?? "—";
      case "status_class":
        return row.status_class ?? "—";
      case "team":
        return row.team_id ? teamName(row.team_id) : "—";
      case "owner":
        return row.owner_id ? ownerLabel(row.owner_kind, row.owner_id) : "—";
      case "key":
        return row.key?.name ?? (row.key_id ? row.key_id.slice(0, 8) : "—");
      default:
        return "All usage";
    }
  };
  // The chart draws one series per name of its first non-day dimension, so it is named after that dimension alone.
  const dimLabel = GROUPS.find((option) => option.value === chartDim)?.label ?? "Usage";
  const dimCols: Record<string, Column<Row>[]> = {
    zone: [{ key: "zone", header: "Zone", sort: (a, b) => (a.zone ?? "").localeCompare(b.zone ?? ""), cell: (row) => zoneOf(row.zone) }],
    day: [{ key: "day", header: "Date", sort: (a, b) => (a.date ?? "").localeCompare(b.date ?? ""), cell: (row) => row.date ?? "—" }],
    endpoint: [{ key: "endpoint", header: "Endpoint", sort: (a, b) => (a.endpoint ?? "").localeCompare(b.endpoint ?? ""), cell: (row) => (row.endpoint ? <code>{endpointLabel(row.endpoint)}</code> : <span className="text-muted-foreground">—</span>) }],
    model_type: [{ key: "model_type", header: "Type", sort: (a, b) => (a.model_type ?? "").localeCompare(b.model_type ?? ""), cell: (row) => <TypeBadge type={row.model_type} /> }],
    called: [{ key: "called", header: "Called name", sort: (a, b) => (a.called ?? "").localeCompare(b.called ?? ""), cell: (row) => <CalledCell row={row} /> }],
    status_class: [{ key: "status_class", header: "Status", sort: (a, b) => (a.status_class ?? "").localeCompare(b.status_class ?? ""), cell: (row) => row.status_class ?? "—" }],
    requested: [{ key: "requested", header: "As typed", sort: (a, b) => (a.requested ?? "").localeCompare(b.requested ?? ""), cell: (row) => row.requested ?? "—" }],
    model: [
      { key: "model", header: "Served model", sort: (a, b) => (a.model ?? "").localeCompare(b.model ?? ""), cell: (row) => <strong>{row.model || "—"}</strong> },
      { key: "upstream_model", header: "Upstream model", cell: (row) => <UpstreamModel row={row} /> },
      // Called × served already names the called side; the routes are the single Served model view's detail.
      ...(groupBy === "model"
        ? [
            { key: "via_alias", header: "Via alias", cell: (row: Row) => <Provenance single={row.alias} list={row.via_aliases} /> },
            { key: "via_router", header: "Via router", cell: (row: Row) => <Provenance single={row.router} list={row.via_routers} /> },
          ]
        : []),
    ],
    upstream_model: [
      { key: "upstream_model", header: "Upstream model", cell: (row) => <UpstreamModel row={row} /> },
      { key: "served_by", header: "Served by", cell: (row) => <ServedBy row={row} /> },
    ],
    alias: [
      { key: "alias", header: "Alias", cell: (row) => (row.alias ? <strong>{row.alias}</strong> : <span className="text-muted-foreground">none</span>) },
      { key: "served_by", header: "Served by", cell: (row) => <ServedBy row={row} /> },
    ],
    router: [
      { key: "router", header: "Router", cell: (row) => (row.router ? <strong>{row.router}</strong> : <span className="text-muted-foreground">none</span>) },
      { key: "served_by", header: "Served by", cell: (row) => <ServedBy row={row} /> },
    ],
    team: [{ key: "team", header: "Team", cell: (row) => teamName(row.team_id) }],
    owner: [{ key: "owner", header: "Owner", cell: (row) => ownerLabel(row.owner_kind, row.owner_id) }],
    key: [{ key: "key", header: "API key", cell: (row) => (row.key_id ? <KeyPill keyRef={row.key} fallbackId={row.key_id} /> : "—") }],
  };
  const peak = Math.max(1, ...(query.data ?? []).map((r) => r.total_tokens));
  const costPeak = Math.max(1, ...(query.data ?? []).map((r) => r.cost_nano_usd));
  // The inline bar needs room the dimension columns take at laptop widths: wide screens only.
  const bar = (share: number) => (
    <span className="hidden h-1.5 w-20 overflow-hidden rounded-full bg-muted 2xl:inline-block" aria-hidden="true">
      <span className="block h-full bg-primary" style={{ width: `${share * 100}%` }} />
    </span>
  );
  const muted = (text: string) => <span className="text-muted-foreground">{text}</span>;
  const requestsColumn: Column<Row> = { key: "requests", header: "Requests", align: "right", sort: (a, b) => a.requests - b.requests, cell: (row) => n(row.requests) };
  const logColumn: Column<Row> = {
    key: "log",
    header: "Log",
    cell: (row) => (
      <span className="inline-flex items-center gap-1">
        <Link to="/requests" search={requestsSearchFor(row)} className="font-medium whitespace-nowrap">
          View
        </Link>
        {routing && dims.includes("called") && row.called && !dims.includes("model") && <CalledBreakdown row={row} filters={filters} />}
      </span>
    ),
  };
  const usageColumns: Column<Row>[] = [
    requestsColumn,
    {
      key: "tokens",
      header: "Total tokens",
      align: "right",
      sort: (a, b) => a.total_tokens - b.total_tokens,
      cell: (row) => (
        <span className="inline-flex items-center justify-end gap-2">
          {n(row.total_tokens)}
          {bar(row.total_tokens / peak)}
        </span>
      ),
    },
    { key: "prompt", header: "Prompt", align: "right", hideBelow: "md", cell: (row) => n(row.prompt_tokens) },
    { key: "completion", header: "Completion", align: "right", hideBelow: "md", cell: (row) => n(row.completion_tokens) },
    ...(total.audio > 0 ? [{ key: "audio", header: "Audio (s)", align: "right" as const, hideBelow: "md" as const, sort: (a: Row, b: Row) => a.audio_ms - b.audio_ms, cell: (row: Row) => (row.audio_ms ? fmtSeconds(row.audio_ms) : muted("—")) }] : []),
    { key: "errors", header: "Errors", align: "right", hideBelow: "md", sort: (a, b) => a.error_count / Math.max(1, a.requests) - b.error_count / Math.max(1, b.requests), cell: (row) => <span className={row.error_count ? "tabular-nums" : "text-muted-foreground"}>{fmtShare(row.error_count, row.requests)}</span> },
    { key: "p95", header: "p95", align: "right", hideBelow: "md", sort: (a, b) => (a.p95_duration_ms ?? 0) - (b.p95_duration_ms ?? 0), cell: (row) => <span className="whitespace-nowrap tabular-nums">{fmtMs(row.p95_duration_ms)}</span> },
  ];
  const perRequest = (row: Row) => (row.requests ? row.cost_nano_usd / row.requests : 0);
  const costColumns: Column<Row>[] = [
    requestsColumn,
    {
      key: "cost",
      header: "Cost",
      align: "right",
      sort: (a, b) => a.cost_nano_usd - b.cost_nano_usd,
      cell: (row) => (
        <span className="inline-flex items-center justify-end gap-2">
          {fmtUsd(row.cost_nano_usd)}
          {bar(row.cost_nano_usd / costPeak)}
        </span>
      ),
    },
    { key: "cost_share", header: "Share of cost", align: "right", hideBelow: "md", sort: (a, b) => a.cost_nano_usd - b.cost_nano_usd, cell: (row) => (total.cost > 0 ? `${((row.cost_nano_usd / total.cost) * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%` : muted("—")) },
    { key: "cost_per_request", header: "Cost / request", align: "right", hideBelow: "md", sort: (a, b) => perRequest(a) - perRequest(b), cell: (row) => (row.requests ? fmtUsd(perRequest(row)) : muted("—")) },
    { key: "unpriced", header: "Unpriced", align: "right", hideBelow: "md", sort: (a, b) => a.unknown_cost_count - b.unknown_cost_count, cell: (row) => (row.unknown_cost_count ? <span className="tabular-nums">{n(row.unknown_cost_count)}</span> : muted("—")) },
  ];
  // The same names, key and range the row was grouped by, so Requests opens on the matching rows.
  const requestsSearchFor = (row: Row): RequestsSearch => ({
    kind: "llm",
    from: range.from,
    to: range.to,
    team_id: row.team_id ?? (range.teamId || undefined),
    key_id: row.key_id ?? undefined,
    // A called row opens the log on the one spelling it holds; with several, an admin filters by the catalog item.
    requested: row.requested || (row.called_names?.length === 1 ? row.called_names[0] : !routing ? row.called || undefined : undefined),
    ...(routing
      ? {
          model: row.model || (row.called_kind === "model" && (row.called_names?.length ?? 0) !== 1 ? row.called || undefined : undefined),
          alias: row.alias || (row.called_kind === "alias" && (row.called_names?.length ?? 0) !== 1 ? row.called || undefined : undefined),
          router: row.router || (row.called_kind === "router" && (row.called_names?.length ?? 0) !== 1 ? row.called || undefined : undefined),
        }
      : {}),
  });

  return (
    <>
      <FilterBar
        testId="usage-filters"
        className={
          // Group by is last and keeps room for its longest label ("Called × served").
          routing
            ? "lg:grid-cols-[10rem_10rem_repeat(3,minmax(0,1fr))_minmax(11rem,1fr)_auto]"
            : "lg:grid-cols-[10rem_10rem_repeat(2,minmax(0,1fr))_minmax(11rem,1fr)_auto]"
        }
        more={{
          active: [routing && upstreamText, routing && alias, routing && router, endpoint, modelType].filter(Boolean).length,
          onClear: () => {
            setUpstreamText("");
            setAlias(ANY);
            setRouter(ANY);
            setEndpoint(ANY);
            setModelType("");
          },
          children: (
            <>
              {routing && (<>
              <ComboboxField free label="Upstream model" hint="Any name, including one no model uses any more." placeholder="All upstream models" value={upstreamText} onChange={setUpstreamText} options={upstreamSuggestions.map((name) => ({ value: name, label: name }))} />
              <CatalogFilter label="Alias" kinds={["alias"]} value={alias} onChange={setAlias} entryValue={byId} leading={provenanceModes("alias")} />
              <CatalogFilter label="Router" kinds={["router"]} value={router} onChange={setRouter} entryValue={byId} leading={provenanceModes("router")} />
              </>)}
              <ComboboxField label="Endpoint" value={endpoint} onChange={setEndpoint} options={[{ value: ANY, label: "All endpoints" }, ...ENDPOINTS]} />
              <SelectField label="Model type" value={modelType} onChange={(x) => setModelType(x as ModelType | "")} options={[{ value: "", label: "All types" }, ...MODEL_TYPES]} />
            </>
          ),
        }}
      >
        <RangeFields range={range} teams={teams.data} />
        {routing && <CatalogFilter label="Model" kinds={["model"]} value={model} onChange={setModel} leading={[{ value: ANY, label: "All models" }]} />}
        <SelectField label="Group by" value={groupBy} onChange={setGroupBy} options={groupOptions} />
      </FilterBar>
      <Insights filters={{ ...filters, group_by: undefined }} admin={routing} ranged={ranged} lens={lens} />
      {!costLens && total.audio > 0 && <p className="text-muted-foreground">Audio transcribed: {fmtSeconds(total.audio)} s.</p>}
      {costLens && total.unpriced > 0 && <p className="text-muted-foreground">{n(total.unpriced)} request(s) had no price configured and are counted at $0.</p>}
      <div className="my-4">
        <UsageChart
          rows={ranged ? chart.data : []}
          labelOf={seriesLabel}
          dimLabel={dimLabel}
          singleSeries={chartDim === "day"}
          from={range.from}
          to={range.to}
          metric={costLens ? "cost" : usageMetric}
          onMetricChange={setUsageMetric}
          metrics={costLens ? ["cost"] : USAGE_METRICS}
          loading={ranged && chart.isPending}
          error={chart.error}
          onRetry={() => void chart.refetch()}
          empty={ranged ? "No usage in this range." : "Choose a date range."}
          title={costLens ? "Cost by day" : "Usage by day"}
        />
      </div>
      {ranged && <Leaderboard from={rangeQuery.from} to={rangeQuery.to} teamId={range.teamId} zone={range.zone.param} lens={lens} seesOthers={routing || ownerOffered} />}
      <DataTable
        rows={query.data}
        loading={query.isPending || teams.isPending || (directoryEnabled && directory.isPending)}
        error={query.error ?? teams.error ?? (directoryEnabled ? directory.error : null)}
        onRetry={() => void query.refetch()}
        rowKey={(row) => [row.zone, row.date, row.called, row.status_class, row.endpoint, row.model_type, row.requested, row.model_id ?? row.model, row.alias_id ?? row.alias, row.router_id ?? row.router, row.upstream_model, row.team_id, row.owner_kind, row.owner_id, row.key_id].join("|")}
        empty="No usage in this period."
        columns={[...dims.flatMap((dimension) => dimCols[dimension] ?? []), ...(costLens ? costColumns : usageColumns), logColumn]}
      />
    </>
  );
}

/**
 * MCP or A2A traffic: which server or agent, which JSON-RPC method and (for MCP) which tool, with
 * requests, failures and duration — the two protocols carry no model, tokens or cost.
 */
function TrafficUsage({ kind, range }: { kind: "mcp" | "a2a"; range: RangeProps }) {
  const groups = kind === "mcp" ? MCP_GROUPS : A2A_GROUPS;
  const [groupBy, setGroupBy] = useState(groups[0].value);
  const teams = useTeams();
  const ranged = !!range.from && !!range.to;
  const filters: UsageQuery = {
    from: dayStart(range.from),
    to: dayStart(shiftDay(range.to, 1)),
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
      <FilterBar testId="usage-filters" className="lg:grid-cols-[10rem_10rem_repeat(3,minmax(0,1fr))]">
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
