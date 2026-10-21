import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type ApiKey } from "../api";
import { BudgetBar, fmtDate, PageHeader, SecretOnce, StatusBadge } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Dialog } from "../components/Dialog";
import { FormDialog, ListField, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useToast } from "../components/Toast";
import { useCanWrite, useCrud, useList, useNames } from "../hooks";

const blank = {
  project_id: "",
  name: "",
  budget_usd: null as number | null,
  rpm: null as number | null,
  tpm: null as number | null,
  allowed_models: [] as string[],
  expires_at: null as string | null,
};

export function Keys() {
  const qc = useQueryClient();
  const toast = useToast();
  const can = useCanWrite();
  const { list, update, remove } = useCrud("keys", api.keys, "Key");
  const projects = useList("projects", () => api.projects.list());
  const projectName = useNames(projects.data);
  const [editing, setEditing] = useState<ApiKey | "new" | null>(null);
  const [deleting, setDeleting] = useState<ApiKey | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const form = useForm(blank, { name: required("Name"), project_id: required("Project") });

  const create = useMutation({
    mutationFn: () => api.keys.create(form.values),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["keys"] });
      setSecret(r.secret);
    },
  });
  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "active" | "revoked" | "suspended" }) => api.setKeyStatus(id, status),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["keys"] });
      toast("success", "Key updated");
    },
    onError: (e: Error) => toast("error", e.message),
  });

  const open = (k: ApiKey | "new") => {
    form.reset(k === "new" ? { ...blank, project_id: projects.data?.[0]?.id ?? "" } : { project_id: k.project_id, name: k.name, budget_usd: k.budget_usd, rpm: k.rpm, tpm: k.tpm, allowed_models: k.allowed_models, expires_at: k.expires_at });
    setEditing(k);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    if (editing === "new") await create.mutateAsync();
    else if (editing) await update.mutateAsync({ id: editing.id, input: form.values });
  };

  return (
    <>
      <PageHeader title="API keys" description="Virtual keys used by clients against the gateway. Only a hash is stored." actions={<CreateButton onClick={() => open("new")}>New key</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No API keys yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => (<><strong>{r.name}</strong> <code className="muted">{r.prefix}…</code></>) },
          { key: "project", header: "Project", sort: (a, b) => projectName(a.project_id).localeCompare(projectName(b.project_id)), cell: (r) => projectName(r.project_id) },
          { key: "status", header: "Status", sort: (a, b) => a.status.localeCompare(b.status), cell: (r) => <StatusBadge status={r.status} /> },
          { key: "budget", header: "Budget", cell: (r) => <BudgetBar spent={r.spent_usd} budget={r.budget_usd} /> },
          { key: "expires", header: "Expires", cell: (r) => fmtDate(r.expires_at) },
          { key: "used", header: "Last used", sort: (a, b) => (a.last_used_at ?? "").localeCompare(b.last_used_at ?? ""), cell: (r) => fmtDate(r.last_used_at) },
          {
            key: "actions",
            header: "",
            cell: (r) => (
              <RowActions
                onEdit={() => open(r)}
                onDelete={() => setDeleting(r)}
                extra={
                  r.status === "revoked" ? null : r.status === "suspended" ? (
                    <button className="btn btn-sm" disabled={!can} onClick={() => setStatus.mutate({ id: r.id, status: "active" })}>
                      Resume
                    </button>
                  ) : (
                    <>
                      <button className="btn btn-sm" disabled={!can} onClick={() => setStatus.mutate({ id: r.id, status: "suspended" })}>
                        Suspend
                      </button>
                      <button className="btn btn-sm btn-danger-ghost" disabled={!can} onClick={() => setRevoking(r)}>
                        Revoke
                      </button>
                    </>
                  )
                }
              />
            ),
          },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New API key" : "Edit API key"} submitLabel={editing === "new" ? "Create key" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <SelectField label="Project" value={form.values.project_id} onChange={(v) => form.set("project_id", v)} error={form.errors.project_id} options={(projects.data ?? []).map((p) => ({ value: p.id, label: p.name }))} />
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <NumberField label="Budget (USD)" value={form.values.budget_usd} onChange={(v) => form.set("budget_usd", v)} />
        <NumberField label="Requests per minute" value={form.values.rpm} onChange={(v) => form.set("rpm", v)} />
        <NumberField label="Tokens per minute" value={form.values.tpm} onChange={(v) => form.set("tpm", v)} />
        <ListField label="Allowed models" value={form.values.allowed_models} onChange={(v) => form.set("allowed_models", v)} hint="Comma-separated aliases; empty inherits from the project." />
        <TextField label="Expires" type="date" value={form.values.expires_at?.slice(0, 10) ?? ""} onChange={(v) => form.set("expires_at", v ? new Date(v).toISOString() : null)} />
      </FormDialog>
      <Dialog open={!!secret} title="Key created" onClose={() => setSecret(null)} footer={<button className="btn btn-primary" onClick={() => setSecret(null)}>I’ve saved it</button>}>
        {secret && <SecretOnce secret={secret} />}
      </Dialog>
      <ConfirmDialog open={!!revoking} title="Revoke key" confirmLabel="Revoke" message={`Revoke “${revoking?.name}”? Gateways reject it within seconds and this cannot be undone.`} onConfirm={() => setStatus.mutateAsync({ id: revoking!.id, status: "revoked" })} onClose={() => setRevoking(null)} />
      <ConfirmDialog open={!!deleting} title="Delete key" message={`Delete key “${deleting?.name}”?`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
