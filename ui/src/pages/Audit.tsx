import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Schemas } from "../api";
import { fmtDateTime, PageHeader } from "../components/Bits";
import { SelectField, TextField } from "../components/Form";
import { Badge } from "../components/ui/badge";
import { ComboboxField } from "../components/ui/combobox-field";
import { DataTable } from "../components/Table";
import { FilterBar } from "../components/FilterBar";
import { useCan, useNames, useOrgLayer, useTeams } from "../hooks";
import { dayStart, shiftDay } from "../lib/days";
import { KeyPill } from "../components/KeyPill";

type Via = NonNullable<Schemas["AuditOut"]["via"]>;

const VIA_LABEL: Record<Via, string> = { session: "Session", pat: "PAT", mcp: "MCP" };
const VIA_VARIANT: Record<Via, "muted" | "outline" | "info"> = { session: "muted", pat: "outline", mcp: "info" };
const VIA_FILTERS = [
  { value: "", label: "Any" },
  { value: "session", label: "Session" },
  { value: "pat", label: "PAT" },
  { value: "mcp", label: "MCP" },
];

export function Audit() {
  const [action, setAction] = useState("");
  const [teamId, setTeamId] = useState("");
  const [orgId, setOrgId] = useState("");
  const [via, setVia] = useState<"" | Via>("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const perms = useCan();
  const teams = useTeams();
  // The org layer (the filter and the Org column) is hidden for a single-org installation.
  const { orgs, show: showOrg } = useOrgLayer();
  const orgName = useNames(orgs.data);
  // Rows are per administered team; offering a team the caller only belongs to would be a 403.
  const administered = (teams.data ?? []).filter((team) => perms.teamAdmin(team.id));
  const teamName = useNames(administered);
  const q = useQuery({
    queryKey: ["audit", action, teamId, orgId, via, from, to],
    queryFn: () =>
      api.audit({
        action: action.trim() || undefined,
        team_id: teamId || undefined,
        org_id: orgId || undefined,
        via: via || undefined,
        // The range is inclusive on screen and exclusive at its end, as on Usage and Requests.
        from: from ? dayStart(from) || undefined : undefined,
        to: to ? dayStart(shiftDay(to, 1)) || undefined : undefined,
      }),
  });
  return (
    <>
      <PageHeader title="Audit log" />
      <FilterBar
        testId="audit-filters"
        className={
          showOrg
            ? "lg:grid-cols-[10rem_10rem_10rem_repeat(3,minmax(0,1fr))]"
            : "lg:grid-cols-[10rem_10rem_10rem_repeat(2,minmax(0,1fr))]"
        }
      >
        <TextField label="From" type="date" value={from} onChange={setFrom} hint="Empty has no start." />
        <TextField label="To" type="date" value={to} onChange={setTo} hint="Inclusive; empty ends now." />
        {showOrg && (
          <ComboboxField
            label="Org"
            value={orgId}
            onChange={setOrgId}
            options={[{ value: "", label: "All orgs" }, ...(orgs.data ?? []).map((org) => ({ value: org.id, label: org.name }))]}
          />
        )}
        <ComboboxField
          label="Team"
          value={teamId}
          onChange={setTeamId}
          options={[{ value: "", label: "All teams" }, ...administered.map((team) => ({ value: team.id, label: team.name }))]}
        />
        <SelectField label="Via" value={via} onChange={(value) => setVia(value as "" | Via)} options={VIA_FILTERS} />
        <TextField label="Action" value={action} onChange={setAction} placeholder="Any action" />
      </FilterBar>
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
          ...(showOrg ? [{ key: "org", header: "Org", cell: (r: { org_id: string | null }) => orgName(r.org_id) }] : []),
          { key: "actor", header: "Actor", cell: (r) => (<><span>{r.actor_type}</span> <code className="text-muted-foreground">{r.actor_id?.slice(0, 8)}</code></>) },
          {
            key: "via",
            header: "Via",
            cell: (r) =>
              r.via ? (
                <Badge variant={VIA_VARIANT[r.via]} data-testid="audit-via">
                  {VIA_LABEL[r.via]}
                </Badge>
              ) : null,
          },
          { key: "action", header: "Action", sort: (a, b) => a.action.localeCompare(b.action), cell: (r) => <code>{r.action}</code> },
          { key: "resource", header: "Resource", cell: (r) => (r.resource_type === "key" ? <KeyPill keyRef={r.key} fallbackId={r.resource_id} /> : <>{r.resource_type} <code className="text-muted-foreground">{r.resource_id?.slice(0, 8)}</code></>) },
          { key: "details", header: "Details", cell: (r) => <code className="text-muted-foreground">{Object.keys(r.details).length ? JSON.stringify(r.details) : ""}</code> },
        ]}
      />
    </>
  );
}
