import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { api, type McpServer } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMcpServers, useMut } from "../hooks";

/** The review mode as an administrator reads it. */
export const REVIEW_LABELS: Record<McpServer["review_mode"], string> = {
  tofu: "TOFU",
  manual: "Manual",
  off: "Off",
};

const REVIEW_HINTS: Record<McpServer["review_mode"], string> = {
  tofu: "A tool or prompt seen for the first time is served; a changed definition of a known one waits for approval.",
  manual: "Every tool or prompt an administrator has not approved is withheld from clients.",
  off: "Definitions are never checked; whatever the upstream lists is served.",
};

export const reviewLabel = (mode: string) => REVIEW_LABELS[mode as McpServer["review_mode"]] ?? mode;

/** One count out of the server's `definitions` map, without trusting its prototype. */
export function definitionCount(server: Pick<McpServer, "definitions">, state: string): number {
  return state in server.definitions && typeof server.definitions[state] === "number" ? server.definitions[state] : 0;
}

/** The plugin a server's requests authenticate with; an unusable one is called out where it matters. */
export function AuthBadges({ server }: { server: Pick<McpServer, "auth" | "config_invalid" | "config_problems"> }) {
  return (
    <div className="flex flex-col items-start gap-1">
      <code>{server.auth.plugin}</code>
      {server.config_invalid && (
        <div className="flex flex-col items-start gap-1" data-testid="config-invalid">
          <Badge tone="danger">Config invalid</Badge>
          {server.config_problems.slice(0, 3).map((problem) => (
            <span key={problem} className="max-w-64 text-xs text-muted-foreground [overflow-wrap:anywhere]">
              {problem}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function McpServers() {
  const perms = useCan();
  const navigate = useNavigate();
  const list = useMcpServers();
  const [deleting, setDeleting] = useState<McpServer | null>(null);
  const remove = useMut((id: string) => api.deleteMcpServer(id), "MCP server archived");

  return (
    <>
      <PageHeader
        title="MCP servers"
        hint="What connected gateways proxy at /mcp: one aggregate endpoint across the servers a key may use, and one endpoint per server."
        actions={
          <CreateButton can={perms.isInstallationAdmin} onClick={() => void navigate({ to: "/mcp-servers/new" })}>
            New server
          </CreateButton>
        }
      />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(s) => s.id}
        empty="No MCP servers yet."
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (s) => (
              <Link to="/mcp-servers/$serverId" params={{ serverId: s.id }} className="font-semibold hover:underline">
                {s.name}
              </Link>
            ),
          },
          { key: "url", header: "URL", hideBelow: "md", cell: (s) => <code className="[overflow-wrap:anywhere]">{s.url}</code> },
          { key: "auth", header: "Auth", cell: (s) => <AuthBadges server={s} /> },
          {
            key: "review",
            header: "Review",
            hideBelow: "sm",
            cell: (s) => (
              <span title={REVIEW_HINTS[s.review_mode]}>
                <Badge tone={s.review_mode === "off" ? "warn" : "muted"}>{reviewLabel(s.review_mode)}</Badge>
              </span>
            ),
          },
          { key: "definitions", header: "Definitions", cell: (s) => <Counts server={s} /> },
          {
            key: "actions",
            header: "",
            cell: (s) => (
              <RowActions
                canEdit={perms.isInstallationAdmin}
                canDelete={perms.isInstallationAdmin}
                deleteLabel="Archive"
                onEdit={() => void navigate({ to: "/mcp-servers/$serverId", params: { serverId: s.id } })}
                onDelete={() => setDeleting(s)}
              />
            ),
          },
        ]}
      />
      <ConfirmDialog
        open={!!deleting}
        title="Archive MCP server"
        confirmLabel="Archive"
        message={`Archive “${deleting?.name}”? Gateways stop serving it and the name becomes free; its approved definitions and grants are kept.`}
        onConfirm={() => remove.mutateAsync(deleting!.id)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}

/** Approved, pending and rejected definitions; the pending count is what needs an administrator. */
function Counts({ server }: { server: McpServer }) {
  const approved = definitionCount(server, "approved");
  const pending = definitionCount(server, "pending");
  const rejected = definitionCount(server, "rejected");
  return (
    <div className="flex flex-wrap items-center gap-1" data-testid={`counts-${server.name}`}>
      <span className="tabular-nums">{approved}</span>
      <span className="text-xs text-muted-foreground">approved</span>
      {pending > 0 ? <Badge tone="warn">{pending} pending</Badge> : <span className="text-xs text-muted-foreground">0 pending</span>}
      {rejected > 0 && <span className="text-xs text-muted-foreground">{rejected} rejected</span>}
    </div>
  );
}
