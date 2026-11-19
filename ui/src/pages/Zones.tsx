import { useQuery } from "@tanstack/react-query";
import { getRouteApi } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api, type Schemas, type Zone } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CheckField, FormDialog, NumberField, SelectField, SwitchField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useMut } from "../hooks";
import { useZone, zonesKey, ZONE_COLORS, ZoneBadge } from "../zone";

/*
 * Manage zones: installation admins create, edit and archive config zones. Reached from the header's zone
 * menu, not from the sidebar. A zone's name is a slug and never changes.
 */

type Role = Schemas["KeyMinCreatorRole"];
type Color = Schemas["ZoneCreate"]["color"];

const ROLES: { value: Role; label: string }[] = [
  { value: "member", label: "Member" },
  { value: "team_admin", label: "Team admin" },
  { value: "org_admin", label: "Org admin" },
];
const ROLE_LABEL: Record<string, string> = Object.fromEntries(ROLES.map((r) => [r.value, r.label]));
const COLORS = (Object.keys(ZONE_COLORS) as Color[]).map((value) => ({ value, label: ZONE_COLORS[value].label }));

const SLUG = /^[a-z][a-z0-9-]{0,62}$/;

interface Values {
  name: string;
  display_name: string;
  color: Color;
  playground_gateway_url: string;
  key_min_creator_role: Role;
  key_max_lifetime_days: number | null;
  key_expiry_required: boolean;
  key_max_per_member: number | null;
  service_account_keys_allowed: boolean;
  playground_allowed: boolean;
}

const blank = (): Values => ({
  name: "",
  display_name: "",
  color: "gray",
  playground_gateway_url: "",
  key_min_creator_role: "member",
  key_max_lifetime_days: null,
  key_expiry_required: false,
  key_max_per_member: null,
  service_account_keys_allowed: true,
  playground_allowed: true,
});

const DAY = 86_400;
const from = (z: Zone): Values => ({
  name: z.name,
  display_name: z.display_name,
  color: (z.color in ZONE_COLORS ? z.color : "gray") as Color,
  playground_gateway_url: z.playground_gateway_url ?? "",
  key_min_creator_role: z.key_min_creator_role,
  key_max_lifetime_days: z.key_max_lifetime_seconds === null ? null : z.key_max_lifetime_seconds / DAY,
  key_expiry_required: z.key_expiry_required,
  key_max_per_member: z.key_max_per_member,
  service_account_keys_allowed: z.service_account_keys_allowed,
  playground_allowed: z.playground_allowed,
});

function rules(v: Values) {
  return {
    display_name: v.display_name.trim() || v.name,
    color: v.color,
    playground_gateway_url: v.playground_gateway_url.trim() || null,
    key_min_creator_role: v.key_min_creator_role,
    key_max_lifetime_seconds: v.key_max_lifetime_days === null ? null : Math.round(v.key_max_lifetime_days * DAY),
    key_expiry_required: v.key_expiry_required,
    key_max_per_member: v.key_max_per_member,
    service_account_keys_allowed: v.service_account_keys_allowed,
    playground_allowed: v.playground_allowed,
  };
}

function ZoneEditor({ zone, onClose, onSaved }: { zone: Zone | "new"; onClose: () => void; onSaved?: (name: string) => void }) {
  const isNew = zone === "new";
  const form = useForm<Values>(isNew ? blank() : from(zone), {
    name: (v) => (SLUG.test(v) ? undefined : "Use lowercase letters, digits and dashes, starting with a letter"),
  });
  const save = useMut(
    (v: Values) => (isNew ? api.createZone({ name: v.name, ...rules(v) }) : api.updateZone(zone.name, rules(v))),
    isNew ? "Zone created" : "Zone saved",
  );
  return (
    <FormDialog
      open
      title={isNew ? "New zone" : `Edit zone ${zone.name}`}
      submitLabel={isNew ? "Create" : "Save"}
      onClose={onClose}
      onSubmit={async () => {
        if (!form.validate()) return false;
        const result = await form.run(() => save.mutateAsync(form.values));
        if (result === false) return false;
        onSaved?.(form.values.name);
      }}
    >
      {isNew && (
        <TextField
          label="Name"
          value={form.values.name}
          onChange={(value) => form.set("name", value)}
          error={form.errors.name}
          hint="The zone's slug, used by gateways and the API; it cannot be changed later."
          autoFocus
        />
      )}
      <TextField label="Display name" value={form.values.display_name} onChange={(value) => form.set("display_name", value)} hint="Empty uses the name." />
      <SelectField label="Colour" value={form.values.color} onChange={(value) => form.set("color", value)} options={COLORS} strict />
      <TextField
        label="Playground gateway URL"
        value={form.values.playground_gateway_url}
        onChange={(value) => form.set("playground_gateway_url", value)}
        error={form.errors.playground_gateway_url}
        hint="Empty makes the Playground unavailable for keys of this zone."
      />
      <SelectField
        label="Lowest role that may create a key"
        value={form.values.key_min_creator_role}
        onChange={(value) => form.set("key_min_creator_role", value)}
        options={ROLES}
        strict
        hint="Installation admins may always create keys."
      />
      <NumberField
        label="Longest key lifetime (days)"
        value={form.values.key_max_lifetime_days}
        onChange={(value) => form.set("key_max_lifetime_days", value)}
        error={form.errors.key_max_lifetime_days}
        min={0}
        step="any"
        placeholder="team limit only"
        hint="Empty leaves only the team's own limit."
      />
      <NumberField
        label="Most keys per member"
        value={form.values.key_max_per_member}
        onChange={(value) => form.set("key_max_per_member", value)}
        error={form.errors.key_max_per_member}
        min={0}
        hint="Service-account keys do not count."
      />
      <CheckField label="A key needs an expiry" checked={form.values.key_expiry_required} onChange={(value) => form.set("key_expiry_required", value)} />
      <CheckField label="Allow service-account keys" checked={form.values.service_account_keys_allowed} onChange={(value) => form.set("service_account_keys_allowed", value)} />
      <CheckField label="Allow the Playground" checked={form.values.playground_allowed} onChange={(value) => form.set("playground_allowed", value)} />
    </FormDialog>
  );
}

