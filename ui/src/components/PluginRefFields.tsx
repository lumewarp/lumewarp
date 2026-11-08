import { TriangleAlert } from "lucide-react";
import { useMemo } from "react";

import type { PluginKind } from "../api";
import { usePlugins } from "../hooks";
import { parseSchema, strategyDescription, strategyTitle, unknownKeys, type Values } from "../lib/schema";
import { NumberField, SelectField } from "./Form";
import { JsonEditor, SchemaFields } from "./SchemaForm";
import { Alert, AlertDescription } from "./ui/alert";
import { HelpTip } from "./ui/help-tip";

/**
 * The plugin half of an MCP server's or an A2A agent's outbound settings: which plugin, and its settings.
 *
 * Plugins and their schemas come from gateways (`GET /plugins`), so everything here is untrusted data: a
 * title, a description, an enum value and a schema are only ever rendered as text (`SchemaFields`), and a
 * schema outside the subset the form can show falls back to a JSON editor. A built-in plugin takes no
 * settings and has no timeout; a WASM plugin (`builtin === false`) takes a timeout in ms, where 0 is the
 * gateway's 60 second default.
 *
 * Credentials are not part of this component: only `static_bearer` takes one, and it is the caller's field
 * (a secret with its own "stored or environment variable" choice, write-only). `allowNone` adds an explicit
 * "None" row, whose value is the empty plugin name, for a settings group the caller does not model as a
 * switch.
 *
 * Errors are read from `errors` under the API's field names: `plugin`, `timeout_ms`, `params` (the
 * plugin's settings as a whole) and `params.<path>` for one setting, exactly as the control plane's 422
 * messages spell them. A JSON editor problem (text that is not a JSON object) is reported through
 * `onProblem`, so the owning form can refuse to submit; the caller's own schema errors come in already.
 */

/** The plugin reference as the API stores it (`PluginRefIn` / `PluginRefOut`). */
export interface PluginRefValue {
  plugin: string;
  params: unknown;
  timeout_ms: number;
}

/** The plugin name that means "no plugin" for `allowNone`. */
export const NO_PLUGIN = "";

const ROLE: Record<Exclude<PluginKind, "router-strategy">, string> = {
  "upstream-auth": "Upstream auth",
  "a2a-middleware": "A2A middleware",
};

/**
 * `^[a-z][a-z0-9_-]{0,62}$` and no `__` (the gateway splits an aggregated MCP tool name on the first
 * `__`): the name rule an MCP server and an A2A agent share. Exported for the A2A pages.
 */
const TARGET_NAME = /^[a-z][a-z0-9_-]{0,62}$/;

export function targetNameProblem(name: string): string | undefined {
  if (!TARGET_NAME.test(name) || name.includes("__")) {
    return "Lowercase letters, digits, “_” or “-”, starting with a letter, at most 63; two underscores in a row are refused";
  }
  return undefined;
}

const isObject = (v: unknown): v is Values => typeof v === "object" && v !== null && !Array.isArray(v);

interface Props {
  kind: Exclude<PluginKind, "router-strategy">;
  value: PluginRefValue;
  onChange: (value: PluginRefValue) => void;
  errors?: Record<string, string>;
  allowNone?: boolean;
  /** A JSON editor problem, or null once the text parses again. */
  onProblem?: (message: string | null) => void;
}

