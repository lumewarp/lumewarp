import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";
import { PlusIcon, Trash2Icon } from "lucide-react";
import { useState } from "react";

import { api, type Model, type ModelTest } from "../api";
import { Badge, nanoToUsd, usdToNano } from "../components/Bits";
import { EditPage, Section } from "../components/EditPage";
import { CheckField, Field, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { ComboboxField } from "../components/ui/combobox-field";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { Input } from "../components/ui/input";
import { Textarea } from "../components/ui/textarea";
import { useAliases, useModels, useMut, useNames, useRouters, useUpstreams } from "../hooks";
import { usedBy } from "../lib/catalog";
import { PageLoading } from "./Access";
import { ProbeRow } from "./UpstreamEdit";
import { HealthBadge } from "./Upstreams";

type Mode = Model["reasoning"]["mode"];
/** Keys the gateway owns; the API refuses them in a default body or a reasoning fragment. */
const RESERVED = ["model", "stream", "stream_options"];
/** Level names the API accepts. */
const LEVEL_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$/;

const MODES: { value: Mode; label: string }[] = [
  { value: "mapped", label: "Map levels to request fields" },
  { value: "strip", label: "Remove reasoning_effort" },
  { value: "passthrough", label: "Pass through with a default level" },
];

/** One mapped level; `key` only identifies the row in the list. */
interface LevelRow {
  key: number;
  name: string;
  json: string;
}
let rowSeq = 1000;
const newRow = (name = "", json = ""): LevelRow => ({ key: rowSeq++, name, json });

const thinking = (key: string, on: boolean) => JSON.stringify({ chat_template_kwargs: { [key]: on } });
const rowsOf = (entries: [string, string][]): LevelRow[] => entries.map(([name, json], i) => ({ key: i, name, json }));
const PRESETS: { label: string; mode: Mode; rows?: [string, string][]; level?: string }[] = [
  { label: "Qwen3 (enable_thinking)", mode: "mapped", rows: [["none", thinking("enable_thinking", false)], ["low", thinking("enable_thinking", true)], ["medium", thinking("enable_thinking", true)], ["high", thinking("enable_thinking", true)]] },
  { label: "DeepSeek (thinking)", mode: "mapped", rows: [["none", thinking("thinking", false)], ["low", thinking("thinking", true)], ["medium", thinking("thinking", true)], ["high", thinking("thinking", true)]] },
  { label: "OpenAI standard levels", mode: "passthrough", level: "medium" },
  { label: "Remove (no reasoning control)", mode: "strip" },
];

interface Values {
  name: string;
  keep_old_name_as_alias: boolean;
  upstream_id: string;
  upstream_model: string;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  price_in: number | null;
  price_out: number | null;
  default_body: string;
  reasoning_on: boolean;
  reasoning_mode: Mode;
  levels: LevelRow[];
  reasoning_default: string;
  check_enabled: boolean;
  check_interval_ms: number | null;
  check_timeout_ms: number | null;
  check_unhealthy: number | null;
  check_healthy: number | null;
}

const blank: Values = {
  name: "",
  keep_old_name_as_alias: true,
  upstream_id: "",
  upstream_model: "",
  max_input_tokens: null,
  max_output_tokens: null,
  price_in: null,
  price_out: null,
  default_body: "",
  reasoning_on: false,
  reasoning_mode: "mapped",
  levels: [],
  reasoning_default: "",
  check_enabled: false,
  check_interval_ms: 60000,
  check_timeout_ms: 10000,
  check_unhealthy: null,
  check_healthy: null,
};

const fromModel = (m: Model): Values => ({
  name: m.name,
  keep_old_name_as_alias: true,
  upstream_id: m.upstream_id,
  upstream_model: m.upstream_model,
  max_input_tokens: m.max_input_tokens,
  max_output_tokens: m.max_output_tokens,
  price_in: nanoToUsd(m.price_input_nano_usd_per_mtok),
  price_out: nanoToUsd(m.price_output_nano_usd_per_mtok),
  default_body: m.default_body ? JSON.stringify(m.default_body, null, 2) : "",
  // Pass through without a default level changes nothing, which is the same as no handling.
  reasoning_on: !(m.reasoning.mode === "passthrough" && !m.reasoning.default_level),
  reasoning_mode: m.reasoning.mode === "passthrough" && !m.reasoning.default_level ? blank.reasoning_mode : m.reasoning.mode,
  levels: rowsOf(Object.entries(m.reasoning.fragments ?? {}).map(([k, v]) => [k, JSON.stringify(v)])),
  reasoning_default: m.reasoning.default_level ?? "",
  check_enabled: !!m.chat_check,
  check_interval_ms: m.chat_check?.interval_ms ?? blank.check_interval_ms,
  check_timeout_ms: m.chat_check?.timeout_ms ?? blank.check_timeout_ms,
  check_unhealthy: m.chat_check?.unhealthy_threshold ?? null,
  check_healthy: m.chat_check?.healthy_threshold ?? null,
});

/** An empty string is "not set"; anything else must be a JSON object without gateway-owned keys. */
function jsonObjectProblem(text: string, reserved: string[] = RESERVED): string | undefined {
  if (!text.trim()) return undefined;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return "Not valid JSON";
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) return "Must be a JSON object";
  const taken = reserved.find((k) => k in value);
  return taken ? `Must not set "${taken}"` : undefined;
}

