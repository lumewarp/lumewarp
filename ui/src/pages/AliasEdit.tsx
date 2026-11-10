import { useNavigate, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { api, type Alias } from "../api";
import { PageLoading } from "./Access";
import { EditPage, Section } from "../components/EditPage";
import { required, TextField, useForm } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { ComboboxField } from "../components/ui/combobox-field";
import { useAliases, useModels, useMut, useRouters } from "../hooks";
import { typeLabel } from "../lib/modelType";

interface Values {
  name: string;
  target: string;
}
const blank: Values = { name: "", target: "" };

export function AliasEdit() {
  const { aliasId } = useParams({ strict: false }) as { aliasId?: string };
  const list = useAliases();
  const existing = aliasId ? list.data?.find((a) => a.id === aliasId) : undefined;
  if (aliasId && list.isPending) return <PageLoading />;
  if (aliasId && !existing) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{list.error?.message ?? "Alias not found."}</AlertDescription>
      </Alert>
    );
  }
  return <AliasForm key={existing?.id ?? "new"} existing={existing} />;
}

function AliasForm({ existing }: { existing?: Alias }) {
  const navigate = useNavigate();
  const models = useModels();
  const routers = useRouters();
  const [initial] = useState<Values>(() => (existing ? { name: existing.name, target: existing.target } : blank));
  const form = useForm<Values>(initial, {
    name: required("Name"),
    target: required("Target"),
  });
  const v = form.values;
  const dirty = JSON.stringify(v) !== JSON.stringify(initial);
  const save = useMut(
    (x: Values) => {
      const body = { name: x.name.trim(), target: x.target };
      return existing ? api.updateAlias(existing.id, body) : api.createAlias(body);
    },
    existing ? "Alias saved" : "Alias created",
  );
  const targets = [
    ...(models.data ?? []).map((m) => ({
      value: m.id,
      label: `${m.name} (model · ${typeLabel(m.model_type)})`,
    })),
    ...(routers.data ?? []).map((r) => ({
      value: r.id,
      label: `${r.name} (router${r.model_type ? ` · ${typeLabel(r.model_type)}` : ""})`,
    })),
  ];
  const targetNote =
    models.error || routers.error ? `Could not load every target: ${(models.error ?? routers.error)!.message}` : models.isPending || routers.isPending ? "Loading models and routers…" : undefined;

  return (
    <EditPage
      title={existing ? existing.name : "New alias"}
      hint="Access is granted to the alias itself, not to its target."
      back="/aliases"
      backLabel="Aliases"
      dirty={dirty}
      submitLabel={existing ? "Save" : "Create"}
      onSubmit={async () => {
        if (!form.validate()) return false;
        if ((await form.run(() => save.mutateAsync(v))) === false) return false;
        await navigate({ to: "/aliases" });
      }}
    >
      <Section title="Alias">
        <TextField label="Name" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus={!existing} hint="Must be unique across models, aliases and routers." />
        <ComboboxField
          label="Points to"
          value={v.target}
          onChange={(x) => form.set("target", x)}
          error={form.errors.target}
          options={targets}
          placeholder="Select a model or router…"
          emptyText="No model or router matches"
          hint="An alias cannot point at another alias."
          note={targetNote}
        />
      </Section>
    </EditPage>
  );
}
