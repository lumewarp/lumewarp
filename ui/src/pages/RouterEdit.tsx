import { useNavigate, useParams } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { api, type Model, type Plugin, type Router } from "../api";
import { PageLoading } from "./Access";
import { EditPage, Section } from "../components/EditPage";
import { CheckField, Field, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { JsonEditor, NumberInput, SchemaFields } from "../components/SchemaForm";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Checkbox } from "../components/ui/checkbox";
import { Combobox, ComboboxField } from "../components/ui/combobox-field";
import { Label } from "../components/ui/label";
import { useById, useModels, useMut, useNames, useRouters, useStrategies } from "../hooks";
import { typeOfFirst } from "../lib/modelType";
import { parseSchema, sameJson, strategyDescription, strategyTitle, unknownKeys, validateValues, type ParsedSchema, type Values as Params } from "../lib/schema";
import { splitShares } from "./Routers";

type Sticky = Router["sticky"];

interface MemberV {
  model_id: string;
  weight: number;
  params: Params;
}

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
  members: MemberV[];
}

interface Values {
  name: string;
  strategy: string;
  params: Params;
  /** One strategy call's timeout; only a WASM strategy has one (0 = the 60 s default). */
  plugin_timeout_ms: number | null;
  members: MemberV[];
  retry_enabled: boolean;
  retry_max_attempts: number | null;
  retry_statuses: string;
  retry_total_timeout_ms: number | null;
  fallback_model_ids: string[];
  splits: Split[];
  sticky: Sticky;
  sticky_header: string;
  force: boolean;
}
const blank: Values = {
  name: "",
  strategy: "weighted",
  params: {},
  plugin_timeout_ms: null,
  members: [],
  retry_enabled: false,
  retry_max_attempts: null,
  retry_statuses: "",
  retry_total_timeout_ms: null,
  fallback_model_ids: [],
  splits: [],
  sticky: "none",
  sticky_header: "",
  force: false,
};

const fromRouter = (r: Router): Values => ({
  name: r.name,
  strategy: r.strategy,
  params: r.params,
  plugin_timeout_ms: r.plugin_timeout_ms || null,
  members: r.members.map((m) => ({ ...m })),
  retry_enabled: !!r.retry,
  retry_max_attempts: r.retry?.max_attempts ?? null,
  retry_statuses: (r.retry?.retry_on_status ?? []).join(", "),
  retry_total_timeout_ms: r.retry?.total_timeout_ms || null,
  fallback_model_ids: [...r.fallback_model_ids],
  splits: r.traffic_split.map((s) => ({
    name: s.name,
    weight: s.weight,
    members: s.members.map((m) => ({ ...m })),
  })),
  sticky: r.sticky,
  sticky_header: r.sticky_header ?? "",
  force: false,
});

/** "429, 503" -> [429, 503]; undefined when an entry is not a status in 400..599. */
function parseStatuses(text: string): number[] | undefined {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  const codes = parts.map((p) => (/^\d{3}$/.test(p) ? Number(p) : NaN));
  return codes.every((c) => c >= 400 && c <= 599) ? codes : undefined;
}

/** What the form needs of a strategy to render and validate settings. */
interface Shape {
  /** The picked strategy's name: the identity settings editors are keyed on. */
  name: string;
  desc: Plugin | undefined;
  params: ParsedSchema | null;
  member: ParsedSchema | null;
  usesWeight: boolean;
  /** No usable member schema at all: members get a JSON editor. */
  memberJson: boolean;
  /** A WASM plugin (`builtin === false`): the only strategies with a timeout of their own. */
  wasm: boolean;
}

function shapeOf(name: string, desc: Plugin | undefined): Shape {
  const params = parseSchema(desc?.params_schema);
  const member = parseSchema(desc?.member_schema);
  return {
    name,
    desc,
    params,
    member,
    usesWeight: desc?.uses_weight ?? true,
    memberJson: !member,
    wasm: !!desc && !desc.builtin,
  };
}