const parseObject = (text: string) => (text.trim() ? (JSON.parse(text) as Record<string, unknown>) : null);

/** A level row's own problem, or undefined. `others` are the other rows' names. */
function rowProblem(r: LevelRow, rows: LevelRow[]): { name?: string; json?: string } {
  const name = r.name.trim();
  const out: { name?: string; json?: string } = {};
  if (!LEVEL_NAME.test(name)) out.name = "Letters, digits, . _ - (max 32)";
  else if (rows.some((o) => o !== r && o.name.trim() === name)) out.name = "Already used";
  out.json = r.json.trim() ? jsonObjectProblem(r.json) : "Fields required";
  return out;
}
const levelsProblem = (rows: LevelRow[]) => {
  if (!rows.length) return "Add at least one level";
  if (rows.length > 16) return "At most 16 levels";
  const bad = rows.map((r) => rowProblem(r, rows)).find((p) => p.name || p.json);
  return bad ? (bad.name ?? bad.json) : undefined;
};

/** The API value; Off sends null so the client's reasoning_effort passes through untouched. */
function reasoning(v: Values) {
  if (!v.reasoning_on) return null;
  const fragments = v.reasoning_mode === "mapped" ? Object.fromEntries(v.levels.map((r) => [r.name.trim(), parseObject(r.json)!])) : null;
  return { mode: v.reasoning_mode, fragments, default_level: v.reasoning_mode === "strip" ? null : v.reasoning_default.trim() || null };
}

/** Prices are entered as USD per 1M tokens and stored as integer nano-USD per 1M tokens. */
const body = (v: Values) => ({
  name: v.name.trim(),
  upstream_id: v.upstream_id,
  upstream_model: v.upstream_model.trim(),
  price_input_nano_usd_per_mtok: usdToNano(v.price_in),
  price_output_nano_usd_per_mtok: usdToNano(v.price_out),
  max_input_tokens: v.max_input_tokens,
  max_output_tokens: v.max_output_tokens,
  default_body: parseObject(v.default_body),
  // Level names are free strings; schema.d.ts may still list the old literals.
  reasoning: reasoning(v),
  chat_check: v.check_enabled
    ? { interval_ms: v.check_interval_ms ?? 0, timeout_ms: v.check_timeout_ms ?? 0, unhealthy_threshold: v.check_unhealthy, healthy_threshold: v.check_healthy }
    : null,
});

