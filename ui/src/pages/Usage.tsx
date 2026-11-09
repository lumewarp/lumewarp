import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, type UsageQuery, type UsageRow } from "../api";
import { Badge, fmtNumber as n, fmtUsd, PageHeader } from "../components/Bits";
import { SelectField, TextField } from "../components/Form";
import { UsageChart } from "../components/UsageChart";
import { Card } from "../components/ui/card";
import { ComboboxField } from "../components/ui/combobox-field";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { DataTable, type Column } from "../components/Table";
import { dayStart, isoDay, shiftDay } from "../lib/days";
import { useUsageMetric } from "../lib/usageMetric";
import { useCan, useCatalog, useMe, useModels, useNames, useTeamDirectory, useTeams } from "../hooks";

/** `value`, once it has stopped changing for `ms`: typed filters do not query on every keystroke. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

const GROUPS = [
  { value: "model", label: "Model" },
  { value: "upstream_model", label: "Upstream model" },
  { value: "alias", label: "Alias" },
  { value: "router", label: "Router" },
  { value: "requested", label: "Requested name" },
  { value: "team", label: "Team" },
  { value: "owner", label: "Owner" },
  { value: "key", label: "API key" },
  { value: "day", label: "Day" },
  { value: "day,model", label: "Day × model" },
  { value: "team,owner,key", label: "Team × owner × key" },
];

/** MCP traffic read by server, method and tool; the first entry is the default (the most detail). */
const MCP_GROUPS = [
  { value: "target,method,tool", label: "Server × method × tool" },
  { value: "target,method", label: "Server × method" },
  { value: "target,tool", label: "Server × tool" },
  { value: "method", label: "Method" },
  { value: "tool", label: "Tool" },
  { value: "target", label: "Server" },
  { value: "client_fallback,client_protocol,upstream_protocol", label: "Fallback × client protocol × upstream protocol" },
  { value: "client,client_version,client_fallback", label: "Client × version × fallback" },
];

/** How an MCP client negotiates, derived per client over the range by the API. */
const FALLBACK_LABELS: Record<string, string> = {
  falls_back: "Falls back",
  no_fallback: "No fallback",
  no_probe: "Never probes",
  unknown: "Unknown",
};

/** A2A traffic read by agent and method; the first entry is the default. */
const A2A_GROUPS = [
  { value: "target,method", label: "Agent × method" },
  { value: "method", label: "Method" },
  { value: "target", label: "Agent" },
];

const today = new Date();
const daysAgo = (count: number) => new Date(today.getTime() - count * 86_400_000);

type Row = UsageRow;

export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Card className="gap-0 p-3.5">
      <div className="text-muted-foreground">{label}</div>
      <div className="text-2xl font-bold tabular-nums">{value}</div>
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
}