/** An existing member keeps its weight and settings exactly: hiding a weight is not deleting it. */
const memberBody = (m: MemberV) => ({
  model_id: m.model_id,
  weight: m.weight,
  params: m.params,
});

const body = (v: Values, missing: boolean) => ({
  name: v.name.trim(),
  strategy: v.strategy,
  params: v.params,
  plugin_timeout_ms: v.plugin_timeout_ms ?? 0,
  // A split router has its members under each split.
  members: v.splits.length ? [] : v.members.map(memberBody),
  retry: v.retry_enabled
    ? {
        max_attempts: v.retry_max_attempts,
        retry_on_status: parseStatuses(v.retry_statuses) ?? [],
        total_timeout_ms: v.retry_total_timeout_ms ?? 0,
      }
    : null,
  fallback_model_ids: v.fallback_model_ids,
  traffic_split: v.splits.map((s) => ({
    name: s.name.trim(),
    weight: s.weight ?? 0,
    members: s.members.map(memberBody),
  })),
  sticky: v.splits.length ? v.sticky : ("none" as const),
  sticky_header: v.splits.length && v.sticky === "header" ? v.sticky_header.trim() : null,
  ...(v.force && missing ? { force: true } : {}),
});

const PATCHABLE = ["name", "strategy", "params", "plugin_timeout_ms", "members", "retry", "fallback_model_ids", "traffic_split", "sticky", "sticky_header"] as const;

/**
 * The PATCH of an edit: only what the form changed against the router as it was loaded. A save never
 * rewrites what the user did not edit (settings, weights, members hidden by the strategy). A changed
 * strategy resets settings, so everything that depends on it is sent.
 */
export function patchBody(original: Router, v: Values, missing: boolean): Record<string, unknown> {
  const next: Record<string, unknown> = body(v, missing);
  const before: Record<string, unknown> = body(fromRouter(original), false);
  const strategyChanged = next.strategy !== before.strategy;
  const out: Record<string, unknown> = {};
  for (const key of PATCHABLE) {
    const dependsOnStrategy = key === "params" || key === "plugin_timeout_ms" || key === "members" || key === "traffic_split";
    if ((strategyChanged && dependsOnStrategy) || !sameJson(next[key], before[key])) out[key] = next[key];
  }
  if (next.force) out.force = true;
  return out;
}