/** Leaf field names of 422 messages mapped to form fields. */
const ALIAS = {
  price_input_nano_usd_per_mtok: "price_in",
  price_output_nano_usd_per_mtok: "price_out",
  reasoning: "reasoning_mode",
  mode: "reasoning_mode",
  fragments: "levels",
  default_level: "reasoning_default",
  interval_ms: "check_interval_ms",
  timeout_ms: "check_timeout_ms",
  unhealthy_threshold: "check_unhealthy",
  healthy_threshold: "check_healthy",
} as const;

export function ModelEdit() {
  const { modelId } = useParams({ strict: false }) as { modelId?: string };
  const list = useModels();
  const existing = modelId ? list.data?.find((m) => m.id === modelId) : undefined;
  if (modelId && list.isPending) return <PageLoading />;
  if (modelId && !existing) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{list.error?.message ?? "Model not found."}</AlertDescription>
      </Alert>
    );
  }
  return <ModelForm key={existing?.id ?? "new"} existing={existing} />;
}

function ModelForm({ existing }: { existing?: Model }) {
  const navigate = useNavigate();
  const upstreams = useUpstreams();
  const upstreamName = useNames(upstreams.data);
  const initial = existing ? fromModel(existing) : blank;
  const form = useForm<Values>(initial, {
    name: required("Name"),
    upstream_id: required("Upstream"),
    upstream_model: required("Upstream model"),
    // reasoning_effort has its own default below.
    default_body: (x) => jsonObjectProblem(x, [...RESERVED, "reasoning_effort"]),
    levels: (x, all) => (all.reasoning_on && all.reasoning_mode === "mapped" ? levelsProblem(x) : undefined),
    reasoning_default: (x, all) => {
      if (!all.reasoning_on || all.reasoning_mode === "strip") return undefined;
      if (all.reasoning_mode === "mapped") return x && !all.levels.some((r) => r.name.trim() === x) ? "This level is not in the list" : undefined;
      return LEVEL_NAME.test(x.trim()) ? undefined : "Letters, digits, . _ - (max 32)";
    },
    check_interval_ms: (x, all) => (all.check_enabled && (x ?? 0) < 1000 ? "At least 1000 ms" : undefined),
    check_timeout_ms: (x, all) => (all.check_enabled && ((x ?? 0) < 1 || (x ?? 0) >= (all.check_interval_ms ?? 0)) ? "Must be above 0 and below the interval" : undefined),
  });
  const v = form.values;
  const dirty = JSON.stringify(v) !== JSON.stringify(initial);
  const test = useChatTest(v);
  const renamed = !!existing && v.name.trim() !== existing.name;
  const save = useMut(
    (x: Values) => (existing ? api.updateModel(existing.id, { ...body(x), keep_old_name_as_alias: renamed && x.keep_old_name_as_alias }) : api.createModel(body(x))),
    existing ? "Model saved" : "Model created",
  );
  // The upstream's own list feeds the picker; any name can still be typed.
  const offered = useQuery({ queryKey: ["upstream-models", v.upstream_id], queryFn: () => api.upstreamModels(v.upstream_id), enabled: !!v.upstream_id, retry: false, staleTime: 60_000 });
  const listed = offered.data?.models ?? [];
  // Choosing a listed model brings its lengths along; a custom name leaves them as they are.
  const pickModel = (name: string) => {
    form.set("upstream_model", name);
    const info = listed.find((m) => m.id === name);
    if (info?.max_input_tokens) form.set("max_input_tokens", info.max_input_tokens);
    if (info?.max_output_tokens) form.set("max_output_tokens", info.max_output_tokens);
  };
  const typed = v.upstream_model.trim();
  const modelOptions = [
    ...(typed && !listed.some((m) => m.id === typed) ? [{ value: typed, label: `Use “${typed}” (custom)` }] : []),
    ...listed.map((m) => ({ value: m.id, label: m.id })),
  ];
  const modelNote = offered.error
    ? `Could not list the upstream's models (${offered.error.message}); type the name.`
    : offered.isFetching
      ? "Loading the upstream's models…"
      : undefined;
  const applyPreset = (p: (typeof PRESETS)[number]) => {
    form.set("reasoning_mode", p.mode);
    form.set("levels", (p.rows ?? []).map(([name, json]) => newRow(name, json)));
    form.set("reasoning_default", p.level ?? "");
  };
  const setRow = (key: number, patch: Partial<LevelRow>) => form.set("levels", v.levels.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const levelNames = v.levels.map((r) => r.name.trim()).filter((n, i, all) => LEVEL_NAME.test(n) && all.indexOf(n) === i);

  return (
    <EditPage
      title={existing ? existing.name : "New model"}
      description={v.upstream_id && v.upstream_model.trim() ? `${upstreamName(v.upstream_id)}/${v.upstream_model.trim()}` : undefined}
      back="/models"
      backLabel="Models"
      dirty={dirty}
      submitLabel={existing ? "Save" : "Create"}
      actions={
        <>
          {existing && <HealthBadge health={existing.health} />}
          {test.button}
        </>
      }
      onSubmit={async () => {
        if (!form.validate()) return false;
        const ok = await form.run(() => save.mutateAsync(v), ALIAS);
        if (ok === false) return false;
        await navigate({ to: "/models" });
      }}
    >
      {test.panel}

      <Section title="Model">
        <TextField label="Name" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus={!existing} hint="What clients send as model." />
        {renamed && <CheckField label="Keep old name as alias" checked={v.keep_old_name_as_alias} onChange={(c) => form.set("keep_old_name_as_alias", c)} />}
        <ComboboxField label="Upstream" value={v.upstream_id} onChange={(x) => form.set("upstream_id", x)} error={form.errors.upstream_id} options={(upstreams.data ?? []).map((u) => ({ value: u.id, label: u.name }))} />
        <ComboboxField
          free
          label="Upstream model"
          value={v.upstream_model}
          onChange={(x) => pickModel(String(x))}
          error={form.errors.upstream_model}
          placeholder="meta-llama/Llama-3.1-8B-Instruct"
          options={modelOptions}
          note={modelNote}
        />
      </Section>

      <Section title="Limits and pricing">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <NumberField label="Input length (tokens)" value={v.max_input_tokens} onChange={(x) => form.set("max_input_tokens", x)} error={form.errors.max_input_tokens} min={1} placeholder="" />
          <NumberField label="Max output (tokens)" value={v.max_output_tokens} onChange={(x) => form.set("max_output_tokens", x)} error={form.errors.max_output_tokens} min={1} placeholder="" />
          <NumberField label="Input price (USD / 1M tokens)" value={v.price_in} onChange={(x) => form.set("price_in", x)} error={form.errors.price_in} step="any" placeholder="0" />
          <NumberField label="Output price (USD / 1M tokens)" value={v.price_out} onChange={(x) => form.set("price_out", x)} error={form.errors.price_out} step="any" placeholder="0" />
        </div>
      </Section>

      <Section title="Default request body" hint="Merged into every request; fields the client sends win.">
        <JsonField label="JSON object" value={v.default_body} onChange={(x) => form.set("default_body", x)} error={form.errors.default_body} placeholder={'{\n  "temperature": 0.6,\n  "top_k": 20\n}'} rows={5} />
      </Section>

      <Section
        title="Reasoning effort"
        hint="Off forwards the client's reasoning_effort untouched."
        toggle={{ label: "Handle reasoning effort", checked: v.reasoning_on, onChange: (x) => form.set("reasoning_on", x) }}
      >
        {v.reasoning_on && (
          <>
            <div className="flex flex-wrap gap-2">
              {PRESETS.map((p) => (
                <Button key={p.label} type="button" variant="outline" size="sm" onClick={() => applyPreset(p)}>
                  {p.label}
                </Button>
              ))}
            </div>
            <SelectField label="Mode" value={v.reasoning_mode} onChange={(x) => form.set("reasoning_mode", x as Mode)} options={MODES} error={form.errors.reasoning_mode} />
            {v.reasoning_mode === "mapped" && (
              <div className="flex flex-col gap-2" data-testid="reasoning-levels">
                {form.errors.levels && <p className="text-sm text-destructive">{form.errors.levels}</p>}
                {v.levels.map((r, i) => {
                  const problem = rowProblem(r, v.levels);
                  return (
                    <div key={r.key} className="grid grid-cols-[1fr_auto] items-start gap-2 sm:grid-cols-[10rem_1fr_auto]">
                      <div className="flex min-w-0 flex-col gap-1">
                        <Input aria-label={`Level ${i + 1} name`} aria-invalid={!!r.name.trim() && !!problem.name} value={r.name} onChange={(e) => setRow(r.key, { name: e.target.value })} placeholder="level name" spellCheck={false} />
                        {r.name.trim() && problem.name && <p className="text-sm text-destructive">{problem.name}</p>}
                      </div>
                      <Button type="button" variant="ghost" size="icon" aria-label={`Remove level ${i + 1}`} className="sm:order-3" onClick={() => form.set("levels", v.levels.filter((o) => o.key !== r.key))}>
                        <Trash2Icon aria-hidden="true" />
                      </Button>
                      <div className="col-span-2 sm:order-2 sm:col-span-1">
                        <JsonField
                          label={`Level ${i + 1} fields`}
                          hideLabel
                          value={r.json}
                          onChange={(x) => setRow(r.key, { json: x })}
                          error={r.json.trim() ? problem.json : undefined}
                          placeholder='{"chat_template_kwargs": {"enable_thinking": true}}'
                          rows={1}
                        />
                      </div>
                    </div>
                  );
                })}
                <div>
                  <Button type="button" variant="outline" size="sm" onClick={() => form.set("levels", [...v.levels, newRow()])} disabled={v.levels.length >= 16}>
                    <PlusIcon aria-hidden="true" />
                    Add level
                  </Button>
                </div>
              </div>
            )}
            {v.reasoning_mode === "mapped" && (
              <SelectField
                label="Default level"
                value={v.reasoning_default || undefined}
                onChange={(x) => form.set("reasoning_default", x)}
                unset={{ label: "Not set", onSelect: () => form.set("reasoning_default", "") }}
                options={levelNames.map((l) => ({ value: l, label: l }))}
                error={form.errors.reasoning_default}
                hint="Used when the client sends no reasoning_effort."
              />
            )}
            {v.reasoning_mode === "passthrough" && (
              <TextField label="Default level" value={v.reasoning_default} onChange={(x) => form.set("reasoning_default", x)} error={form.errors.reasoning_default} placeholder="medium" hint="Used when the client sends no reasoning_effort." />
            )}
          </>
        )}
      </Section>

      <Section
        title="Periodic health check"
        hint="A short chat completion to this model. While on, each gateway sends it on this schedule (it uses tokens), and while it fails the model gets no traffic if another candidate is available."
        toggle={{ label: "Check periodically", checked: v.check_enabled, onChange: (x) => form.set("check_enabled", x) }}
      >
        {v.check_enabled && (
          <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
            <NumberField label="Interval (ms)" value={v.check_interval_ms} onChange={(x) => form.set("check_interval_ms", x)} error={form.errors.check_interval_ms} min={1000} placeholder="60000" />
            <NumberField label="Timeout (ms)" value={v.check_timeout_ms} onChange={(x) => form.set("check_timeout_ms", x)} error={form.errors.check_timeout_ms} min={1} placeholder="10000" />
            <NumberField label="Failures to mark down" value={v.check_unhealthy} onChange={(x) => form.set("check_unhealthy", x)} error={form.errors.check_unhealthy} min={1} placeholder="3" />
            <NumberField label="Successes to mark up" value={v.check_healthy} onChange={(x) => form.set("check_healthy", x)} error={form.errors.check_healthy} min={1} placeholder="1" />
          </div>
        )}
      </Section>

      {existing && <UsedBy model={existing} />}
    </EditPage>
  );
}

const CREDENTIAL: Record<ModelTest["credential"], string> = {
  stored: "Sent with the upstream's stored key.",
  env: "Sent with the variable from the control plane's environment.",
  env_unset: "The upstream's variable is not set on the control plane, so this test sent no key; gateways use their own environment.",
  none: "Sent without a key.",
};
/** One chat completion now, from the control plane, of the form as it stands: the button goes in the page actions, the result at the top of the form. */
function useChatTest(values: Values) {
  const [result, setResult] = useState<ModelTest | null>(null);
  const run = useMutation({
    mutationFn: () => {
      let defaultBody: Record<string, unknown> | null = null;
      try {
        defaultBody = parseObject(values.default_body);
      } catch {
        // An invalid body is flagged on the field; test without it.
      }
      return api.testModel({ upstream_id: values.upstream_id, upstream_model: values.upstream_model.trim(), default_body: defaultBody });
    },
    onSuccess: setResult,
  });
  const missing = !values.upstream_id ? "Pick the upstream first." : !values.upstream_model.trim() ? "Enter the upstream model first." : null;
  const button = (
    <Button type="button" variant="outline" size="sm" title={missing ?? undefined} disabled={!!missing || run.isPending} onClick={() => run.mutate()}>
      {run.isPending ? "Testing…" : "Test now"}
    </Button>
  );
  const panel = (run.error || result) && (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
      {run.error && <p className="text-sm text-destructive">{run.error.message}</p>}
      {result && <p className={result.credential === "env_unset" ? "text-sm text-destructive" : "text-sm text-muted-foreground"}>{CREDENTIAL[result.credential]}</p>}
      {result && (
        <DescriptionList data-testid="model-test-result">
          <ProbeRow label="Chat" probe={result.probe} />
          {result.reply !== null && (
            <>
              <DescriptionTerm>Reply</DescriptionTerm>
              <DescriptionDetails className="min-w-0 break-words">{result.reply || <span className="text-muted-foreground">(empty)</span>}</DescriptionDetails>
            </>
          )}
        </DescriptionList>
      )}
    </div>
  );
  return { button, panel };
}

function JsonField({ label, hideLabel, value, onChange, error, placeholder, rows }: { label: string; hideLabel?: boolean; value: string; onChange: (v: string) => void; error?: string; placeholder?: string; rows: number }) {
  const control = (p: { id?: string; "aria-invalid"?: boolean; "aria-describedby"?: string }) => (
    <Textarea {...p} aria-label={hideLabel ? label : undefined} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} rows={rows} spellCheck={false} className="min-h-0 font-mono text-xs" />
  );
  if (hideLabel) {
    return (
      <div className="flex min-w-0 flex-col gap-1">
        {control({ "aria-invalid": !!error })}
        {error && <p className="text-sm text-destructive">{error}</p>}
      </div>
    );
  }
  return (
    <Field label={label} error={error}>
      {control}
    </Field>
  );
}

function UsedBy({ model }: { model: Model }) {
  const routers = useRouters();
  const aliases = useAliases();
  const used = usedBy(model, routers.data ?? [], aliases.data ?? []);
  return (
    <Section title="Used by">
      {!used.routers.length && !used.aliases.length ? (
        <p className="text-sm text-muted-foreground">No router or alias uses this model.</p>
      ) : (
        <div className="flex flex-wrap gap-1">
          {used.routers.map((n) => (
            <Badge key={`r-${n}`} tone="info">
              {n}
            </Badge>
          ))}
          {used.aliases.map((n) => (
            <Badge key={`a-${n}`}>↳ {n}</Badge>
          ))}
        </div>
      )}
    </Section>
  );
}
