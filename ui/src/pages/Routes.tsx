import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Route } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CheckField, Field, FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames } from "../hooks";

type Strategy = Route["strategy"];
type Sticky = Route["sticky"];

const STRATEGIES: { value: Strategy; label: string }[] = [
  { value: "weighted", label: "Weighted" },
  { value: "least_in_flight", label: "Fewest in-flight requests" },
  { value: "lowest_latency", label: "Lowest latency (EWMA)" },
  { value: "vllm_load", label: "vLLM load (queue / KV cache)" },
];
const STICKY: { value: Sticky; label: string }[] = [
  { value: "none", label: "Not sticky (random by weight)" },
  { value: "header", label: "By request header" },
  { value: "team", label: "By team" },
  { value: "key", label: "By API key" },
];
// Gateway rules (snapshot.rs::validate_split).
const SPLIT_NAME = /^[A-Za-z0-9_.-]{1,32}$/;
const MAX_SPLITS = 16;
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;

interface Split {
  name: string;
  weight: number | null;
  deployment_ids: string[];
}

interface Values {
  model: string;
  strategy: Strategy;
  deployment_ids: string[];
  retry_enabled: boolean;
  retry_max_attempts: number | null;
  retry_statuses: string;
  retry_total_timeout_ms: number | null;
  fallback_route_ids: string[];
  splits: Split[];
  sticky: Sticky;
  sticky_header: string;
}
const blank: Values = {
  model: "",
  strategy: "weighted",
  deployment_ids: [],
  retry_enabled: false,
  retry_max_attempts: null,
  retry_statuses: "",
  retry_total_timeout_ms: null,
  fallback_route_ids: [],
  splits: [],
  sticky: "none",
  sticky_header: "",
};

const ordered = (r: Route) => [...r.deployments].sort((a, b) => a.position - b.position).map((d) => d.deployment_id);

const fromRoute = (r: Route): Values => ({
  model: r.model,
  strategy: r.strategy,
  deployment_ids: ordered(r),
  retry_enabled: !!r.retry,
  retry_max_attempts: r.retry?.max_attempts ?? null,
  retry_statuses: (r.retry?.retry_on_status ?? []).join(", "),
  retry_total_timeout_ms: r.retry?.total_timeout_ms || null,
  fallback_route_ids: [...r.fallback_route_ids],
  splits: r.traffic_split.map((s) => ({ name: s.name, weight: s.weight, deployment_ids: [...s.deployment_ids] })),
  sticky: r.sticky,
  sticky_header: r.sticky_header ?? "",
});

/** "429, 503" -> [429, 503]; undefined when an entry is not a status in 400..599. */
function parseStatuses(text: string): number[] | undefined {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  const codes = parts.map((p) => (/^\d{3}$/.test(p) ? Number(p) : NaN));
  return codes.every((c) => c >= 400 && c <= 599) ? codes : undefined;
}

const body = (v: Values) => ({
  model: v.model,
  strategy: v.strategy,
  deployment_ids: v.deployment_ids,
  retry: v.retry_enabled
    ? {
        max_attempts: v.retry_max_attempts,
        retry_on_status: parseStatuses(v.retry_statuses) ?? [],
        total_timeout_ms: v.retry_total_timeout_ms ?? 0,
      }
    : null,
  fallback_route_ids: v.fallback_route_ids,
  traffic_split: v.splits.map((s) => ({ name: s.name.trim(), weight: s.weight ?? 0, deployment_ids: s.deployment_ids })),
  sticky: v.splits.length ? v.sticky : ("none" as const),
  sticky_header: v.splits.length && v.sticky === "header" ? v.sticky_header.trim() : null,
});

/** Leaf field names of 422 messages mapped to form fields. */
const ALIAS = {
  deployment_ids: "deployment_ids",
  fallback_route_ids: "fallback_route_ids",
  traffic_split: "splits",
  name: "splits",
  weight: "splits",
  sticky: "sticky",
  sticky_header: "sticky_header",
  retry: "retry_max_attempts",
  max_attempts: "retry_max_attempts",
  retry_on_status: "retry_statuses",
  total_timeout_ms: "retry_total_timeout_ms",
} as const;

