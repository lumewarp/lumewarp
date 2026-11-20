import { useNavigate, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { api, type Alias } from "../api";
import { PageLoading } from "./Access";
import { CatalogFilter } from "../components/CatalogPicker";
import { EditPage, Section } from "../components/EditPage";
import { required, TextField, useForm } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { useAliases, useMut } from "../hooks";
import { useZone } from "../zone";
import { NotAvailableWarning, unavailableHere } from "./Routers";

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
  const { id: zone, zone: zoneRow } = useZone();
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
      {existing && unavailableHere(existing, zone) && <NotAvailableWarning what="alias" zoneLabel={zoneRow?.name || zone} />}
      <Section title="Alias">
        <TextField label="Name" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus={!existing} hint="Must be unique across models, aliases and routers." />
        <CatalogFilter
          label="Points to"
          entryValue={(e) => e.id}
          kinds={["model", "router"]}
          value={v.target}
          onChange={(x) => form.set("target", x)}
          error={form.errors.target}
          placeholder="Select a model or router…"
          emptyText="No model or router matches"
          hint="An alias cannot point at another alias."
        />
      </Section>
    </EditPage>
  );
}
