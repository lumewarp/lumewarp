import { useState } from "react";
import { api, type Key } from "../api";
import { fmtDate, KeyBadge, PageHeader, SecretOnce } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { Dialog } from "../components/Dialog";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { blankPolicy, Budget, PolicyFields, policyBody, policyFrom, PolicySummary, POLICY_ALIAS, type PolicyValues } from "../components/Policy";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useToast } from "../components/Toast";
import { useAction, useAllKeys, useCan, useMut, useNames, useRoutesQuery, useTree } from "../hooks";

type Values = PolicyValues & { name: string; project_id: string; expires: string };
const blank: Values = { name: "", project_id: "", expires: "", ...blankPolicy() };

export function Keys() {
  const perms = useCan();
  const toast = useToast();
  const tree = useTree();
  const keys = useAllKeys(tree.data?.projects);
  const projectName = useNames(tree.data?.projects);
  const routes = useRoutesModels();
  const [editing, setEditing] = useState<Key | "new" | null>(null);
  const [revoking, setRevoking] = useState<Key | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const form = useForm<Values>(blank, { name: required("Name"), project_id: required("Project") });

  const teamOfProject = (projectId: string) => {
    const p = tree.data?.projects.find((x) => x.id === projectId);
    return p && tree.data?.teams.find((t) => t.id === p.team_id);
  };
  const mayWrite = (projectId: string) => {
    const t = teamOfProject(projectId);
    return !!t && perms.keyWrite(t);
  };
  const creatableProjects = (tree.data?.projects ?? []).filter((p) => mayWrite(p.id));

  const expiresIso = (v: Values) => (v.expires ? new Date(`${v.expires}T23:59:59`).toISOString() : null);
  const create = useMut((v: Values) => api.createKey(v.project_id, { name: v.name, expires_at: expiresIso(v), ...policyBody(v) }), (r) => setSecret(r.secret));
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateKey(id, { name: v.name, expires_at: expiresIso(v), ...policyBody(v) }), "Key saved");
  const setLifecycle = useAction(({ id, lifecycle }: { id: string; lifecycle: "active" | "suspended" }) => api.updateKey(id, { lifecycle }), "Key updated");
  const revoke = useMut((id: string) => api.revokeKey(id), () => toast("success", "Key revoked"));

  const open = (k: Key | "new") => {
    form.reset(k === "new" ? { ...blank, project_id: creatableProjects[0]?.id ?? "" } : { name: k.name, project_id: k.project_id, expires: k.expires_at?.slice(0, 10) ?? "", ...policyFrom(k) });
    setEditing(k);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Key).id, v: form.values })), POLICY_ALIAS);
  };

  return (
    <>
      <PageHeader title="API keys" description="Virtual keys used by clients against the gateway. Only a hash is stored; the secret is shown once." actions={<CreateButton can={creatableProjects.length > 0} onClick={() => open("new")}>New key</CreateButton>} />
      <DataTable
        rows={keys.data}
        loading={tree.isPending || keys.isPending}
        error={tree.error ?? keys.error}
        onRetry={() => void keys.refetch()}
        rowKey={(r) => r.id}
        empty="No API keys yet."
        columns={[
          { key: "name", header: "Name", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => (<><strong>{r.name}</strong> <code className="muted">{r.display_prefix}…</code></>) },
          { key: "project", header: "Project", sort: (a, b) => projectName(a.project_id).localeCompare(projectName(b.project_id)), cell: (r) => projectName(r.project_id) },
          { key: "status", header: "Status", sort: (a, b) => a.lifecycle.localeCompare(b.lifecycle), cell: (r) => <KeyBadge k={r} /> },
          { key: "policy", header: "Policy", cell: (r) => <PolicySummary p={r} /> },
          { key: "budget", header: "Budget", cell: (r) => <Budget p={r} /> },
          { key: "expires", header: "Expires", cell: (r) => fmtDate(r.expires_at) },
          {
            key: "actions",
            header: "",
            cell: (r) => {
              const w = mayWrite(r.project_id);
              return (
                <RowActions
                  canEdit={w && r.lifecycle !== "revoked"}
                  onEdit={() => open(r)}
                  extra={
                    r.lifecycle === "revoked" ? null : (
                      <>
                        <button className="btn btn-sm" disabled={!w} onClick={() => void setLifecycle({ id: r.id, lifecycle: r.lifecycle === "suspended" ? "active" : "suspended" })}>
                          {r.lifecycle === "suspended" ? "Resume" : "Suspend"}
                        </button>
                        <button className="btn btn-sm btn-danger-ghost" disabled={!w} onClick={() => setRevoking(r)}>
                          Revoke
                        </button>
                      </>
                    )
                  }
                />
              );
            },
          },
        ]}
      />
      <FormDialog open={!!editing} title={editing === "new" ? "New API key" : "Edit API key"} submitLabel={editing === "new" ? "Create key" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        {editing === "new" && <SelectField label="Project" value={form.values.project_id} onChange={(v) => form.set("project_id", v)} error={form.errors.project_id} options={creatableProjects.map((p) => ({ value: p.id, label: p.name }))} />}
        <TextField label="Name" value={form.values.name} onChange={(v) => form.set("name", v)} error={form.errors.name} autoFocus />
        <PolicyFields values={form.values} errors={form.errors} set={form.set} models={routes} />
        <TextField label="Expires" type="date" value={form.values.expires} onChange={(v) => form.set("expires", v)} hint="Empty means the key never expires." />
      </FormDialog>
      <Dialog open={!!secret} title="Key created" onClose={() => setSecret(null)} footer={<button className="btn btn-primary" onClick={() => setSecret(null)}>I’ve saved it</button>}>
        {secret && <SecretOnce secret={secret} />}
      </Dialog>
      <ConfirmDialog open={!!revoking} title="Revoke key" confirmLabel="Revoke" message={`Revoke “${revoking?.name}”? Gateways reject it within seconds and this cannot be undone.`} onConfirm={() => revoke.mutateAsync(revoking!.id).catch((e: Error) => toast("error", e.message))} onClose={() => setRevoking(null)} />
    </>
  );
}

/** Known model aliases, to help fill allow-lists. */
function useRoutesModels() {
  const { data } = useRoutesQuery();
  return (data ?? []).map((r) => r.model);
}