export function PluginRefFields({ kind, value, onChange, errors = {}, allowNone = false, onProblem }: Props) {
  const plugins = usePlugins(kind);
  const listed = plugins.data;
  const current = useMemo(() => listed?.find((p) => p.name === value.plugin), [listed, value.plugin]);
  const schema = useMemo(() => parseSchema(current?.params_schema), [current]);
  const params = isObject(value.params) ? value.params : {};
  const wasm = !!current && !current.builtin;
  const role = ROLE[kind];

  const options = useMemo(
    () => [
      ...(allowNone ? [{ value: NO_PLUGIN, label: "None" }] : []),
      ...(listed ?? []).map((p) => ({ value: p.name, label: strategyTitle(p) })),
      // A name no gateway reports any more stays visible instead of silently becoming the first option.
      ...(value.plugin !== NO_PLUGIN && !listed?.some((p) => p.name === value.plugin)
        ? [{ value: value.plugin, label: `${value.plugin} (not reported by any gateway)` }]
        : []),
    ],
    [allowNone, listed, value.plugin],
  );

  const set = (patch: Partial<PluginRefValue>) => onChange({ ...value, ...patch });
  const setParams = (next: Values) => set({ params: next });
  // Settings belong to one plugin: they never carry over to another.
  const changePlugin = (plugin: string) => set({ plugin, params: {}, timeout_ms: plugin === NO_PLUGIN ? 0 : value.timeout_ms });

  return (
    <div className="flex min-w-0 flex-col gap-3" role="group" aria-label={role}>
      <SelectField
        label="Plugin"
        value={value.plugin}
        onChange={changePlugin}
        options={options}
        error={errors.plugin}
        hint={current ? strategyDescription(current) || undefined : undefined}
        note={plugins.isPending ? "Loading the plugins the gateways report…" : plugins.error ? plugins.error.message : undefined}
      />

      {wasm && (
        <NumberField
          label="Timeout (ms)"
          value={value.timeout_ms ?? 0}
          onChange={(n) => set({ timeout_ms: n ?? 0 })}
          error={errors.timeout_ms}
          min={0}
          placeholder="0"
          hint="0 uses the 60 second default."
        />
      )}

      {current && schema ? <SchemaSettings role={role} schema={schema} params={params} onChange={setParams} errors={errors} /> : null}

      {current && !schema && current.builtin && (
        <p className="text-[13px] text-muted-foreground" data-testid="plugin-params-note">
          {Object.keys(params).length > 0 ? `Also set, and kept as is: ${Object.keys(params).join(", ")}. A built-in plugin takes no settings.` : "This built-in plugin takes no settings."}
        </p>
      )}

      {current && !schema && !current.builtin && <JsonSettings current={current} params={params} onChange={setParams} error={errors.params} onProblem={onProblem} />}

      {!current && value.plugin !== NO_PLUGIN && (
        <p className="text-[13px] text-muted-foreground">No gateway reports this plugin, so its settings cannot be shown or checked here; a save that names it is refused.</p>
      )}
    </div>
  );
}

/** The plugin's settings as a form from its schema, or nothing when it declares none. */
function SchemaSettings({
  role,
  schema,
  params,
  onChange,
  errors,
}: {
  role: string;
  schema: NonNullable<ReturnType<typeof parseSchema>>;
  params: Values;
  onChange: (v: Values) => void;
  errors: Record<string, string>;
}) {
  const extra = unknownKeys(schema.fields, params);
  if (!schema.fields.length && !extra.length) return null;
  const title = schema.title || `${role} settings`;
  // The caller keys one setting as `params.<path>`, the control plane's own spelling.
  const fieldErrors = Object.fromEntries(Object.entries(errors).filter(([key]) => key.startsWith("params.")).map(([key, message]) => [key.slice("params.".length), message]));
  return (
    <fieldset className="m-0 flex min-w-0 flex-col gap-3 rounded-xl border p-3">
      <legend className="px-1 font-semibold">
        <span className="flex items-center gap-1">
          {title}
          {schema.description && <HelpTip label={title} text={schema.description} />}
        </span>
      </legend>
      {errors.params && (
        <p className="mt-1 text-[13px] text-destructive" role="alert">
          {errors.params}
        </p>
      )}
      <SchemaFields fields={schema.fields} values={params} onChange={onChange} errors={fieldErrors} idPrefix="plugin" />
      {extra.length > 0 && <p className="text-[13px] text-muted-foreground">Also set, and kept as is: {extra.join(", ")}</p>}
    </fieldset>
  );
}

/** No schema the form can use: the settings are edited as JSON, with the gateway's reason. */
function JsonSettings({
  current,
  params,
  onChange,
  error,
  onProblem,
}: {
  current: { name: string; title: string; params_schema: unknown; schema_problems: string[] };
  params: Values;
  onChange: (v: Values) => void;
  error?: string;
  onProblem?: (message: string | null) => void;
}) {
  const title = `${strategyTitle(current)} settings`;
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="plugin-settings-json">
      <Alert variant="warn">
        <TriangleAlert aria-hidden="true" />
        <AlertDescription>
          {current.params_schema === null ? "No settings schema is known for this plugin." : "The settings schema is outside what the form can show."} Edit the settings as JSON.
        </AlertDescription>
      </Alert>
      {current.schema_problems.length > 0 && <p className="text-[13px] text-muted-foreground">{current.schema_problems.slice(0, 3).join("; ")}</p>}
      <JsonEditor key={current.name} label={title} value={params} onChange={onChange} onProblem={onProblem ?? (() => undefined)} error={error} hint="Settings are stored in config snapshots and audit logs: do not put secrets here." />
    </div>
  );
}
