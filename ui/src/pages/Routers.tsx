import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Router } from "../api";
import { Badge, PageHeader } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { CheckField, Field, FormDialog, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { CreateButton, RowActions } from "../components/RowActions";
import { DataTable } from "../components/Table";
import { useCan, useMut, useNames } from "../hooks";

type Member = Router["members"][number];
type Sticky = Router["sticky"];

const STRATEGIES: { value: string; label: string }[] = [
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
  members: Member[];
}

interface Values {
  name: string;
  strategy: string;
  params: Router["params"];
  members: Member[];
  retry_enabled: boolean;
  retry_max_attempts: number | null;
  retry_statuses: string;
  retry_total_timeout_ms: number | null;
  fallback_model_ids: string[];
  splits: Split[];
  sticky: Sticky;
  sticky_header: string;
}
const blank: Values = {
  name: "",
  strategy: "weighted",
  params: {},
  members: [],
  retry_enabled: false,
  retry_max_attempts: null,
  retry_statuses: "",
  retry_total_timeout_ms: null,
  fallback_model_ids: [],
  splits: [],
  sticky: "none",
  sticky_header: "",
};

const fromRouter = (r: Router): Values => ({
  name: r.name,
  strategy: r.strategy,
  params: r.params,
  members: r.members,
  retry_enabled: !!r.retry,
  retry_max_attempts: r.retry?.max_attempts ?? null,
  retry_statuses: (r.retry?.retry_on_status ?? []).join(", "),
  retry_total_timeout_ms: r.retry?.total_timeout_ms || null,
  fallback_model_ids: [...r.fallback_model_ids],
  splits: r.traffic_split.map((s) => ({ name: s.name, weight: s.weight, members: s.members })),
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
  name: v.name,
  strategy: v.strategy,
  // Built-in strategies take no params; a custom one keeps what it has.
  params: STRATEGIES.some((s) => s.value === v.strategy) ? {} : v.params,
  // A split router has its members under each split.
  members: v.splits.length ? [] : v.members,
  retry: v.retry_enabled
    ? {
        max_attempts: v.retry_max_attempts,
        retry_on_status: parseStatuses(v.retry_statuses) ?? [],
        total_timeout_ms: v.retry_total_timeout_ms ?? 0,
      }
    : null,
  fallback_model_ids: v.fallback_model_ids,
  traffic_split: v.splits.map((s) => ({ name: s.name.trim(), weight: s.weight ?? 0, members: s.members })),
  sticky: v.splits.length ? v.sticky : ("none" as const),
  sticky_header: v.splits.length && v.sticky === "header" ? v.sticky_header.trim() : null,
});

/** Leaf field names of 422 messages mapped to form fields. */
const ALIAS = {
  members: "members",
  fallback_model_ids: "fallback_model_ids",
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
    if (!s.members.length) return `Split “${name}” needs at least one model`;
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

function ModelChecks({ id, invalid, options, members, onChange, label }: { id?: string; invalid?: boolean; options: { id: string; name: string }[]; members: Member[]; onChange: (members: Member[]) => void; label?: string }) {
  return (
    <div className="targets" id={id} aria-invalid={invalid} role="group" aria-label={label}>
      {options.map((d) => {
        const member = members.find((m) => m.model_id === d.id);
        return (
          <div key={d.id} className="target-row">
            <label>
              <input type="checkbox" checked={!!member} onChange={() => onChange(member ? members.filter((m) => m !== member) : [...members, { model_id: d.id, weight: 1, params: {} }])} /> {d.name}
            </label>
            {member && <input className="input" type="number" min={0} aria-label="Member weight" title={`Weight of ${d.name}`} value={member.weight} onChange={(e) => onChange(members.map((m) => (m === member ? { ...m, weight: Number(e.target.value) } : m)))} />}
          </div>
        );
      })}
      {options.length === 0 && <p className="muted">Create a model first.</p>}
    </div>
  );
}

export function Routers() {
  const perms = useCan();
  const list = useQuery({ queryKey: ["routers"], queryFn: () => api.routers() });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const depName = useNames(models.data);
  const [editing, setEditing] = useState<Router | "new" | null>(null);
  const [deleting, setDeleting] = useState<Router | null>(null);
  const form = useForm<Values>(blank, {
    name: required("Model alias"),
    members: (v, all) => (v.length || all.splits.length ? undefined : "Pick at least one model or add a traffic split"),
    splits: (v) => splitsError(v),
    retry_statuses: (v, all) => (all.retry_enabled && parseStatuses(v) === undefined ? "Comma-separated HTTP statuses in 400–599" : undefined),
    retry_max_attempts: (v, all) => (all.retry_enabled && v !== null && (v < 1 || v > 5) ? "Between 1 and 5" : undefined),
    sticky_header: (v, all) => (all.splits.length && all.sticky === "header" && !HEADER_NAME.test(v.trim()) ? "A header name such as x-session-id" : undefined),
  });
  const create = useMut((v: Values) => api.createRouter(body(v)), "Router created");
  const update = useMut(({ id, v }: { id: string; v: Values }) => api.updateRouter(id, body(v)), "Router saved");
  const remove = useMut((id: string) => api.deleteRouter(id), "Router deleted");

  const open = (r: Router | "new") => {
    form.reset(r === "new" ? blank : fromRouter(r));
    setEditing(r);
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => (editing === "new" ? create.mutateAsync(form.values) : update.mutateAsync({ id: (editing as Router).id, v: form.values })), ALIAS);
  };

  const v = form.values;
  const depOptions = models.data ?? [];
  const fallbackOptions = depOptions.filter((m) => !v.fallback_model_ids.includes(m.id));
  // A custom strategy has no entry of its own until c6.
  const strategyOptions = STRATEGIES.some((s) => s.value === v.strategy) ? STRATEGIES : [...STRATEGIES, { value: v.strategy, label: v.strategy }];
  const setSplit = (i: number, patch: Partial<Split>) => form.set("splits", v.splits.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const moveFallback = (i: number, delta: number) => {
    const next = [...v.fallback_model_ids];
    const [item] = next.splice(i, 1);
    next.splice(i + delta, 0, item);
    form.set("fallback_model_ids", next);
  };
  const shares = splitShares(v.splits.map((s) => s.weight ?? 0));

  return (
    <>
      <PageHeader title="Routers" description="A model alias that clients request, mapped to one or more models, with optional retries, fallbacks and traffic splits." actions={<CreateButton can={perms.isSuper} onClick={() => open("new")}>New router</CreateButton>} />
      <DataTable
        rows={list.data}
        loading={list.isPending}
        error={list.error}
        onRetry={() => void list.refetch()}
        rowKey={(r) => r.id}
        empty="No routers yet."
        columns={[
          { key: "name", header: "Model alias", sort: (a, b) => a.name.localeCompare(b.name), cell: (r) => <strong>{r.name}</strong> },
          { key: "strategy", header: "Strategy", cell: (r) => STRATEGIES.find((s) => s.value === r.strategy)?.label ?? r.strategy },
          {
            key: "deps",
            header: "Models",
            cell: (r) => {
              if (!r.traffic_split.length) return r.members.map((m) => <Badge key={m.model_id} tone="info">{depName(m.model_id)}</Badge>);
              const pct = splitShares(r.traffic_split.map((s) => s.weight));
              return r.traffic_split.map((s, i) => (
                <div key={s.name}>
                  <Badge tone="ok">
                    {s.name} {pct[i]}
                  </Badge>
                  {s.members.map((m) => (
                    <Badge key={m.model_id} tone="info">
                      {depName(m.model_id)}
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
              r.retry || r.fallback_model_ids.length || r.sticky !== "none" ? (
                <>
                  {r.retry && <Badge tone="info">retry ×{r.retry.max_attempts ?? 2}</Badge>}
                  {r.fallback_model_ids.map((id, i) => (
                    <Badge key={id}>
                      {i + 1}. → {depName(id)}
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
      <FormDialog variant="drawer" open={!!editing} title={editing === "new" ? "New router" : "Edit router"} submitLabel={editing === "new" ? "Create" : "Save"} onClose={() => setEditing(null)} onSubmit={submit}>
        <TextField label="Model alias" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus hint="The `model` value clients send." />
        <SelectField label="Strategy" value={v.strategy} onChange={(x) => form.set("strategy", x)} options={strategyOptions} hint="How a model is picked (inside the chosen split when the router is split). vLLM load reads each upstream's metrics endpoint." />
        <Field label="Models" error={form.errors.members} hint={v.splits.length ? "Ignored while a traffic split is defined." : undefined}>
          {(p) => <ModelChecks id={p.id} invalid={p["aria-invalid"]} options={depOptions} members={v.members} onChange={(next) => form.set("members", next)} />}
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

        <Field label="Fallback models" error={form.errors.fallback_model_ids} hint="Tried in order once this router's models failed, within this router's attempt budget (enable retries with at least 2 attempts). Clients still see this router's name.">
          {(p) => (
            <div className="targets" id={p.id} aria-invalid={p["aria-invalid"]}>
              {v.fallback_model_ids.map((id, i) => (
                <div key={id} className="order-row">
                  <span>
                    {i + 1}. {depName(id)}
                  </span>
                  <button type="button" className="btn btn-sm" aria-label={`Move ${depName(id)} up`} disabled={i === 0} onClick={() => moveFallback(i, -1)}>
                    ↑
                  </button>
                  <button type="button" className="btn btn-sm" aria-label={`Move ${depName(id)} down`} disabled={i === v.fallback_model_ids.length - 1} onClick={() => moveFallback(i, 1)}>
                    ↓
                  </button>
                  <button type="button" className="btn btn-sm btn-danger-ghost" aria-label={`Remove fallback ${depName(id)}`} onClick={() => form.set("fallback_model_ids", v.fallback_model_ids.filter((x) => x !== id))}>
                    ✕
                  </button>
                </div>
              ))}
              <select className="input" aria-label="Add fallback model" value="" disabled={!fallbackOptions.length} onChange={(e) => e.target.value && form.set("fallback_model_ids", [...v.fallback_model_ids, e.target.value])}>
                <option value="">{fallbackOptions.length ? "Add a fallback model…" : "No other models"}</option>
                {fallbackOptions.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </select>
            </div>
          )}
        </Field>

        <fieldset className="form-section">
          <legend>Traffic split</legend>
          <p className="field-hint">Send a share of requests to each group of models (canary). A retry stays in the chosen split first, then tries the others.</p>
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
              <ModelChecks label={`Models of split ${i + 1}`} options={depOptions} members={s.members} onChange={(next) => setSplit(i, { members: next })} />
            </div>
          ))}
          <div>
            <button type="button" className="btn btn-sm" disabled={v.splits.length >= MAX_SPLITS} onClick={() => form.set("splits", [...v.splits, { name: v.splits.length ? `split-${v.splits.length + 1}` : "stable", weight: v.splits.length ? 0 : 100, members: [] }])}>
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
      <ConfirmDialog open={!!deleting} title="Delete router" message={`Delete router “${deleting?.name}”? Clients requesting this name will get an error. A router an alias points at cannot be deleted.`} onConfirm={() => remove.mutateAsync(deleting!.id)} onClose={() => setDeleting(null)} />
    </>
  );
}