function splitsError(splits: Split[]): string | undefined {
  if (splits.length > MAX_SPLITS) return `At most ${MAX_SPLITS} splits`;
  const names = new Set<string>();
  for (const s of splits) {
    const name = s.name.trim();
    if (!SPLIT_NAME.test(name)) return "Split names are 1–32 characters of letters, digits, “_”, “.” or “-”";
    if (names.has(name)) return `Split “${name}” is listed twice`;
    names.add(name);
    if ((s.weight ?? 0) < 0) return "Weights cannot be negative";
    if (!s.deployment_ids.length) return `Split “${name}” needs at least one deployment`;
  }
  if (splits.length && !splits.some((s) => (s.weight ?? 0) > 0)) return "At least one split needs a weight above 0";
  return undefined;
}

/** Share of each split as a percentage string ("90%", "33.3%"). */
function splitShares(weights: number[]): string[] {
  const total = weights.reduce((a, w) => a + Math.max(0, w), 0);
  return weights.map((w) => {
    if (!total) return "—";
    const pct = (Math.max(0, w) / total) * 100;
    return `${Number.isInteger(pct) ? pct : pct.toFixed(1)}%`;
  });
}

function DeploymentChecks({ id, invalid, options, selected, onToggle, label }: { id?: string; invalid?: boolean; options: { id: string; name: string; weight: number }[]; selected: string[]; onToggle: (id: string) => void; label?: string }) {
  return (
    <div className="targets" id={id} aria-invalid={invalid} role="group" aria-label={label}>
      {options.map((d) => (
        <label key={d.id} className="target-row">
          <input type="checkbox" checked={selected.includes(d.id)} onChange={() => onToggle(d.id)} />
          <span>
            {d.name} <span className="muted">· weight {d.weight}</span>
          </span>
        </label>
      ))}
      {options.length === 0 && <p className="muted">Create a deployment first.</p>}
    </div>
  );
}

const toggled = (ids: string[], id: string) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]);

