import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { api, type ModelGroup } from "../api";
import { PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CreateButton, RowActions } from "../components/RowActions";
import { SearchBox } from "../components/Search";
import { DataTable } from "../components/Table";
import { useCan, useModelGroups, useMut } from "../hooks";
import { matches } from "../lib/catalog";

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function ModelGroups() {
  const perms = useCan();
  const list = useModelGroups();
  const [search, setSearch] = useState("");
  const [deleting, setDeleting] = useState<ModelGroup | null>(null);
  const navigate = useNavigate();
  const remove = useMut((id: string) => api.deleteModelGroup(id), "Model group deleted");
  const rows = useMemo(() => (list.data ?? []).filter((g) => matches(search, g.name, g.description, ...g.members.map((m) => m.name))), [list.data, search]);
  const edit = (g: ModelGroup) => void navigate({ to: "/model-groups/$groupId", params: { groupId: g.id } });

  return (
    <>
      <PageHeader
        title="Model groups"
        hint="A group is granted like a name and stands for its members at the time, so changing the members changes every grant."
        actions={
          <CreateButton can={perms.isInstallationAdmin} onClick={() => void navigate({ to: "/model-groups/new" })}>
            New group
          </CreateButton>
        }
      />
      <SearchBox value={search} onChange={setSearch} label="Search model groups" placeholder="Name, description or member" />
      <DataTable
        rows={rows}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(g) => g.id}
        empty={search ? "No model groups match your search." : "No model groups yet."}
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (g) => (
              <Link to="/model-groups/$groupId" params={{ groupId: g.id }} className="font-semibold hover:underline">
                {g.name}
              </Link>
            ),
          },
          { key: "description", header: "Description", cell: (g) => g.description || <span className="text-muted-foreground">—</span> },
          { key: "members", header: "Members", sort: (a, b) => a.members.length - b.members.length, cell: (g) => g.members.length },
          { key: "grants", header: "Grants", sort: (a, b) => a.grants - b.grants, cell: (g) => g.grants },
          {
            key: "actions",
            header: "",
            cell: (g) => <RowActions canEdit={perms.isInstallationAdmin} canDelete={perms.isInstallationAdmin} onEdit={() => edit(g)} onDelete={() => setDeleting(g)} />,
          },
        ]}
      />
      <ConfirmDialog
        open={!!deleting}
        title="Delete model group"
        message={`Delete group “${deleting?.name}”? ${deleting?.grants ? `${plural(deleting.grants, "grant")} of it will be removed, and the holders lose its members.` : "Nothing is granted through it."}`}
        onConfirm={() => remove.mutateAsync(deleting!.id)}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}
