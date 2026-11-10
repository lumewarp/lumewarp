import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { api, type Alias } from "../api";
import { Badge, PageHeader, TypeBadge } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CreateButton, RowActions } from "../components/RowActions";
import { SearchBox } from "../components/Search";
import { DataTable } from "../components/Table";
import { useAliases, useById, useCan, useModels, useMut, useRouters } from "../hooks";
import { aliasUpstreamModels, matches } from "../lib/catalog";
import { typeLabel } from "../lib/modelType";

export function Aliases() {
  const perms = useCan();
  const list = useAliases();
  const models = useModels();
  const routers = useRouters();
  const modelById = useById(models.data);
  const routerById = useById(routers.data);
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<Alias | null>(null);
  const navigate = useNavigate();
  const remove = useMut((id: string) => api.deleteAlias(id), "Alias deleted");

  const rows = useMemo(
    () => (list.data ?? []).filter((a) => matches(search, a.name, a.target_name, a.model_type, typeLabel(a.model_type), ...aliasUpstreamModels(a, modelById, routerById))),
    [list.data, search, modelById, routerById],
  );

  const edit = (a: Alias) => void navigate({ to: "/aliases/$aliasId", params: { aliasId: a.id } });

  return (
    <>
      <PageHeader
        title="Aliases"
        hint="Access is granted to the alias itself, not to its target."
        actions={
          <CreateButton can={perms.isInstallationAdmin} onClick={() => void navigate({ to: "/aliases/new" })}>
            New alias
          </CreateButton>
        }
      />
      <SearchBox value={search} onChange={setSearch} label="Search aliases" placeholder="Name, type, target or upstream model" />
      <DataTable
        rows={rows}
        loading={list.isPending || models.isPending || routers.isPending}
        error={list.error ?? models.error ?? routers.error}
        onRetry={() => {
          void list.refetch();
          void models.refetch();
          void routers.refetch();
        }}
        rowKey={(r) => r.id}
        empty={search ? "No aliases match your search." : "No aliases yet."}
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (r) => (
              <Link to="/aliases/$aliasId" params={{ aliasId: r.id }} className="font-semibold hover:underline">
                {r.name}
              </Link>
            ),
          },
          {
            key: "target",
            header: "Points to",
            cell: (r) => (
              <span className="inline-flex items-center gap-2">
                {r.target_name}
                <Badge tone={r.target_kind === "router" ? "info" : "muted"}>{r.target_kind}</Badge>
              </span>
            ),
          },
          {
            key: "type",
            header: "Type",
            cell: (r) => <TypeBadge type={r.model_type} />,
          },
          {
            key: "upstream",
            header: "Upstream model",
            cell: (r) => {
              const names = aliasUpstreamModels(r, modelById, routerById);
              return names.length ? (
                <div className="flex flex-col">
                  {names.map((n) => (
                    <code key={n}>{n}</code>
                  ))}
                </div>
              ) : (
                <span className="text-muted-foreground">—</span>
              );
            },
          },
          {
            key: "actions",
            header: "",
            cell: (r) => <RowActions canEdit={perms.isInstallationAdmin} canDelete={perms.isInstallationAdmin} onEdit={() => edit(r)} onDelete={() => setDeleting(r)} />,
          },
        ]}
      />
      <ConfirmDialog
        open={!!deleting}
        title="Delete alias"
        message={`Delete alias “${deleting?.name}”? Clients calling this name will get an error, and access granted to it is removed.`}
        onConfirm={() => remove.mutateAsync(deleting!.id)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}