const zonesRoute = getRouteApi("/app/zones");

export function Zones() {
  const search = zonesRoute.useSearch();
  const navigate = zonesRoute.useNavigate();
  const current = useZone();
  const [showArchived, setShowArchived] = useState(false);
  const list = useQuery({ queryKey: [...zonesKey, "manage", showArchived], queryFn: () => api.zones(showArchived) });
  const [editing, setEditing] = useState<Zone | "new" | null>(search.new ? "new" : null);
  const [archiving, setArchiving] = useState<Zone | null>(null);
  const archive = useMut((name: string) => api.archiveZone(name), "Zone archived");

  // "New zone" in the header menu arrives here with ?new=1; consume it so a reload does not reopen the dialog.
  useEffect(() => {
    if (search.new) void navigate({ search: {}, replace: true });
  }, [search.new, navigate]);

  return (
    <>
      <PageHeader
        title="Zones"
        hint="A zone is one config scope with its own gateways, upstreams and keys; names, users and roles are shared by all zones."
        actions={
          <div className="flex items-center gap-4">
            <SwitchField label="Show archived" checked={showArchived} onChange={setShowArchived} />
            <CreateButton onClick={() => setEditing("new")}>New zone</CreateButton>
          </div>
        }
      />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(z) => z.name}
        mutedRow={(z) => !!z.archived_at}
        empty="No zones."
        columns={[
          {
            key: "zone",
            header: "Zone",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (z) => (
              <span className="inline-flex flex-wrap items-center gap-2">
                <ZoneBadge zone={z} muted={!!z.archived_at} />
                <code className="text-muted-foreground">{z.name}</code>
                {z.name === current.defaultName && <Badge tone="info">default</Badge>}
                {z.archived_at && <Badge>Archived {fmtDateTime(z.archived_at)}</Badge>}
              </span>
            ),
          },
          { key: "creator", header: "Key creators", hideBelow: "md", cell: (z) => `${ROLE_LABEL[z.key_min_creator_role] ?? z.key_min_creator_role} and up` },
          {
            key: "rules",
            header: "Key rules",
            hideBelow: "lg",
            cell: (z) => (
              <span className="inline-flex flex-wrap gap-1.5">
                {z.key_max_lifetime_seconds !== null && <Badge>max {Math.round((z.key_max_lifetime_seconds / DAY) * 10) / 10} d</Badge>}
                {z.key_expiry_required && <Badge>expiry required</Badge>}
                {z.key_max_per_member !== null && <Badge>{z.key_max_per_member} per member</Badge>}
                {!z.service_account_keys_allowed && <Badge tone="warn">no service-account keys</Badge>}
                {!z.playground_allowed && <Badge tone="warn">no Playground</Badge>}
              </span>
            ),
          },
          {
            key: "actions",
            header: "",
            cell: (z) =>
              z.archived_at ? null : (
                <RowActions
                  canEdit
                  canDelete={z.name !== current.defaultName}
                  deleteReason="The default zone cannot be archived"
                  deleteLabel="Archive"
                  onEdit={() => setEditing(z)}
                  onDelete={() => setArchiving(z)}
                />
              ),
          },
        ]}
      />
      {editing && <ZoneEditor key={editing === "new" ? "new" : editing.name} zone={editing} onClose={() => setEditing(null)} onSaved={(name) => editing === "new" && current.setZone(name)} />}
      <ConfirmDialog
        open={!!archiving}
        title="Archive zone"
        confirmLabel="Archive"
        message={`Archive zone “${archiving?.display_name || archiving?.name}”? Its gateway tokens are revoked and it stops being compiled. A zone cannot be archived while it is the default zone, a gateway is connected or a live key exists.`}
        onConfirm={() => archive.mutateAsync(archiving!.name)}
        onClose={() => setArchiving(null)}
      />
    </>
  );
}
