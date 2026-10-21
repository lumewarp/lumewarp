import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "../api";
import { PageHeader, usd } from "../components/Bits";
import { DataTable } from "../components/Table";

const n = (x: number) => x.toLocaleString();

export function Usage() {
  const q = useQuery({ queryKey: ["usage"], queryFn: () => api.usage() });
  const total = useMemo(() => (q.data ?? []).reduce((a, r) => ({ requests: a.requests + r.requests, tokens: a.tokens + r.tokens_in + r.tokens_out, cost: a.cost + r.cost_usd }), { requests: 0, tokens: 0, cost: 0 }), [q.data]);
  return (
    <>
      <PageHeader title="Usage" description="Daily requests, tokens and spend by model and team (last 14 days)." />
      <div className="stats">
        <Stat label="Requests" value={n(total.requests)} />
        <Stat label="Tokens" value={n(total.tokens)} />
        <Stat label="Spend" value={usd(total.cost)} />
      </div>
      <DataTable
        rows={q.data}
        loading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        rowKey={(r) => `${r.date}|${r.model}|${r.team}`}
        pageSize={15}
        columns={[
          { key: "date", header: "Date", sort: (a, b) => a.date.localeCompare(b.date), cell: (r) => r.date },
          { key: "model", header: "Model", sort: (a, b) => a.model.localeCompare(b.model), cell: (r) => r.model },
          { key: "team", header: "Team", sort: (a, b) => a.team.localeCompare(b.team), cell: (r) => r.team },
          { key: "req", header: "Requests", align: "right", sort: (a, b) => a.requests - b.requests, cell: (r) => n(r.requests) },
          { key: "in", header: "Tokens in", align: "right", sort: (a, b) => a.tokens_in - b.tokens_in, cell: (r) => n(r.tokens_in) },
          { key: "out", header: "Tokens out", align: "right", sort: (a, b) => a.tokens_out - b.tokens_out, cell: (r) => n(r.tokens_out) },
          { key: "cost", header: "Cost", align: "right", sort: (a, b) => a.cost_usd - b.cost_usd, cell: (r) => usd(r.cost_usd) },
        ]}
      />
    </>
  );
}

export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card stat">
      <div className="muted">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}