/** Leaf field names of 422 messages mapped to form fields. */
const ALIAS = {
  members: "members",
  fallback_model_ids: "fallback_model_ids",
  traffic_split: "splits",
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

/** Schema-validation errors of a set of members, keyed `<scope>:<model id>:<path>`. */
function memberErrors(scope: string, members: MemberV[], shape: Shape): Record<string, string> {
  const out: Record<string, string> = {};
  if (!shape.member) return out;
  for (const m of members) {
    for (const [path, message] of Object.entries(validateValues(shape.member.fields, m.params))) out[`${scope}:${m.model_id}:${path}`] = message;
  }
  return out;
}

interface MembersProps {
  scope: string;
  label: string;
  options: Model[];
  members: MemberV[];
  onChange: (members: MemberV[]) => void;
  shape: Shape;
  errors: Record<string, string>;
  showErrors: boolean;
  onProblem: (key: string, message: string | null) => void;
  id?: string;
  invalid?: boolean;
  describedBy?: string;
}

/** Pick models for a router (or one split) and fill in what the strategy asks of each member. */
function MemberEditor({ scope, label, options, members, onChange, shape, errors, showErrors, onProblem, id, invalid, describedBy }: MembersProps) {
  const patch = (model_id: string, change: Partial<MemberV>) => onChange(members.map((m) => (m.model_id === model_id ? { ...m, ...change } : m)));
  return (
    <div className="flex flex-col gap-1.5" id={id} aria-invalid={invalid} aria-describedby={describedBy} role="group" aria-label={label}>
      {options.map((d) => {
        const member = members.find((m) => m.model_id === d.id);
        return (
          <div key={d.id} className="flex flex-col gap-2 rounded-md bg-secondary px-2.5 py-1.5" data-testid={`member-${d.name}`}>
            <div className="flex items-center gap-2">
              <Checkbox
                id={`${scope}-${d.id}`}
                checked={!!member}
                onCheckedChange={(c) => onChange(c === true ? [...members, { model_id: d.id, weight: 1, params: {} }] : members.filter((m) => m.model_id !== d.id))}
              />
              <Label htmlFor={`${scope}-${d.id}`} className="min-w-0 flex-1 cursor-pointer font-normal [overflow-wrap:anywhere]">
                {d.name}
              </Label>
              {member && shape.usesWeight && (
                <>
                  <Label htmlFor={`${scope}-${d.id}-w`} className="sr-only">
                    Weight of {d.name}
                  </Label>
                  <span className="text-xs text-muted-foreground" aria-hidden="true">
                    Weight
                  </span>
                  <div className="w-20">
                    <NumberInput id={`${scope}-${d.id}-w`} integer min={0} value={member.weight} onChange={(n) => patch(d.id, { weight: n ?? 0 })} />
                  </div>
                </>
              )}
            </div>
            {member && shape.member && shape.member.fields.length > 0 && (
              <div className="flex flex-col gap-2 border-l-2 pl-3">
                <SchemaFields
                  fields={shape.member.fields}
                  values={member.params}
                  onChange={(next) => patch(d.id, { params: next })}
                  errors={
                    showErrors
                      ? Object.fromEntries(
                          Object.entries(errors)
                            .filter(([k]) => k.startsWith(`${scope}:${d.id}:`))
                            .map(([k, v]) => [k.slice(`${scope}:${d.id}:`.length), v]),
                        )
                      : {}
                  }
                  idPrefix={`${scope}-${d.id}`}
                />
              </div>
            )}
            {member && shape.memberJson && (
              <JsonEditor
                key={`${scope}-${d.id}|${shape.name}`}
                label={`Settings for ${d.name} (JSON)`}
                value={member.params}
                onChange={(next) => patch(d.id, { params: next })}
                onProblem={(m) => onProblem(`${scope}:${d.id}`, m)}
                hint="This strategy has no settings schema the form can show."
              />
            )}
          </div>
        );
      })}
      {options.length === 0 && <p className="text-muted-foreground">Create a model first.</p>}
    </div>
  );
}

export function RouterEdit() {
  const { routerId } = useParams({ strict: false }) as { routerId?: string };
  const list = useRouters();
  const strategies = useStrategies();
  const models = useModels();
  const existing = routerId ? list.data?.find((r) => r.id === routerId) : undefined;
  if ((routerId && list.isPending) || strategies.isPending || models.isPending) return <PageLoading />;
  const error = routerId && !existing ? (list.error?.message ?? "Router not found.") : (strategies.error?.message ?? models.error?.message);
  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }
  return <RouterForm key={existing?.id ?? "new"} existing={existing} strategies={strategies.data ?? []} models={models.data ?? []} />;
}

