import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Schemas, type Zone } from "../api";
import { Badge, fmtDateTime, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CheckField, FormDialog, NumberField, SelectField, SwitchField, TextField, useForm } from "../components/Form";
import { RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { HelpTip } from "../components/ui/help-tip";
import { useMut } from "../hooks";
import { useZone, zonesKey, ZONE_COLORS, ZoneBadge } from "../zone";

/*
 * Manage zones: installation admins edit and archive config zones. Zones are declared on the control plane by
 * their gateway token variable, so there is no create here. Reached from the header's zone menu, not from the
 * sidebar. A zone's id is a slug and never changes; its name is what people see.
 */

type Role = Schemas["KeyMinCreatorRole"];
type Color = NonNullable<Schemas["ZoneUpdate"]["color"]>;

const ROLES: { value: Role; label: string }[] = [
  { value: "member", label: "Member" },
  { value: "team_admin", label: "Team admin" },
  { value: "org_admin", label: "Org admin" },
];
const ROLE_LABEL: Record<string, string> = Object.fromEntries(ROLES.map((r) => [r.value, r.label]));
const COLORS = (Object.keys(ZONE_COLORS) as Color[]).map((value) => ({
  value,
  label: ZONE_COLORS[value].label,
  icon: <span className={`size-3 shrink-0 rounded-full ${ZONE_COLORS[value].dot}`} aria-hidden="true" />,
}));

interface Values {
  id: string;
  name: string;
  color: Color;
  playground_gateway_url: string;
  key_min_creator_role: Role;
  key_max_lifetime_days: number | null;
  key_expiry_required: boolean;
  key_max_per_member: number | null;
  service_account_keys_allowed: boolean;
  playground_allowed: boolean;
}

const DAY = 86_400;
const from = (z: Zone): Values => ({
  id: z.id,
  name: z.name,
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
    name: v.name.trim() || v.id,
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

function ZoneEditor({ zone, onClose }: { zone: Zone; onClose: () => void }) {
  const form = useForm<Values>(from(zone));
  const save = useMut((v: Values) => api.updateZone(zone.id, rules(v)), "Zone saved");
  return (
    <FormDialog
      open
      title={`Edit zone ${zone.name || zone.id}`}
      submitLabel="Save"
      onClose={onClose}
      onSubmit={async () => {
        if (!form.validate()) return false;
        return form.run(() => save.mutateAsync(form.values));
      }}
    >
      <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} hint="Empty uses the ID." autoFocus />
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

/** A zone without a token cannot be reached by a gateway; an archived zone with one has its gateways refused. */
function TokenNote({ zone }: { zone: Zone }) {
  if (zone.archived_at) {
    return zone.token_configured ? (
      <span className="inline-flex items-center gap-1" data-testid={`zone-refused-${zone.id}`}>
        <Badge tone="warn">Gateways refused</Badge>
        <HelpTip text="The zone is archived, so its gateways are refused even though a token is still configured on the control plane." label={`Archived zone ${zone.id}`} />
      </span>
    ) : null;
  }
  if (zone.token_configured) return null;
  return (
    <span className="inline-flex items-center gap-1" data-testid={`zone-no-token-${zone.id}`}>
      <Badge tone="warn">No gateway token</Badge>
      <HelpTip text="The zone's gateways authenticate with the LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN variable set on the control plane." label={`Gateway token of ${zone.id}`} />
    </span>
  );
}

export function Zones() {
  const current = useZone();
  const [showArchived, setShowArchived] = useState(false);
  const list = useQuery({ queryKey: [...zonesKey, "manage", showArchived], queryFn: () => api.zones(showArchived) });
  const [editing, setEditing] = useState<Zone | null>(null);
  const [archiving, setArchiving] = useState<Zone | null>(null);
  const archive = useMut((name: string) => api.archiveZone(name), "Zone archived");

  return (
    <>
      <PageHeader
        title="Zones"
        hint="A zone is one config scope with its own gateways, upstreams and keys, declared on the control plane by its gateway token; names, users and roles are shared by all zones."
        actions={<SwitchField label="Show archived" checked={showArchived} onChange={setShowArchived} />}
      />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(z) => z.id}
        mutedRow={(z) => !!z.archived_at}
        empty="No zones."
        columns={[
          {
            key: "zone",
            header: "Zone",
            sort: (a, b) => (a.name || a.id).localeCompare(b.name || b.id),
            cell: (z) => (
              <span className="inline-flex flex-wrap items-center gap-2">
                <ZoneBadge zone={z} muted={!!z.archived_at} />
                <code className="text-muted-foreground">{z.id}</code>
                {z.id === current.defaultId && <Badge tone="info">default</Badge>}
                {z.archived_at && <Badge>Archived {fmtDateTime(z.archived_at)}</Badge>}
                <TokenNote zone={z} />
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
                  canDelete={z.id !== current.defaultId}
                  deleteReason="The default zone cannot be archived"
                  deleteLabel="Archive"
                  onEdit={() => setEditing(z)}
                  onDelete={() => setArchiving(z)}
                />
              ),
          },
        ]}
      />
      {editing && <ZoneEditor key={editing.id} zone={editing} onClose={() => setEditing(null)} />}
      <ConfirmDialog
        open={!!archiving}
        title="Archive zone"
        confirmLabel="Archive"
        message={`Archive zone “${archiving?.name || archiving?.id}”? It stops being compiled and its gateways are refused. A zone cannot be archived while it is the default zone, a gateway is connected or a live key exists.`}
        onConfirm={() => archive.mutateAsync(archiving!.id)}
        onClose={() => setArchiving(null)}
      />
    </>
  );
}