function RangeFields({ range, teams }: { range: RangeProps; teams: { id: string; name: string }[] | undefined }) {
  return (
    <>
      <TextField label="From" type="date" value={range.from} onChange={range.onFrom} />
      <TextField label="To (inclusive)" type="date" value={range.to} onChange={range.onTo} />
      <ComboboxField
        label="Team"
        value={range.teamId}
        onChange={range.onTeam}
        options={[{ value: "", label: "All teams" }, ...(teams ?? []).map((team) => ({ value: team.id, label: team.name }))]}
      />
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

/** A sortable column for a plain string dimension, showing a dash when the request had none. */
function textColumn(key: string, header: string, value: (row: Row) => string | null | undefined): Column<Row> {
  return {
    key,
    header,
    sort: (a, b) => (value(a) ?? "").localeCompare(value(b) ?? ""),
    cell: (row) => (value(row) ? <code className="[overflow-wrap:anywhere]">{value(row)}</code> : <span className="text-muted-foreground">—</span>),
  };
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
  const range: RangeProps = { from, to, teamId, onFrom: setFrom, onTo: setTo, onTeam: setTeamId };
  return (
    <>
      <PageHeader title="Usage" hint="Spend is for the model that served each request." />
      <Tabs value={tab} onValueChange={(value) => setTab(value as typeof tab)}>
        <TabsList variant="line">
          <TabsTrigger value="llm">LLM</TabsTrigger>
          <TabsTrigger value="mcp">MCP</TabsTrigger>
          <TabsTrigger value="a2a">A2A</TabsTrigger>
        </TabsList>
        <TabsContent value="llm">
          <LlmUsage range={range} />
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

function LlmUsage({ range }: { range: RangeProps }) {
  const [groupBy, setGroupBy] = useState("model");
  const [model, setModel] = useState(ANY);
  const [upstreamText, setUpstreamText] = useState("");
  const upstreamModel = useDebounced(upstreamText.trim(), 300);
  const [alias, setAlias] = useState(ANY);
  const [router, setRouter] = useState(ANY);
  const perms = useCan();
  const me = useMe();
  const teams = useTeams();
  const catalog = useCatalog();
  // Upstream suggestions come from the routing details, which only an installation admin
  // may read; member and service-account names are the team-admin view. A plain member
  // never requests either.
  const models = useModels(perms.canReadRouting);
  // Only the teams this caller administers: a mixed-role user (admin of A, member of B)
  // must not ask B for its member or service-account lists. The hook's query key is built
  // from the same set, so changing the role changes the cache entry too.
  const administered = (teams.data ?? []).filter((team) => perms.teamAdmin(team.id));
  const directoryEnabled = administered.length > 0;
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
    group_by: groupBy,
    team_id: range.teamId || undefined,
    model: model || undefined,
    upstream_model: upstreamModel || undefined,
    ...provenanceFilter("alias", alias),
    ...provenanceFilter("router", router),
  };
  const query = useQuery({
    queryKey: ["usage", filters],
    queryFn: () => api.usage(filters),
    enabled: !!range.from && !!range.to,
  });
  // The chart keeps the page's range and filters but is always day × model, whatever the table groups by.
  const chartFilters: UsageQuery = { ...filters, group_by: "day,model" };
  const chart = useQuery({
    queryKey: ["usage", chartFilters],
    queryFn: () => api.usage(chartFilters),
    enabled: !!range.from && !!range.to,
    // A refusal or an outage is shown in the card, with its own retry: it must not stall the page.
    retry: false,
  });
  const [metric, setMetric] = useUsageMetric();
  // A half-filled range is not an error: the query stays disabled and the chart says what is missing.
  const ranged = !!range.from && !!range.to;

  const total = useMemo(
    () =>
      (query.data ?? []).reduce(
        (acc, row) => ({
          requests: acc.requests + row.requests,
          prompt: acc.prompt + row.prompt_tokens,
          completion: acc.completion + row.completion_tokens,
          tokens: acc.tokens + row.total_tokens,
          cost: acc.cost + row.cost_nano_usd,
          unpriced: acc.unpriced + row.unknown_cost_count,
        }),
        { requests: 0, prompt: 0, completion: 0, tokens: 0, cost: 0, unpriced: 0 },
      ),
    [query.data],
  );

  const names = (kind: "model" | "alias" | "router") => (catalog.data ?? []).filter((e) => e.kind === kind).map((e) => e.name);
  // Suggestions only: the filter takes any string, as old usage keeps the upstream name it was served under.
  const upstreamSuggestions = [...new Set((models.data ?? []).map((m) => m.upstream_model))].sort();
  const provenanceOptions = (kind: "alias" | "router") => [
    { value: ANY, label: "Any" },
    { value: NONE, label: kind === "alias" ? "None (no alias)" : "None (no router)" },
    { value: HAS, label: kind === "alias" ? "Via any alias" : "Via any router" },
    ...(catalog.data ?? []).filter((e) => e.kind === kind).map((e) => ({ value: `${ID}${e.id}`, label: e.name })),
  ];

  const dims = groupBy.split(",");
  const ownerLabel = (kind: string | null | undefined, id: string | null | undefined) => {
    if (!id) return "—";
    return kind === "service_account" ? accountName(id) : ownName(id);
  };
  const dimCols: Record<string, Column<Row>[]> = {
    day: [{ key: "day", header: "Date", sort: (a, b) => (a.date ?? "").localeCompare(b.date ?? ""), cell: (row) => row.date ?? "—" }],
    requested: [{ key: "requested", header: "Requested", sort: (a, b) => (a.requested ?? "").localeCompare(b.requested ?? ""), cell: (row) => row.requested ?? "—" }],
    model: [
      { key: "model", header: "Model", sort: (a, b) => (a.model ?? "").localeCompare(b.model ?? ""), cell: (row) => <strong>{row.model || "—"}</strong> },
      { key: "upstream_model", header: "Upstream model", cell: (row) => <UpstreamModel row={row} /> },
      { key: "via_alias", header: "Via alias", cell: (row) => <Provenance single={row.alias} list={row.via_aliases} /> },
      { key: "via_router", header: "Via router", cell: (row) => <Provenance single={row.router} list={row.via_routers} /> },
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
    key: [{ key: "key", header: "API key", cell: (row) => row.key_id?.slice(0, 8) ?? "—" }],
  };
  const peak = Math.max(1, ...(query.data ?? []).map((r) => r.total_tokens));

  return (
    <>
      <Card className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3 p-4">
        <RangeFields range={range} teams={teams.data} />
        <ComboboxField label="Model" value={model} onChange={setModel} options={[{ value: ANY, label: "All models" }, ...names("model").map((name) => ({ value: name, label: name }))]} />
        <ComboboxField free label="Upstream model" hint="Any name, including one no model uses any more." placeholder="All upstream models" value={upstreamText} onChange={setUpstreamText} options={upstreamSuggestions.map((name) => ({ value: name, label: name }))} />
        <ComboboxField label="Alias" value={alias} onChange={setAlias} options={provenanceOptions("alias")} />
        <ComboboxField label="Router" value={router} onChange={setRouter} options={provenanceOptions("router")} />
        <SelectField label="Group by" value={groupBy} onChange={setGroupBy} options={GROUPS} />
      </Card>
      <div className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3">
        <Stat label="Requests" value={n(total.requests)} />
        <Stat label="Total tokens" value={n(total.tokens)} />
        <Stat label="Prompt / completion" value={`${n(total.prompt)} / ${n(total.completion)}`} />
        <Stat label="Spend" value={fmtUsd(total.cost)} />
      </div>
      {total.unpriced > 0 && <p className="text-muted-foreground">{n(total.unpriced)} request(s) had no price configured and are counted at $0.</p>}
      <div className="my-4">
        <UsageChart
          rows={ranged ? chart.data : []}
          from={range.from}
          to={range.to}
          metric={metric}
          onMetricChange={setMetric}
          loading={ranged && chart.isPending}
          error={chart.error}
          onRetry={() => void chart.refetch()}
          empty={ranged ? "No usage in this range." : "Choose a date range."}
          title="Usage by day"
        />
      </div>
      <DataTable
        rows={query.data}
        loading={query.isPending || teams.isPending || (directoryEnabled && directory.isPending)}
        error={query.error ?? teams.error ?? (directoryEnabled ? directory.error : null)}
        onRetry={() => void query.refetch()}
        rowKey={(row) => [row.date, row.requested, row.model_id ?? row.model, row.alias_id ?? row.alias, row.router_id ?? row.router, row.upstream_model, row.team_id, row.owner_kind, row.owner_id, row.key_id].join("|")}
        empty="No usage in this period."
        columns={[
          ...dims.flatMap((dimension) => dimCols[dimension] ?? []),
          { key: "requests", header: "Requests", align: "right", sort: (a, b) => a.requests - b.requests, cell: (row) => n(row.requests) },
          {
            key: "tokens",
            header: "Total tokens",
            align: "right",
            sort: (a, b) => a.total_tokens - b.total_tokens,
            cell: (row) => (
              <span className="inline-flex items-center justify-end gap-2">
                {n(row.total_tokens)}
                <span className="hidden h-1.5 w-20 overflow-hidden rounded-full bg-muted sm:inline-block" aria-hidden="true">
                  <span className="block h-full bg-primary" style={{ width: `${(row.total_tokens / peak) * 100}%` }} />
                </span>
              </span>
            ),
          },
          { key: "prompt", header: "Prompt", align: "right", hideBelow: "md", cell: (row) => n(row.prompt_tokens) },
          { key: "completion", header: "Completion", align: "right", hideBelow: "md", cell: (row) => n(row.completion_tokens) },
          { key: "cost", header: "Cost", align: "right", sort: (a, b) => a.cost_nano_usd - b.cost_nano_usd, cell: (row) => fmtUsd(row.cost_nano_usd) },
        ]}
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
    group_by: groupBy,
    team_id: range.teamId || undefined,
  };
  const query = useQuery({
    queryKey: ["usage", filters],
    queryFn: () => api.usage(filters),
    enabled: ranged,
  });
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
    client: [textColumn("client", "Client", (row) => row.client)],
    client_version: [textColumn("client_version", "Version", (row) => row.client_version)],
    client_protocol: [textColumn("client_protocol", "Client protocol", (row) => row.client_protocol)],
    upstream_protocol: [textColumn("upstream_protocol", "Upstream protocol", (row) => row.upstream_protocol)],
    client_fallback: [
      {
        key: "client_fallback",
        header: "Fallback",
        sort: (a, b) => (a.client_fallback ?? "").localeCompare(b.client_fallback ?? ""),
        cell: (row) => (row.client_fallback ? FALLBACK_LABELS[row.client_fallback] : <span className="text-muted-foreground">—</span>),
      },
    ],
  };
  const dims = groupBy.split(",");
  return (
    <>
      <Card className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3 p-4">
        <RangeFields range={range} teams={teams.data} />
        <SelectField
          label="Group by"
          value={groupBy}
          onChange={setGroupBy}
          options={groups}
          hint={kind === "mcp" ? "Fallback is judged per client over the whole range: one that sent server/discover and later initialize falls back, one that only probed does not." : undefined}
        />
      </Card>
      <div className="mb-4 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3">
        <Stat label="Requests" value={n(total.requests)} />
        <Stat label="Errors" value={n(total.errors)} />
        <Stat label="Error share" value={fmtShare(total.errors, total.requests)} />
        <Stat label="Mean duration" value={fmtDuration(total.requests ? total.duration / total.requests : 0)} />
      </div>
      <p className="mb-4 text-sm text-muted-foreground">
        An error is an error outcome or a 4xx/5xx answer from the gateway; a client that hangs up is neither.
      </p>
      <DataTable
        rows={ranged ? query.data : []}
        loading={ranged && query.isPending}
        error={query.error ?? teams.error}
        onRetry={() => void query.refetch()}
        rowKey={(row) =>
          [
            row.kind,
            row.target,
            row.method,
            row.tool,
            row.client,
            row.client_version,
            row.client_protocol,
            row.upstream_protocol,
            row.client_fallback,
            row.team_id,
          ].join("|")
        }
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