function RouterForm({ existing, strategies, models }: { existing?: Router; strategies: Plugin[]; models: Model[] }) {
  const navigate = useNavigate();
  const modelName = useNames(models);
  const modelById = useById(models);
  const [submitted, setSubmitted] = useState(false);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const setProblem = useCallback((key: string, message: string | null) => {
    setProblems((p) => {
      if ((p[key] ?? null) === message) return p;
      const next = { ...p };
      if (message === null) delete next[key];
      else next[key] = message;
      return next;
    });
  }, []);

  const strategyByName = useMemo(() => new Map(strategies.map((s) => [s.name, s])), [strategies]);
  const [initial] = useState<Values>(() =>
    existing
      ? fromRouter(existing)
      : {
          ...blank,
          strategy: strategyByName.has(blank.strategy) ? blank.strategy : (strategies[0]?.name ?? blank.strategy),
        },
  );
  const form = useForm<Values>(initial, {
    name: required("Name"),
    strategy: (v) => (v ? undefined : "Pick a strategy"),
    members: (v, all) => (v.length || all.splits.length ? undefined : "Pick at least one model or add a traffic split"),
    splits: (v) => splitsError(v),
    retry_statuses: (v, all) => (all.retry_enabled && parseStatuses(v) === undefined ? "Comma-separated HTTP statuses in 400–599" : undefined),
    retry_max_attempts: (v, all) => (all.retry_enabled && v !== null && (v < 1 || v > 5) ? "Between 1 and 5" : undefined),
    sticky_header: (v, all) => (all.splits.length && all.sticky === "header" && !HEADER_NAME.test(v.trim()) ? "Enter a header name" : undefined),
    force: (v, all) => ((strategyByName.get(all.strategy)?.missing_gateways.length ?? 0) > 0 && !v ? "Tick “Save anyway” to save while those gateways lack this strategy" : undefined),
  });
  const v = form.values;
  const dirty = JSON.stringify(v) !== JSON.stringify(initial);
  const shape = useMemo(() => shapeOf(v.strategy, strategyByName.get(v.strategy)), [strategyByName, v.strategy]);
  const missing = shape.desc?.missing_gateways ?? [];

  const schemaErrors = useMemo(() => {
    const out: Record<string, string> = {};
    if (shape.params) for (const [path, message] of Object.entries(validateValues(shape.params.fields, v.params))) out[`params:${path}`] = message;
    Object.assign(out, memberErrors("m", v.splits.length ? [] : v.members, shape));
    v.splits.forEach((s, i) => Object.assign(out, memberErrors(`s${i}`, s.members, shape)));
    return out;
  }, [shape, v.params, v.members, v.splits]);

  const save = useMut(
    (x: Values) => {
      const force = (shape.desc?.missing_gateways.length ?? 0) > 0;
      return existing ? api.updateRouter(existing.id, patchBody(existing, x, force) as Parameters<typeof api.updateRouter>[1]) : api.createRouter(body(x, force));
    },
    existing ? "Router saved" : "Router created",
  );

  const changeStrategy = (name: string) => {
    if (name === v.strategy) return;
    // Settings belong to one strategy's schema: they do not carry over.
    form.set("strategy", name);
    form.set("params", {});
    form.set("plugin_timeout_ms", null);
    form.set("force", false);
    form.set(
      "members",
      v.members.map((m) => ({ ...m, params: {} })),
    );
    form.set(
      "splits",
      v.splits.map((s) => ({
        ...s,
        members: s.members.map((m) => ({ ...m, params: {} })),
      })),
    );
    setProblems({});
  };

  // Every member, split member and fallback shares one type: the first one chosen sets it.
  const memberIds = [...v.members.map((m) => m.model_id), ...v.splits.flatMap((s) => s.members.map((m) => m.model_id))];
  const chosenIds = [...memberIds, ...v.fallback_model_ids];
  const routerType = typeOfFirst(chosenIds, (id) => modelById.get(id)?.model_type);
  // A lone member does not pin the type: picking a model of another type replaces it (see `retype`).
  const memberType = chosenIds.length === 1 && memberIds.length === 1 ? undefined : routerType;
  const depOptions = models.filter((m) => !memberType || m.model_type === memberType);
  const fallbackOptions = models.filter((m) => (!routerType || m.model_type === routerType) && !v.fallback_model_ids.includes(m.id));
  /** Members just picked set the type; anything chosen of another type is dropped so the router stays single-typed. */
  const retype = (prev: MemberV[], next: MemberV[]): MemberV[] => {
    const added = next.find((m) => !prev.some((p) => p.model_id === m.model_id));
    const type = added && modelById.get(added.model_id)?.model_type;
    return type ? next.filter((m) => modelById.get(m.model_id)?.model_type === type) : next;
  };
  const strategyOptions = [
    ...strategies.map((s) => ({ value: s.name, label: strategyTitle(s) })),
    ...(strategyByName.has(v.strategy) || !v.strategy
      ? []
      : [
          {
            value: v.strategy,
            label: `${v.strategy} (not reported by any gateway)`,
          },
        ]),
  ];
  const setSplit = (i: number, patch: Partial<Split>) =>
    form.set(
      "splits",
      v.splits.map((s, j) => (j === i ? { ...s, ...patch } : s)),
    );
  const moveFallback = (i: number, delta: number) => {
    const next = [...v.fallback_model_ids];
    const [item] = next.splice(i, 1);
    next.splice(i + delta, 0, item);
    form.set("fallback_model_ids", next);
  };
  const removeSplit = (i: number) => {
    // The last split going away hands its models back to the router itself.
    if (v.splits.length === 1) form.set("members", v.splits[0].members);
    form.set(
      "splits",
      v.splits.filter((_, j) => j !== i),
    );
  };
  const shares = splitShares(v.splits.map((s) => s.weight ?? 0));
  const paramErrors = (prefix: string, errors: Record<string, string>) =>
    Object.fromEntries(
      Object.entries(errors)
        .filter(([k]) => k.startsWith(prefix))
        .map(([k, m]) => [k.slice(prefix.length), m]),
    );

  return (
    <EditPage
      title={existing ? existing.name : "New router"}
      back="/routers"
      backLabel="Routers"
      dirty={dirty}
      submitLabel={existing ? "Save" : "Create"}
      onSubmit={async () => {
        setSubmitted(true);
        const ok = form.validate();
        if (!ok || Object.keys(schemaErrors).length || Object.keys(problems).length) return false;
        if ((await form.run(() => save.mutateAsync(v), ALIAS)) === false) return false;
        await navigate({ to: "/routers" });
      }}
    >
      <Section title="Router">
        <TextField label="Name" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus={!existing} hint="Must be unique across models, aliases and routers." />
        <ComboboxField
          label="Strategy"
          value={v.strategy}
          onChange={changeStrategy}
          options={strategyOptions}
          error={form.errors.strategy}
          hint={shape.desc ? strategyDescription(shape.desc) || undefined : undefined}
        />
        {shape.wasm && (
          <NumberField
            label="Timeout (ms)"
            value={v.plugin_timeout_ms}
            onChange={(x) => form.set("plugin_timeout_ms", x)}
            error={form.errors.plugin_timeout_ms}
            min={0}
            placeholder="0"
            hint="0 uses the 60 second default."
          />
        )}
        {missing.length > 0 && (
          <Alert variant="warn" data-testid="missing-gateways">
            <TriangleAlert aria-hidden="true" />
            <AlertTitle>Not available on every gateway</AlertTitle>
            <AlertDescription>
              <p>
                {missing.join(", ")} {missing.length === 1 ? "does" : "do"} not have this strategy. Saving sends them a config they reject, and they stop receiving updates, including key revocations,
                until it is fixed.
              </p>
              <CheckField label="Save anyway" checked={v.force} onChange={(x) => form.set("force", x)} />
              {form.errors.force && (
                <p className="mt-1 text-[13px] text-destructive" role="alert">
                  {form.errors.force}
                </p>
              )}
            </AlertDescription>
          </Alert>
        )}
      </Section>

      <StrategySettings
        shape={shape}
        strategy={v.strategy}
        params={v.params}
        onChange={(next) => form.set("params", next)}
        errors={submitted ? paramErrors("params:", schemaErrors) : {}}
        error={form.errors.params}
        onProblem={(m) => setProblem("params", m)}
        idPrefix={`router-${existing?.id ?? "new"}`}
      />

      <Section title="Models">
        {v.splits.length > 0 ? (
          <p className="text-[13px] text-muted-foreground" data-testid="members-in-splits">
            Traffic split is on: choose the models of each split below.
          </p>
        ) : (
          <Field label="Models" error={form.errors.members} hint={shape.usesWeight ? undefined : "This strategy does not use weights."}>
            {(p) => (
              <MemberEditor
                id={p.id}
                invalid={p["aria-invalid"]}
                describedBy={p["aria-describedby"]}
                scope="m"
                label="Models"
                options={depOptions}
                members={v.members}
                onChange={(next) => form.set("members", retype(v.members, next))}
                shape={shape}
                errors={schemaErrors}
                showErrors={submitted}
                onProblem={setProblem}
              />
            )}
          </Field>
        )}
      </Section>

      <Section
        title="Retries"
        hint="Only before any response byte reached the client; streams are not retried once the upstream answered."
        toggle={{
          label: "Retry failed attempts",
          checked: v.retry_enabled,
          onChange: (x) => form.set("retry_enabled", x),
        }}
      >
        {v.retry_enabled && (
          <>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
              <NumberField
                label="Max attempts"
                value={v.retry_max_attempts}
                onChange={(x) => form.set("retry_max_attempts", x)}
                error={form.errors.retry_max_attempts}
                min={1}
                placeholder="default (2)"
                hint="Including the first; at most 5."
              />
              <NumberField
                label="Total timeout (ms)"
                value={v.retry_total_timeout_ms}
                onChange={(x) => form.set("retry_total_timeout_ms", x)}
                error={form.errors.retry_total_timeout_ms}
                min={0}
                placeholder="none"
              />
            </div>
            <TextField
              label="Retry on status"
              value={v.retry_statuses}
              onChange={(x) => form.set("retry_statuses", x)}
              error={form.errors.retry_statuses}
              placeholder="429, 502, 503, 504"
              hint="Empty uses 429, 502, 503, 504."
            />
          </>
        )}
      </Section>

      <Section title="Fallback">
        <Field label="Fallback model (optional)" error={form.errors.fallback_model_ids} hint="Tried in order once this router's models failed, within its attempt budget (needs retries with at least 2 attempts).">
          {(p) => (
            <div className="flex flex-col gap-1.5">
              {v.fallback_model_ids.map((id, i) => (
                <div key={id} className="flex items-center gap-1.5 rounded-md bg-secondary px-2 py-1" data-testid="order-row">
                  <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
                    {i + 1}. {modelName(id)}
                  </span>
                  <Button type="button" variant="outline" size="sm" aria-label={`Move ${modelName(id)} up`} disabled={i === 0} onClick={() => moveFallback(i, -1)}>
                    ↑
                  </Button>
                  <Button type="button" variant="outline" size="sm" aria-label={`Move ${modelName(id)} down`} disabled={i === v.fallback_model_ids.length - 1} onClick={() => moveFallback(i, 1)}>
                    ↓
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    aria-label={`Remove fallback ${modelName(id)}`}
                    onClick={() =>
                      form.set(
                        "fallback_model_ids",
                        v.fallback_model_ids.filter((x) => x !== id),
                      )
                    }
                  >
                    ✕
                  </Button>
                </div>
              ))}
              <Combobox
                {...p}
                aria-label="Add fallback model"
                searchLabel="Search fallback models"
                value={undefined}
                disabled={!fallbackOptions.length}
                placeholder={fallbackOptions.length ? "None — add a fallback model…" : "No other models"}
                options={fallbackOptions.map((r) => ({
                  value: r.id,
                  label: r.name,
                }))}
                onChange={(id) => form.set("fallback_model_ids", [...v.fallback_model_ids, id])}
              />
            </div>
          )}
        </Field>
      </Section>

      <Section
        title="Traffic split"
        hint="With a split, models are chosen under each split; a retry stays in the chosen split first, then tries the others."
        actions={
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={v.splits.length >= MAX_SPLITS}
            onClick={() =>
              form.set("splits", [
                ...v.splits,
                {
                  name: v.splits.length ? `split-${v.splits.length + 1}` : "stable",
                  weight: v.splits.length ? 0 : 100,
                  members: v.splits.length ? [] : v.members,
                },
              ])
            }
          >
            Add split
          </Button>
        }
      >
        {form.errors.splits && (
          <p className="text-[13px] text-destructive" role="alert">
            {form.errors.splits}
          </p>
        )}
        {v.splits.map((s, i) => (
          <div key={i} className="flex flex-col gap-2 rounded-lg border p-2.5" role="group" aria-label={`Split ${i + 1}`}>
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-0 flex-[1_1_140px]">
                <TextField label="Split name" value={s.name} onChange={(x) => setSplit(i, { name: x })} placeholder={i === 0 ? "stable" : "canary"} />
              </div>
              <div className="min-w-0 flex-[1_1_140px]">
                <NumberField label="Weight" value={s.weight} onChange={(x) => setSplit(i, { weight: x })} min={0} placeholder="0" />
              </div>
              <span className="min-w-14 pb-2 text-right font-semibold tabular-nums" aria-label={`Share of split ${i + 1}`}>
                {shares[i]}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                aria-label={`Remove split ${i + 1}`}
                onClick={() => removeSplit(i)}
              >
                Remove
              </Button>
            </div>
            <MemberEditor
              scope={`s${i}`}
              label={`Models of split ${i + 1}`}
              options={depOptions}
              members={s.members}
              onChange={(next) => setSplit(i, { members: retype(s.members, next) })}
              shape={shape}
              errors={schemaErrors}
              showErrors={submitted}
              onProblem={setProblem}
            />
          </div>
        ))}
        {v.splits.length > 0 && (
          <>
            <SelectField
              label="Sticky"
              value={v.sticky}
              onChange={(x) => form.set("sticky", x as Sticky)}
              error={form.errors.sticky}
              options={STICKY}
              hint="Keep the same caller on the same split while the weights are unchanged."
            />
            {v.sticky === "header" && (
              <TextField
                label="Sticky header"
                value={v.sticky_header}
                onChange={(x) => form.set("sticky_header", x)}
                error={form.errors.sticky_header}
                placeholder="x-session-id"
                hint="Requests without this header are split at random."
              />
            )}
          </>
        )}
      </Section>
    </EditPage>
  );
}

