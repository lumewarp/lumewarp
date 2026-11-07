import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";

import { api, type Model } from "../api";
import { Badge, nanoToUsd, usdToNano } from "../components/Bits";
import { EditPage, Section } from "../components/EditPage";
import { CheckField, Field, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { ComboboxField } from "../components/ui/combobox-field";
import { Textarea } from "../components/ui/textarea";
import { useAliases, useModels, useMut, useNames, useRouters, useUpstreams } from "../hooks";
import { usedBy } from "../lib/catalog";
import { PageLoading } from "./Access";

const LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh"] as const;
type Level = (typeof LEVELS)[number];
type Mode = Model["reasoning"]["mode"];
/** Keys the gateway owns; the API refuses them in a default body or a reasoning fragment. */
const RESERVED = ["model", "stream", "stream_options"];

const MODES: { value: Mode; label: string }[] = [
  { value: "passthrough", label: "Pass through" },
  { value: "mapped", label: "Map each level to request fields" },
  { value: "strip", label: "Remove (model has no reasoning control)" },
];

const thinking = (key: string, on: boolean) => JSON.stringify({ chat_template_kwargs: { [key]: on } });
const PRESETS: { label: string; mode: Mode; fragments?: Partial<Record<Level, string>>; level?: Level | "" }[] = [
  { label: "Qwen3 (enable_thinking)", mode: "mapped", fragments: { none: thinking("enable_thinking", false), low: thinking("enable_thinking", true), medium: thinking("enable_thinking", true), high: thinking("enable_thinking", true) } },
  { label: "DeepSeek (thinking)", mode: "mapped", fragments: { none: thinking("thinking", false), low: thinking("thinking", true), medium: thinking("thinking", true), high: thinking("thinking", true) } },
  { label: "Native (gpt-oss, OpenAI)", mode: "passthrough" },
  { label: "None (strip)", mode: "strip" },
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
  reasoning_mode: Mode;
  fragments: Record<Level, string>;
  reasoning_default: Level | "";
}

const noFragments = () => Object.fromEntries(LEVELS.map((l) => [l, ""])) as Record<Level, string>;

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
  reasoning_mode: "passthrough",
  fragments: noFragments(),
  reasoning_default: "",
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
  reasoning_mode: m.reasoning.mode,
  fragments: { ...noFragments(), ...Object.fromEntries(Object.entries(m.reasoning.fragments ?? {}).map(([k, v]) => [k, JSON.stringify(v)])) },
  reasoning_default: (m.reasoning.default_level as Level | null) ?? "",
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
const mappedLevels = (v: Values) => LEVELS.filter((l) => v.fragments[l].trim());

function reasoning(v: Values) {
  if (v.reasoning_mode === "passthrough" && !v.reasoning_default) return null;
  const fragments = v.reasoning_mode === "mapped" ? Object.fromEntries(mappedLevels(v).map((l) => [l, parseObject(v.fragments[l])!])) : null;
  return { mode: v.reasoning_mode, fragments, default_level: v.reasoning_mode === "strip" ? null : v.reasoning_default || null };
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
  reasoning: reasoning(v),
});

