import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api";
import { fmtUsd, PageHeader } from "../components/Bits";
import { SelectField, TextField } from "../components/Form";
import { DataTable, type Column } from "../components/Table";
import { useAllKeys, useNames, useTree } from "../hooks";

const n = (x: number) => x.toLocaleString();
const GROUPS = [
  { value: "day,model", label: "Day × model" },
  { value: "day", label: "Day" },
  { value: "model", label: "Model" },
  { value: "org", label: "Organization" },
  { value: "team", label: "Team" },
  { value: "project", label: "Project" },
  { value: "key", label: "API key" },
];

const isoDay = (d: Date) => d.toISOString().slice(0, 10);
const today = new Date();
const daysAgo = (k: number) => new Date(today.getTime() - k * 86_400_000);

type Row = Awaited<ReturnType<typeof api.usage>>[number];

export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card stat">
      <div className="muted">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

export function Usage() {
  const [from, setFrom] = useState(isoDay(daysAgo(29)));
  const [to, setTo] = useState(isoDay(today));
  const [groupBy, setGroupBy] = useState("day,model");
  const tree = useTree();
  const keys = useAllKeys(tree.data?.projects);
  const orgName = useNames(tree.data?.orgs);
  const teamName = useNames(tree.data?.teams);
  const projectName = useNames(tree.data?.projects);
  const keyName = useNames(keys.data);

  // `to` is exclusive on the server; include the chosen end day.
  const range = { from: `${from}T00:00:00Z`, to: `${isoDay(new Date(new Date(`${to}T00:00:00Z`).getTime() + 86_400_000))}T00:00:00Z` };
  const q = useQuery({ queryKey: ["usage", from, to, groupBy], queryFn: () => api.usage({ ...range, group_by: groupBy }), enabled: !!from && !!to });

  const total = useMemo(
    () => (q.data ?? []).reduce((a, r) => ({ requests: a.requests + r.requests, tokens: a.tokens + r.total_tokens, cost: a.cost + r.cost_nano_usd, unpriced: a.unpriced + r.unknown_cost_count }), { requests: 0, tokens: 0, cost: 0, unpriced: 0 }),
    [q.data],
  );

  const dims = groupBy.split(",");
  const dimCols: Record<string, Column<Row>> = {
    day: { key: "day", header: "Date", sort: (a, b) => (a.date ?? "").localeCompare(b.date ?? ""), cell: (r) => r.date ?? "—" },
    model: { key: "model", header: "Model", sort: (a, b) => (a.model ?? "").localeCompare(b.model ?? ""), cell: (r) => r.model ?? "—" },
    org: { key: "org", header: "Organization", cell: (r) => orgName(r.org_id) },
    team: { key: "team", header: "Team", cell: (r) => teamName(r.team_id) },
    project: { key: "project", header: "Project", cell: (r) => projectName(r.project_id) },
    key: { key: "key", header: "API key", cell: (r) => keyName(r.key_id) },
  };

  return (
    <>
      <PageHeader title="Usage" description="Requests, tokens and spend, aggregated from gateway usage events." />
      <section className="card pad filters">
        <TextField label="From" type="date" value={from} onChange={setFrom} />
        <TextField label="To (inclusive)" type="date" value={to} onChange={setTo} />
        <SelectField label="Group by" value={groupBy} onChange={setGroupBy} options={GROUPS} />
      </section>
      <div className="stats">
        <Stat label="Requests" value={n(total.requests)} />
        <Stat label="Tokens" value={n(total.tokens)} />
        <Stat label="Spend" value={fmtUsd(total.cost)} />
      </div>
      {total.unpriced > 0 && <p className="muted">{n(total.unpriced)} request(s) had no price configured and are counted at $0.</p>}
      <DataTable
        rows={q.data}
        loading={q.isPending && q.fetchStatus !== "idle"}
        error={q.error}
        onRetry={() => void q.refetch()}
        rowKey={(r) => [r.date, r.model, r.org_id, r.team_id, r.project_id, r.key_id].join("|")}
        empty="No usage in this period."
        columns={[
          ...dims.map((d) => dimCols[d]),
          { key: "requests", header: "Requests", align: "right", sort: (a, b) => a.requests - b.requests, cell: (r) => n(r.requests) },
          { key: "prompt", header: "Prompt tokens", align: "right", cell: (r) => n(r.prompt_tokens) },
          { key: "completion", header: "Completion tokens", align: "right", cell: (r) => n(r.completion_tokens) },
          { key: "cost", header: "Cost", align: "right", sort: (a, b) => a.cost_nano_usd - b.cost_nano_usd, cell: (r) => fmtUsd(r.cost_nano_usd) },
        ]}
      />
    </>
  );
}
