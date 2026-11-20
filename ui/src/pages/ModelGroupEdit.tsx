import { useNavigate, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { api, type ModelGroup } from "../api";
import { PageLoading } from "./Access";
import { CatalogMultiSelect } from "../components/CatalogPicker";
import { EditPage, Section } from "../components/EditPage";
import { TextField, useForm } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { useCatalog, useModelGroups, useMut } from "../hooks";

interface Values {
  name: string;
  description: string;
  members: string[];
}
const blank: Values = { name: "", description: "", members: [] };

export function ModelGroupEdit() {
  const { groupId } = useParams({ strict: false }) as { groupId?: string };
  const list = useModelGroups();
  const existing = groupId ? list.data?.find((g) => g.id === groupId) : undefined;
  if (groupId && list.isPending) return <PageLoading />;
  if (groupId && !existing) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{list.error?.message ?? "Model group not found."}</AlertDescription>
      </Alert>
    );
  }
  return <GroupForm key={existing?.id ?? "new"} existing={existing} />;
}

function GroupForm({ existing }: { existing?: ModelGroup }) {
  const navigate = useNavigate();
  const catalog = useCatalog();
  const [initial] = useState<Values>(() => (existing ? { name: existing.name, description: existing.description, members: existing.members.map((m) => m.name) } : blank));
  const taken = new Set((catalog.data ?? []).map((entry) => entry.name));
  const form = useForm<Values>(initial, {
    name: (value) => (!value.trim() ? "Name is required" : taken.has(value.trim()) ? "A model, alias or router already has this name" : undefined),
  });
  const v = form.values;
  const dirty = JSON.stringify(v) !== JSON.stringify(initial);
  const save = useMut(
    (x: Values) => {
      const body = { name: x.name.trim(), description: x.description.trim(), members: x.members };
      return existing ? api.updateModelGroup(existing.id, body) : api.createModelGroup(body);
    },
    existing ? "Model group saved" : "Model group created",
  );
  return (
    <EditPage
      title={existing ? existing.name : "New model group"}
      hint="A group is granted like a name; it is never callable itself."
      back="/model-groups"
      backLabel="Model groups"
      dirty={dirty}
      submitLabel={existing ? "Save" : "Create"}
      onSubmit={async () => {
        if (!form.validate()) return false;
        if ((await form.run(() => save.mutateAsync(v))) === false) return false;
        await navigate({ to: "/model-groups" });
      }}
    >
      <Section title="Group">
        <TextField label="Name" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus={!existing} hint="Unique among groups and different from every model, alias and router name." />
        <TextField label="Description" value={v.description} onChange={(x) => form.set("description", x)} />
      </Section>
      <Section title="Members" hint="Models, aliases and routers; a group cannot contain another group.">
        <CatalogMultiSelect label="Group members" idPrefix="group-member" selected={v.members} onChange={(members) => form.set("members", members)} emptyText="No models, aliases or routers exist yet." />
      </Section>
    </EditPage>
  );
}
