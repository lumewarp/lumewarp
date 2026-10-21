import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { fmtDateTime, PageHeader } from "../components/Bits";
import { DataTable } from "../components/Table";

export function Audit() {
  const q = useQuery({ queryKey: ["audit"], queryFn: () => api.audit() });
  return (
    <>
      <PageHeader title="Audit log" description="Who changed what, newest first." />
      <DataTable
        rows={q.data}
        loading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        rowKey={(r) => r.id}
        pageSize={20}
        empty="No audit entries."
        columns={[
          { key: "at", header: "When", sort: (a, b) => a.at.localeCompare(b.at), cell: (r) => fmtDateTime(r.at) },
          { key: "actor", header: "Actor", sort: (a, b) => a.actor.localeCompare(b.actor), cell: (r) => r.actor },
          { key: "action", header: "Action", cell: (r) => <code>{r.action}</code> },
          { key: "target", header: "Target", cell: (r) => r.target },
        ]}
      />
    </>
  );
}
