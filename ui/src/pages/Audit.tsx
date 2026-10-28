import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { fmtDateTime, PageHeader } from "../components/Bits";
import { SelectField, TextField } from "../components/Form";
import { DataTable } from "../components/Table";
import { useNames, useTeams } from "../hooks";

export function Audit() {
  const [action, setAction] = useState("");
  const [teamId, setTeamId] = useState("");
  const teams = useTeams();
  const teamName = useNames(teams.data);
  const q = useQuery({
    queryKey: ["audit", action, teamId],
    queryFn: () => api.audit({ action: action.trim() || undefined, team_id: teamId || undefined }),
  });
  return (
    <>
      <PageHeader title="Audit log" description="Every change made through the API. Secrets and hashes are never recorded." />
      <section className="card pad filters">
        <SelectField
          label="Team"
          value={teamId}
          onChange={setTeamId}
          options={[{ value: "", label: "All teams" }, ...(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))]}
        />
        <TextField label="Action" value={action} onChange={setAction} placeholder="e.g. key.create" />
      </section>
      <DataTable
        rows={q.data}
        loading={q.isPending || teams.isPending}
        error={q.error ?? teams.error}
        onRetry={() => { void q.refetch(); void teams.refetch(); }}
        rowKey={(r) => String(r.id)}
        empty="No audit entries."
        columns={[
          { key: "ts", header: "When", sort: (a, b) => a.ts.localeCompare(b.ts), cell: (r) => fmtDateTime(r.ts) },
          { key: "team", header: "Team", cell: (r) => teamName(r.team_id) },
          { key: "actor", header: "Actor", cell: (r) => (<><span>{r.actor_type}</span> <code className="muted">{r.actor_id?.slice(0, 8)}</code></>) },
          { key: "action", header: "Action", sort: (a, b) => a.action.localeCompare(b.action), cell: (r) => <code>{r.action}</code> },
          { key: "resource", header: "Resource", cell: (r) => (<>{r.resource_type} <code className="muted">{r.resource_id?.slice(0, 8)}</code></>) },
          { key: "details", header: "Details", cell: (r) => <code className="muted">{Object.keys(r.details).length ? JSON.stringify(r.details) : ""}</code> },
        ]}
      />
    </>
  );
}
