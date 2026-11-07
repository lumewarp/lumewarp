import { useMemo, useState } from "react";
import { api, type Alias } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { SearchBox } from "../components/Search";
import { DataTable } from "../components/Table";
import { ComboboxField } from "../components/ui/combobox-field";
import { useAliases, useById, useCan, useModels, useMut, useRouters } from "../hooks";
import { aliasUpstreamModels, matches } from "../lib/catalog";

interface Values {
  name: string;
  target: string;
}
const blank: Values = { name: "", target: "" };

export function Aliases() {
  const perms = useCan();
  const list = useAliases();
  const models = useModels();
  const routers = useRouters();
  const modelById = useById(models.data);
  const routerById = useById(routers.data);
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<Alias | "new" | null>(null);
  const [deleting, setDeleting] = useState<Alias | null>(null);
  const form = useForm<Values>(blank, { name: required("Name"), target: required("Target") });
  const create = useMut((v: Values) => api.createAlias({ name: v.name.trim(), target: v.target }), "Alias created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateAlias(id, { name: v.name.trim(), target: v.target }), "Alias saved");
  const remove = useMut((id: string) => api.deleteAlias(id), "Alias deleted");

  const rows = useMemo(
    () => (list.data ?? []).filter((a) => matches(search, a.name, a.target_name, ...aliasUpstreamModels(a, modelById, routerById))),
    [list.data, search, modelById, routerById],
  );

  const open = (a: Alias | "new") => {
    form.reset(a === "new" ? blank : { name: a.name, target: a.target });
    setEditing(a);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Alias).id, v: form.values })));
  };
  const targets = [
    ...(models.data ?? []).map((m) => ({ value: m.id, label: `${m.name} (model)` })),
    ...(routers.data ?? []).map((r) => ({ value: r.id, label: `${r.name} (router)` })),
  ];

  return (
    <>
      <PageHeader title="Aliases" hint="Access is granted to the alias itself, not to its target." actions={<CreateButton can={perms.isInstallationAdmin} onClick={() => open("new")}>New alias</CreateButton>} />
      <SearchBox value={search} onChange={setSearch} label="Search aliases" placeholder="Name, target or upstream model" />
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
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
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
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isInstallationAdmin} canDelete={perms.isInstallationAdmin} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New alias" : "Edit alias"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus hint="Must be unique across models, aliases and routers." />
        <ComboboxField label="Points to" value={form.values.target} onChange={(v) => form.set("target", v)} error={form.errors.target} options={targets} placeholder="Select a model or router…" emptyText="No model or router matches" hint="An alias cannot point at another alias." />
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete alias" message={`Delete alias “${deleting?.name}”? Clients calling this name will get an error, and access granted to it is removed.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