/** Settings of the picked strategy: a form from its schema, or a JSON editor when there is no usable one. */
function StrategySettings({
  shape,
  strategy,
  params,
  onChange,
  errors,
  error,
  onProblem,
  idPrefix,
}: {
  shape: Shape;
  strategy: string;
  params: Params;
  onChange: (v: Params) => void;
  errors: Record<string, string>;
  error?: string;
  onProblem: (message: string | null) => void;
  idPrefix: string;
}) {
  const { desc } = shape;
  if (shape.params) {
    const extra = unknownKeys(shape.params.fields, params);
    if (!shape.params.fields.length && !extra.length)
      return error ? (
        <p className="mt-1 text-[13px] text-destructive" role="alert">
          {error}
        </p>
      ) : null;
    return (
      <div data-testid="strategy-settings">
        <Section title={shape.params.title || "Strategy settings"} hint={shape.params.description || undefined}>
          {error && (
            <p className="mt-1 text-[13px] text-destructive" role="alert">
              {error}
            </p>
          )}
          <SchemaFields fields={shape.params.fields} values={params} onChange={onChange} errors={errors} idPrefix={idPrefix} />
          {extra.length > 0 && <p className="mt-1 text-[13px] text-muted-foreground">Also set, and kept as is: {extra.join(", ")}</p>}
        </Section>
      </div>
    );
  }
  return (
    <div data-testid="strategy-settings-json">
      <Section title="Strategy settings">
        {desc ? (
          <Alert variant="warn">
            <TriangleAlert aria-hidden="true" />
            <AlertDescription>
              {desc.params_schema === null ? "No settings schema is known for this strategy." : "The settings schema is outside what the form can show."} Edit the settings as JSON.
            </AlertDescription>
          </Alert>
        ) : (
          <p className="mt-1 text-[13px] text-muted-foreground">No gateway reports this strategy, so its settings schema is unknown. Edit the settings as JSON.</p>
        )}
        {desc && desc.schema_problems.length > 0 && <p className="mt-1 text-[13px] text-muted-foreground">{desc.schema_problems.slice(0, 3).join("; ")}</p>}
        <JsonEditor
          key={`${idPrefix}|${strategy}`}
          label="Settings (JSON)"
          value={params}
          onChange={onChange}
          onProblem={onProblem}
          error={error}
          hint="Settings are stored in config snapshots and audit logs: do not put secrets here."
        />
      </Section>
    </div>
  );
}