export function Routes() {
  const perms = useCan();
  const list = useQuery({ queryKey: ["routes"], queryFn: () => api.routes() });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: () => api.deployments() });
  const depName = useNames(deployments.data);
  const routeName = useNames(list.data?.map((r) => ({ id: r.id, name: r.model })));
  const [editing, setEditing] = useState<Route | "new" | null>(null);
  const [deleting, setDeleting] = useState<Route | null>(null);
  const form = useForm<Values>(blank, {
    model: required("Model alias"),
    deployment_ids: (v, all) => (v.length || all.splits.length ? undefined : "Pick at least one deployment or add a traffic split"),
    splits: (v) => splitsError(v),
    retry_statuses: (v, all) => (all.retry_enabled && parseStatuses(v) === undefined ? "Comma-separated HTTP statuses in 400–599" : undefined),
    retry_max_attempts: (v, all) => (all.retry_enabled && v !== null && (v < 1 || v > 5) ? "Between 1 and 5" : undefined),
    sticky_header: (v, all) => (all.splits.length && all.sticky === "header" && !HEADER_NAME.test(v.trim()) ? "A header name such as x-session-id" : undefined),
  });
  const create = useMut((v: Values) => api.createRoute(body(v)), "Route created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateRoute(id, body(v)), "Route saved");
  const remove = useMut((id: string) => api.deleteRoute(id), "Route deleted");

  const open = (r: Route | "new") => {
    form.reset(r === "new" ? blank : fromRoute(r));
    setEditing(r);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Route).id, v: form.values })), ALIAS);
  };

  const v = form.values;
  const depOptions = deployments.data ?? [];
  const editingId = editing && editing !== "new" ? editing.id : null;
  const fallbackOptions = (list.data ?? []).filter((r) => r.id !== editingId && !v.fallback_route_ids.includes(r.id));
  const setSplit = (i: number, patch: Partial<Split>) => form.set("splits", v.splits.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const moveFallback = (i: number, delta: number) => {
    const next = [...v.fallback_route_ids];
    const [item] = next.splice(i, 1);
    next.splice(i + delta, 0, item);
    form.set("fallback_route_ids", next);
  };
  const shares = splitShares(v.splits.map((s) => s.weight ?? 0));

  return (
    <>
      <PageHeader title="Models / Routes" description="A model alias that clients request, mapped to one or more deployments, with optional retries, fallbacks and traffic splits." actions={<CreateButton can={perms.isSuper} onClick={() => open("new")}>New route</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No routes yet."
        columns={[
          { key: "model", header: "Model alias", sort: (a, b) => a.model.localeCompare(b.model), cell: (r) => <strong>{r.model}</strong> },
          { key: "strategy", header: "Strategy", cell: (r) => STRATEGIES.find((s) => s.value === r.strategy)?.label ?? r.strategy },
          {
            key: "deps",
            header: "Deployments",
            cell: (r) => {
              if (!r.traffic_split.length) return ordered(r).map((id) => <Badge key={id} tone="info">{depName(id)}</Badge>);
              const pct = splitShares(r.traffic_split.map((s) => s.weight));
              return r.traffic_split.map((s, i) => (
                <div key={s.name}>
                  <Badge tone="ok">
                    {s.name} {pct[i]}
                  </Badge>
                  {s.deployment_ids.map((id) => (
                    <Badge key={id} tone="info">
                      {depName(id)}
                    </Badge>
                  ))}
                </div>
              ));
            },
          },
          {
            key: "resilience",
            header: "Resilience",
            cell: (r) =>
              r.retry || r.fallback_route_ids.length || r.sticky !== "none" ? (
                <>
                  {r.retry && <Badge tone="info">retry ×{r.retry.max_attempts ?? 2}</Badge>}
                  {r.fallback_route_ids.map((id, i) => (
                    <Badge key={id}>
                      {i + 1}. → {routeName(id)}
                    </Badge>
                  ))}
                  {r.sticky !== "none" && <Badge>sticky: {r.sticky === "header" ? r.sticky_header : r.sticky}</Badge>}
                </>
              ) : (
                <span className="muted">—</span>
              ),
          },
          { key: "actions", header: "", cell: (r) => <RowActions canEdit={perms.isSuper} canDelete={perms.isSuper} onEdit={() => open(r)} onDelete={() => setDeleting(r)} /> },
        ]}
      />
      <FormDialog variant="drawer" open={!!editing} title={editing === "new" ? "New route" : "Edit route"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Model alias" value={v.model} onChange={(x) => form.set("model", x)} error={form.errors.model} autoFocus hint="The `model` value clients send." />
        <SelectField label="Strategy" value={v.strategy} onChange={(x) => form.set("strategy", x as Strategy)} options={STRATEGIES} hint="How a deployment is picked (inside the chosen split when the route is split). vLLM load reads each upstream's metrics endpoint." />
        <Field label="Deployments" error={form.errors.deployment_ids} hint={v.splits.length ? "Ignored while a traffic split is defined." : undefined}>
          {(p) => <DeploymentChecks id={p.id} invalid={p["aria-invalid"]} options={depOptions} selected={v.deployment_ids} onToggle={(id) => form.set("deployment_ids", toggled(v.deployment_ids, id))} />}
        </Field>

        <fieldset className="form-section">
          <legend>Retries</legend>
          <CheckField label="Retry failed attempts" checked={v.retry_enabled} onChange={(x) => form.set("retry_enabled", x)} hint="Only before any response byte reached the client: connect errors, timeouts and the statuses below. Streams are not retried once the upstream answered." />
          {v.retry_enabled && (
            <>
              <div className="field-grid">
                <NumberField label="Max attempts" value={v.retry_max_attempts} onChange={(x) => form.set("retry_max_attempts", x)} error={form.errors.retry_max_attempts} min={1} placeholder="default (2)" hint="Including the first; at most 5." />
                <NumberField label="Total timeout (ms)" value={v.retry_total_timeout_ms} onChange={(x) => form.set("retry_total_timeout_ms", x)} error={form.errors.retry_total_timeout_ms} min={0} placeholder="none" />
              </div>
              <TextField label="Retry on status" value={v.retry_statuses} onChange={(x) => form.set("retry_statuses", x)} error={form.errors.retry_statuses} placeholder="429, 502, 503, 504" hint="Empty uses 429, 502, 503, 504." />
            </>
          )}
        </fieldset>

        <Field label="Fallback routes" error={form.errors.fallback_route_ids} hint="Tried in order once this route's deployments failed, within this route's attempt budget (enable retries with at least 2 attempts). Clients still see this route's model name.">
          {(p) => (
            <div className="targets" id={p.id} aria-invalid={p["aria-invalid"]}>
              {v.fallback_route_ids.map((id, i) => (
                <div key={id} className="order-row">
                  <span>
                    {i + 1}. {routeName(id)}
                  </span>
                  <button type="button" className="btn btn-sm" aria-label={`Move ${routeName(id)} up`} disabled={i === 0} onClick={() => moveFallback(i, -1)}>
                    ↑
                  </button>
                  <button type="button" className="btn btn-sm" aria-label={`Move ${routeName(id)} down`} disabled={i === v.fallback_route_ids.length - 1} onClick={() => moveFallback(i, 1)}>
                    ↓
                  </button>
                  <button type="button" className="btn btn-sm btn-danger-ghost" aria-label={`Remove fallback ${routeName(id)}`} onClick={() => form.set("fallback_route_ids", v.fallback_route_ids.filter((x) => x !== id))}>
                    ✕
                  </button>
                </div>
              ))}
              <select className="input" aria-label="Add fallback route" value="" disabled={!fallbackOptions.length} onChange={(e) => e.target.value && form.set("fallback_route_ids", [...v.fallback_route_ids, e.target.value])}>
                <option value="">{fallbackOptions.length ? "Add a fallback route…" : "No other routes"}</option>
                {fallbackOptions.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.model}
                  </option>
                ))}
              </select>
            </div>
          )}
        </Field>

        <fieldset className="form-section">
          <legend>Traffic split</legend>
          <p className="field-hint">Send a share of requests to each group of deployments (canary). A retry stays in the chosen split first, then tries the others.</p>
          {form.errors.splits && (
            <p className="field-error" role="alert">
              {form.errors.splits}
            </p>
          )}
          {v.splits.map((s, i) => (
            <div key={i} className="split-card" role="group" aria-label={`Split ${i + 1}`}>
              <div className="split-head">
                <TextField label="Split name" value={s.name} onChange={(x) => setSplit(i, { name: x })} placeholder={i === 0 ? "stable" : "canary"} />
                <NumberField label="Weight" value={s.weight} onChange={(x) => setSplit(i, { weight: x })} min={0} placeholder="0" />
                <span className="split-share" aria-label={`Share of split ${i + 1}`}>
                  {shares[i]}
                </span>
                <button type="button" className="btn btn-sm btn-danger-ghost" aria-label={`Remove split ${i + 1}`} onClick={() => form.set("splits", v.splits.filter((_, j) => j !== i))}>
                  Remove
                </button>
              </div>
              <DeploymentChecks label={`Deployments of split ${i + 1}`} options={depOptions} selected={s.deployment_ids} onToggle={(id) => setSplit(i, { deployment_ids: toggled(s.deployment_ids, id) })} />
            </div>
          ))}
          <div>
            <button type="button" className="btn btn-sm" disabled={v.splits.length >= MAX_SPLITS} onClick={() => form.set("splits", [...v.splits, { name: v.splits.length ? `split-${v.splits.length + 1}` : "stable", weight: v.splits.length ? 0 : 100, deployment_ids: [] }])}>
              Add split
            </button>
          </div>
          {v.splits.length > 0 && (
            <>
              <SelectField label="Sticky" value={v.sticky} onChange={(x) => form.set("sticky", x as Sticky)} error={form.errors.sticky} options={STICKY} hint="Keep the same caller on the same split while the weights are unchanged." />
              {v.sticky === "header" && <TextField label="Sticky header" value={v.sticky_header} onChange={(x) => form.set("sticky_header", x)} error={form.errors.sticky_header} placeholder="x-session-id" hint="Requests without this header are split at random." />}
            </>
          )}
        </fieldset>
      </FormDialog>
      <ConfirmDialog open={!!deleting} title="Delete route" message={`Delete route “${deleting?.model}”? Clients requesting this model will get an error. A route used as another route's fallback cannot be deleted.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
