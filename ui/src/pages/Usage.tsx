import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api";
import { fmtUsd, PageHeader } from "../components/Bits";
import { SelectField, TextField } from "../components/Form";
import { DataTable, type Column } from "../components/Table";
import { useNames, useTeamDirectory, useTeams } from "../hooks";

const n = (x: number) => x.toLocaleString();
const GROUPS = [
  { value: "day,requested", label: "Day × requested" },
  { value: "day", label: "Day" },
  { value: "requested", label: "Model" },
  { value: "team", label: "Team" },
  { value: "owner", label: "Owner" },
  { value: "key", label: "API key" },
  { value: "team,owner,key", label: "Team × owner × key" },
];

const isoDay = (date: Date) => date.toISOString().slice(0, 10);
const today = new Date();
const daysAgo = (count: number) => new Date(today.getTime() - count * 86_400_000);

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
  const [groupBy, setGroupBy] = useState("day,requested");
  const [teamId, setTeamId] = useState("");
  const teams = useTeams();
  const directory = useTeamDirectory(teams.data);
  const teamName = useNames(teams.data);
  const memberName = useNames(directory.data?.members.map((member) => ({ id: member.user_id, name: member.email })));
  const accountName = useNames(directory.data?.serviceAccounts);

  // `to` is exclusive on the server; include the chosen end day.
  const range = {
    from: `${from}T00:00:00Z`,
    to: `${isoDay(new Date(new Date(`${to}T00:00:00Z`).getTime() + 86_400_000))}T00:00:00Z`,
  };
  const query = useQuery({
    queryKey: ["usage", from, to, groupBy, teamId],
    queryFn: () => api.usage({ ...range, group_by: groupBy, team_id: teamId || undefined }),
    enabled: !!from && !!to,
  });

  const total = useMemo(
    () =>
      (query.data ?? []).reduce(
        (acc, row) => ({
          requests: acc.requests + row.requests,
          tokens: acc.tokens + row.total_tokens,
          cost: acc.cost + row.cost_nano_usd,
          unpriced: acc.unpriced + row.unknown_cost_count,
        }),
        { requests: 0, tokens: 0, cost: 0, unpriced: 0 },
      ),
    [query.data],
  );

  const dims = groupBy.split(",");
  const ownerLabel = (kind: string | null | undefined, id: string | null | undefined) => {
    if (!id) return "—";
    return kind === "service_account" ? accountName(id) : memberName(id);
  };
  const dimCols: Record<string, Column<Row>> = {
    day: { key: "day", header: "Date", sort: (a, b) => (a.date ?? "").localeCompare(b.date ?? ""), cell: (row) => row.date ?? "—" },
    requested: { key: "requested", header: "Model", sort: (a, b) => (a.requested ?? "").localeCompare(b.requested ?? ""), cell: (row) => row.requested ?? "—" },
    team: { key: "team", header: "Team", cell: (row) => teamName(row.team_id) },
    owner: { key: "owner", header: "Owner", cell: (row) => ownerLabel(row.owner_kind, row.owner_id) },
    key: { key: "key", header: "API key", cell: (row) => row.key_id?.slice(0, 8) ?? "—" },
  };

  return (
    <>
      <PageHeader title="Usage" description="Requests, tokens, and spend grouped by team, owner, or key." />
      <section className="card pad filters">
        <TextField label="From" type="date" value={from} onChange={setFrom} />
        <TextField label="To (inclusive)" type="date" value={to} onChange={setTo} />
        <SelectField label="Team" value={teamId} onChange={setTeamId} options={[{ value: "", label: "All teams" }, ...(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))]} />
        <SelectField label="Group by" value={groupBy} onChange={setGroupBy} options={GROUPS} />
      </section>
      <div className="stats">
        <Stat label="Requests" value={n(total.requests)} />
        <Stat label="Tokens" value={n(total.tokens)} />
        <Stat label="Spend" value={fmtUsd(total.cost)} />
      </div>
      {total.unpriced > 0 && <p className="muted">{n(total.unpriced)} request(s) had no price configured and are counted at $0.</p>}
      <DataTable
        rows={query.data}
        loading={query.isPending || teams.isPending || directory.isPending}
        error={query.error ?? teams.error ?? directory.error}
        onRetry={() => void query.refetch()}
        rowKey={(row) => [row.date, row.requested, row.team_id, row.owner_kind, row.owner_id, row.key_id].join("|")}
        empty="No usage in this period."
        columns={[
          ...dims.map((dimension) => dimCols[dimension]),
          { key: "requests", header: "Requests", align: "right", sort: (a, b) => a.requests - b.requests, cell: (row) => n(row.requests) },
          { key: "prompt", header: "Prompt tokens", align: "right", cell: (row) => n(row.prompt_tokens) },
          { key: "completion", header: "Completion tokens", align: "right", cell: (row) => n(row.completion_tokens) },
          { key: "cost", header: "Cost", align: "right", sort: (a, b) => a.cost_nano_usd - b.cost_nano_usd, cell: (row) => fmtUsd(row.cost_nano_usd) },
        ]}
      />
    </>
  );
}
