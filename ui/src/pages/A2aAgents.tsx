import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { api, type A2aAgent } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useA2aAgents, useCan, useMut } from "../hooks";
import { cardStatus } from "../lib/a2aCard";

/** Which card a gateway serves for an agent: fetched, pasted or none (`cardStatus`). */
export function CardBadge({ agent }: { agent: A2aAgent }) {
  const status = cardStatus(agent);
  return (
    <span title={status.title}>
      <Badge tone={status.tone}>{status.label}</Badge>
    </span>
  );
}

export function A2aAgents() {
  const perms = useCan();
  const navigate = useNavigate();
  const list = useA2aAgents();
  const [deleting, setDeleting] = useState<A2aAgent | null>(null);
  const remove = useMut((id: string) => api.deleteA2aAgent(id), "Agent deleted");

  return (
    <>
      <PageHeader
        title="A2A agents"
        actions={
          <CreateButton can={perms.isInstallationAdmin} onClick={() => void navigate({ to: "/a2a-agents/new" })}>
            New agent
          </CreateButton>
        }
      />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No A2A agents yet."
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (r) => (
              <Link to="/a2a-agents/$id" params={{ id: r.id }} className="font-semibold hover:underline">
                {r.name}
              </Link>
            ),
          },
          { key: "url", header: "Agent URL", cell: (r) => <code className="[overflow-wrap:anywhere]">{r.url}</code> },
          {
            key: "auth",
            header: "Auth",
            cell: (r) => <Badge tone={r.auth.plugin === "none" ? "muted" : "info"}>{r.auth.plugin}</Badge>,
          },
          { key: "middleware", header: "Middleware", cell: (r) => (r.middleware ? <code>{r.middleware.plugin}</code> : <span className="text-muted-foreground">—</span>) },
          { key: "card", header: "Card", cell: (r) => <CardBadge agent={r} /> },
          {
            key: "actions",
            header: "",
            cell: (r) => (
              <RowActions
                canEdit={perms.isInstallationAdmin}
                canDelete={perms.isInstallationAdmin}
                onEdit={() => void navigate({ to: "/a2a-agents/$id", params: { id: r.id } })}
                onDelete={() => setDeleting(r)}
              />
            ),
          },
        ]}
      />
      <ConfirmDialog
        open={!!deleting}
        title="Delete agent"
        message={`Delete “${deleting?.name}”? Its grants stay, but clients can no longer call it.`}
        onConfirm={() => remove.mutateAsync(deleting!.id)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}