/** Leaf field names of 422 messages mapped to form fields. */
const ALIAS = {
  price_input_nano_usd_per_mtok: "price_in",
  price_output_nano_usd_per_mtok: "price_out",
  reasoning: "reasoning_mode",
  mode: "reasoning_mode",
  fragments: "reasoning_mode",
  default_level: "reasoning_default",
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
    fragments: (x, all) => (all.reasoning_mode === "mapped" ? LEVELS.map((l) => jsonObjectProblem(x[l])).find(Boolean) : undefined),
    reasoning_mode: (x, all) => (x === "mapped" && !mappedLevels(all).length ? "Map at least one level" : undefined),
    reasoning_default: (x, all) => (x && all.reasoning_mode === "mapped" && !all.fragments[x].trim() ? "This level has no mapping" : undefined),
  });
  const v = form.values;
  const dirty = JSON.stringify(v) !== JSON.stringify(initial);
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
  const modelHint = offered.error
    ? `Could not list the upstream's models (${offered.error.message}); type the name.`
    : offered.isFetching
      ? "Loading the upstream's models…"
      : "Pick from the upstream's list, or type any name it accepts.";
  const applyPreset = (p: (typeof PRESETS)[number]) => {
    form.set("reasoning_mode", p.mode);
    form.set("fragments", { ...noFragments(), ...p.fragments });
    form.set("reasoning_default", p.level ?? "");
  };
  const defaultLevels = v.reasoning_mode === "mapped" ? mappedLevels(v) : LEVELS;

  return (
    <EditPage
      title={existing ? existing.name : "New model"}
      description={v.upstream_id && v.upstream_model.trim() ? `${upstreamName(v.upstream_id)}/${v.upstream_model.trim()}` : "A public name for one model on one upstream."}
      back="/models"
      backLabel="Models"
      dirty={dirty}
      submitLabel={existing ? "Save" : "Create"}
      onSubmit={async () => {
        if (!form.validate()) return false;
        const ok = await form.run(() => save.mutateAsync(v), ALIAS);
        if (ok === false) return false;
        await navigate({ to: "/models" });
      }}
    >
      <Section title="Model">
        <TextField label="Name" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus={!existing} hint="What clients send as `model`." />
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
          hint={modelHint}
        />
      </Section>

      <Section title="Limits and pricing">
        <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
          <NumberField label="Input length (tokens)" value={v.max_input_tokens} onChange={(x) => form.set("max_input_tokens", x)} error={form.errors.max_input_tokens} min={1} placeholder="unknown" />
          <NumberField label="Max output (tokens)" value={v.max_output_tokens} onChange={(x) => form.set("max_output_tokens", x)} error={form.errors.max_output_tokens} min={1} hint="Larger max_tokens gets a 400." />
          <NumberField label="Input price (USD / 1M tokens)" value={v.price_in} onChange={(x) => form.set("price_in", x)} error={form.errors.price_in} step="any" placeholder="unpriced" />
          <NumberField label="Output price (USD / 1M tokens)" value={v.price_out} onChange={(x) => form.set("price_out", x)} error={form.errors.price_out} step="any" placeholder="unpriced" />
        </div>
      </Section>

      <Section title="Default request body" description="Merged into every request; fields the client sends win.">
        <JsonField label="JSON object" value={v.default_body} onChange={(x) => form.set("default_body", x)} error={form.errors.default_body} placeholder={'{\n  "temperature": 0.6,\n  "top_k": 20\n}'} rows={5} />
      </Section>

      <Section title="Reasoning effort" description="How the client's reasoning_effort reaches the upstream.">
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
            {form.errors.fragments && <p className="text-sm text-destructive">{form.errors.fragments}</p>}
            {LEVELS.map((level) => (
              <div key={level} className="grid grid-cols-[6rem_1fr] items-start gap-3">
                <Badge tone={v.fragments[level].trim() ? "info" : "muted"}>{level}</Badge>
                <JsonField
                  label={`Fields for ${level}`}
                  hideLabel
                  value={v.fragments[level]}
                  onChange={(x) => form.set("fragments", { ...v.fragments, [level]: x })}
                  error={v.fragments[level].trim() ? jsonObjectProblem(v.fragments[level]) : undefined}
                  placeholder="not accepted"
                  rows={1}
                />
              </div>
            ))}
          </div>
        )}
        {v.reasoning_mode !== "strip" && (
          <SelectField
            label="Default level"
            value={v.reasoning_default || undefined}
            onChange={(x) => form.set("reasoning_default", x as Level)}
            unset={{ label: "Not set", onSelect: () => form.set("reasoning_default", "") }}
            options={defaultLevels.map((l) => ({ value: l, label: l }))}
            error={form.errors.reasoning_default}
            hint="Used when the client sends no reasoning_effort."
          />
        )}
      </Section>

      {existing && <UsedBy model={existing} />}
    </EditPage>
  );
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
