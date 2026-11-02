import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, type UsageQuery, type UsageRow } from "../api";
import { Badge, fmtUsd, PageHeader } from "../components/Bits";
import { Field, SelectField, TextField } from "../components/Form";
import { Input } from "../components/ui/input";
import { DataTable, type Column } from "../components/Table";
import { useCatalog, useModels, useNames, useTeamDirectory, useTeams } from "../hooks";

const n = (x: number) => x.toLocaleString();

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

const isoDay = (date: Date) => date.toISOString().slice(0, 10);
const today = new Date();
const daysAgo = (count: number) => new Date(today.getTime() - count * 86_400_000);

type Row = UsageRow;

export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card stat">
      <div className="muted">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
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

export function Usage() {
  const [from, setFrom] = useState(isoDay(daysAgo(29)));
  const [to, setTo] = useState(isoDay(today));
  const [groupBy, setGroupBy] = useState("model");
  const [teamId, setTeamId] = useState("");
  const [model, setModel] = useState(ANY);
  const [upstreamText, setUpstreamText] = useState("");
  const upstreamModel = useDebounced(upstreamText.trim(), 300);
  const [alias, setAlias] = useState(ANY);
  const [router, setRouter] = useState(ANY);
  const teams = useTeams();
  const catalog = useCatalog();
  const models = useModels();
  const directory = useTeamDirectory(teams.data);
  const teamName = useNames(teams.data);
  const memberName = useNames(directory.data?.members.map((member) => ({ id: member.user_id, name: member.email })));
  const accountName = useNames(directory.data?.serviceAccounts);

  // `to` is exclusive on the server; include the chosen end day.
  const range = {
    from: `${from}T00:00:00Z`,
    to: `${isoDay(new Date(new Date(`${to}T00:00:00Z`).getTime() + 86_400_000))}T00:00:00Z`,
  };
  const filters: UsageQuery = {
    ...range,
    group_by: groupBy,
    team_id: teamId || undefined,
    model: model || undefined,
    upstream_model: upstreamModel || undefined,
    ...provenanceFilter("alias", alias),
    ...provenanceFilter("router", router),
  };
  const query = useQuery({
    queryKey: ["usage", filters],
    queryFn: () => api.usage(filters),
    enabled: !!from && !!to,
  });

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
    return kind === "service_account" ? accountName(id) : memberName(id);
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
      <PageHeader title="Usage" description="Requests, tokens and spend. Spend is for the model that served each request; alias and router show how it was reached." />
      <section className="card pad filters">
        <TextField label="From" type="date" value={from} onChange={setFrom} />
        <TextField label="To (inclusive)" type="date" value={to} onChange={setTo} />
        <SelectField label="Team" value={teamId} onChange={setTeamId} options={[{ value: "", label: "All teams" }, ...(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))]} />
        <SelectField label="Model" value={model} onChange={setModel} options={[{ value: ANY, label: "All models" }, ...names("model").map((name) => ({ value: name, label: name }))]} />
        <Field label="Upstream model" hint="Any name, including one no model uses any more.">
          {(p) => (
            <>
              <Input {...p} list="usage-upstream-models" value={upstreamText} placeholder="All upstream models" autoComplete="off" onChange={(e) => setUpstreamText(e.target.value)} />
              <datalist id="usage-upstream-models">
                {upstreamSuggestions.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </>
          )}
        </Field>
        <SelectField label="Alias" value={alias} onChange={setAlias} options={provenanceOptions("alias")} />
        <SelectField label="Router" value={router} onChange={setRouter} options={provenanceOptions("router")} />
        <SelectField label="Group by" value={groupBy} onChange={setGroupBy} options={GROUPS} />
      </section>
      <div className="stats">
        <Stat label="Requests" value={n(total.requests)} />
        <Stat label="Total tokens" value={n(total.tokens)} />
        <Stat label="Prompt / completion" value={`${n(total.prompt)} / ${n(total.completion)}`} />
        <Stat label="Spend" value={fmtUsd(total.cost)} />
      </div>
      {total.unpriced > 0 && <p className="muted">{n(total.unpriced)} request(s) had no price configured and are counted at $0.</p>}
      <DataTable
        rows={query.data}
        loading={query.isPending || teams.isPending || directory.isPending}
        error={query.error ?? teams.error ?? directory.error}
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
