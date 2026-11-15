import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api, ApiError, type Schemas, type Upstream, type UpstreamTest } from "../api";
import { Badge } from "../components/Bits";
import { EditPage, Section } from "../components/EditPage";
import { CheckField, NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { PluginRefFields } from "../components/PluginRefFields";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { HelpTip } from "../components/ui/help-tip";
import { useMut, usePlugins, useUpstreams } from "../hooks";
import { parseSchema, validateValues, type Values as Params } from "../lib/schema";
import { PageLoading } from "./Access";
import { HealthBadge, KINDS } from "./Upstreams";

const USAGE_MODES = [
  { value: "include_usage", label: "Request usage in the stream" },
  { value: "passthrough", label: "Pass through unchanged" },
];

type KeySource = "none" | "stored" | "env" | "plugin";
const KEY_SOURCES: { value: KeySource; label: string }[] = [
  { value: "none", label: "None" },
  { value: "stored", label: "API key (stored encrypted)" },
  { value: "env", label: "Environment variable on the gateway" },
  { value: "plugin", label: "Auth plugin" },
];

interface Values {
  name: string;
  kind: Upstream["kind"];
  base_url: string;
  key_source: KeySource;
  api_key: string;
  api_key_ref: string;
  timeout_ms: number | null;
  stream_usage_mode: Upstream["stream_usage_mode"];
  health_enabled: boolean;
  health_url: string;
  health_interval_ms: number | null;
  health_timeout_ms: number | null;
  health_unhealthy: number | null;
  health_healthy: number | null;
  breaker_enabled: boolean;
  breaker_failures: number | null;
  breaker_open_ms: number | null;
  metrics_url: string;
  auth_plugin: string;
  auth_params: Params;
  auth_timeout_ms: number | null;
  force: boolean;
}

const blank: Values = {
  name: "",
  kind: "vllm",
  base_url: "",
  key_source: "none",
  api_key: "",
  api_key_ref: "",
  auth_plugin: "",
  auth_params: {},
  auth_timeout_ms: null,
  force: false,
  timeout_ms: 600000,
  stream_usage_mode: "include_usage",
  health_enabled: false,
  health_url: "",
  health_interval_ms: 10000,
  health_timeout_ms: 2000,
  health_unhealthy: null,
  health_healthy: null,
  breaker_enabled: false,
  breaker_failures: null,
  breaker_open_ms: null,
  metrics_url: "",
};

const fromUpstream = (u: Upstream): Values => ({
  name: u.name,
  kind: u.kind,
  base_url: u.base_url,
  key_source: u.auth ? "plugin" : u.api_key_set ? "stored" : u.api_key_ref ? "env" : "none",
  api_key: "",
  api_key_ref: u.api_key_ref ?? "",
  auth_plugin: u.auth?.plugin ?? "",
  auth_params: (u.auth?.params ?? {}) as Params,
  auth_timeout_ms: u.auth?.timeout_ms || null,
  force: false,
  timeout_ms: u.timeout_ms,
  stream_usage_mode: u.stream_usage_mode,
  health_enabled: !!u.health_check,
  health_url: u.health_url ?? "",
  health_interval_ms: u.health_check?.interval_ms ?? blank.health_interval_ms,
  health_timeout_ms: u.health_check?.timeout_ms ?? blank.health_timeout_ms,
  health_unhealthy: u.health_check?.unhealthy_threshold ?? null,
  health_healthy: u.health_check?.healthy_threshold ?? null,
  breaker_enabled: !!u.circuit_breaker,
  breaker_failures: u.circuit_breaker?.failure_threshold ?? null,
  breaker_open_ms: u.circuit_breaker?.open_ms ?? null,
  metrics_url: u.metrics_url ?? "",
});

/** The auth plugin half of the body; only a WASM plugin is valid on an LLM upstream. */
const authBody = (v: Values): Schemas["UpstreamAuthIn"] => ({
  plugin: v.auth_plugin,
  params: v.auth_params,
  timeout_ms: v.auth_timeout_ms ?? 0,
});

/**
 * The credential part of the body: a new key replaces, an untouched stored key is kept, and switching
 * source clears the other credential. `auth: null` removes a plugin, so a switch to a key sends it too.
 */
function credential(v: Values, existing: Upstream | undefined) {
  if (v.key_source === "plugin") return { api_key: null, api_key_ref: null, auth: authBody(v) };
  if (v.key_source === "stored") {
    if (v.api_key.trim()) return { api_key: v.api_key.trim(), api_key_ref: null, auth: null };
    // Keep the stored key only when the saved upstream had one and no plugin to clear.
    return existing?.api_key_set && !existing.auth
      ? { api_key_ref: null, auth: null }
      : { api_key: null, api_key_ref: null, auth: null };
  }
  if (v.key_source === "env") return { api_key: null, api_key_ref: v.api_key_ref.trim() || null, auth: null };
  return { api_key: null, api_key_ref: null, auth: null };
}

const body = (v: Values, existing: Upstream | undefined, force: boolean) => ({
  name: v.name,
  kind: v.kind,
  base_url: v.base_url,
  ...credential(v, existing),
  ...(force ? { force: true } : {}),
  timeout_ms: v.timeout_ms ?? 600000,
  stream_usage_mode: v.stream_usage_mode,
  health_url: v.health_url.trim() || null,
  health_check: v.health_enabled
    ? {
        interval_ms: v.health_interval_ms ?? 0,
        timeout_ms: v.health_timeout_ms ?? 0,
        unhealthy_threshold: v.health_unhealthy,
        healthy_threshold: v.health_healthy,
      }
    : null,
  circuit_breaker: v.breaker_enabled ? { failure_threshold: v.breaker_failures, open_ms: v.breaker_open_ms } : null,
  metrics_url: v.metrics_url.trim() || null,
});

/** Leaf field names of 422 messages ("health_check.interval_ms: …") mapped to form fields. */
const ALIAS = {
  url: "health_url",
  interval_ms: "health_interval_ms",
  unhealthy_threshold: "health_unhealthy",
  healthy_threshold: "health_healthy",
  health_check: "health_timeout_ms",
  failure_threshold: "breaker_failures",
  open_ms: "breaker_open_ms",
  plugin: "auth_plugin",
  params: "auth_params",
  // An auth plugin's timeout shares the API leaf name with the upstream timeout.
  timeout_ms: "auth_timeout_ms",
} as const;

/** `errors` of `PluginRefFields`, without the keys nothing was reported for. */
const definedErrors = (values: Record<string, string | undefined>): Record<string, string> =>
  Object.fromEntries(Object.entries(values).filter((entry): entry is [string, string] => entry[1] !== undefined));

const validUrl = (v: string) => {
  try {
    const u = new URL(v);
    return u.protocol === "http:" || u.protocol === "https:" ? undefined : "Must be an http(s) URL";
  } catch {
    return "Must be a valid URL";
  }
};

/** What is probed when the Health URL is left empty: the base URL plus /models. */
export function defaultHealthUrl(baseUrl: string) {
  const base = baseUrl.trim().replace(/\/+$/, "");
  return base ? `${base}/models` : "<base_url>/models";
}

export function UpstreamEdit() {
  const { upstreamId } = useParams({ strict: false }) as { upstreamId?: string };
  const list = useUpstreams();
  const existing = upstreamId ? list.data?.find((u) => u.id === upstreamId) : undefined;
  if (upstreamId && list.isPending) return <PageLoading />;
  if (upstreamId && !existing) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{list.error?.message ?? "Upstream not found."}</AlertDescription>
      </Alert>
    );
  }
  return <UpstreamForm key={existing?.id ?? "new"} existing={existing} />;
}

const SAVE_FIRST = "Save the upstream first: Test now for an auth plugin runs on a gateway with the saved configuration.";

function UpstreamForm({ existing }: { existing?: Upstream }) {
  const navigate = useNavigate();
  const plugins = usePlugins("upstream-auth");
  const initial = existing ? fromUpstream(existing) : blank;
  const [submitted, setSubmitted] = useState(false);
  const [jsonProblem, setJsonProblem] = useState<string | null>(null);
  const form = useForm<Values>(initial, {
    name: required("Name"),
    base_url: (v) => (v.trim() ? validUrl(v) : "Base URL is required"),
    api_key: (v, all) => (all.key_source === "stored" && !existing?.api_key_set && !v.trim() ? "Enter the API key" : undefined),
    api_key_ref: (v, all) => (all.key_source === "env" && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(v.trim()) ? "An environment variable name" : undefined),
    auth_plugin: (v, all) => (all.key_source === "plugin" && !v ? "Choose a plugin" : undefined),
    auth_timeout_ms: (v) => (v !== null && v < 0 ? "Cannot be negative" : undefined),
    force: (v, all) =>
      all.key_source === "plugin" && (plugins.data?.find((p) => p.name === all.auth_plugin)?.missing_gateways.length ?? 0) > 0 && !v
        ? "Tick “Save anyway” to save while those gateways lack this plugin"
        : undefined,
    metrics_url: (v) => (v.trim() ? validUrl(v) : undefined),
    health_url: (v) => (v.trim() ? validUrl(v) : undefined),
    health_interval_ms: (v, all) => (all.health_enabled && (v ?? 0) < 1000 ? "At least 1000 ms" : undefined),
    health_timeout_ms: (v, all) => (all.health_enabled && ((v ?? 0) < 1 || (v ?? 0) >= (all.health_interval_ms ?? 0)) ? "Must be above 0 and below the interval" : undefined),
  });
  const v = form.values;
  const dirty = JSON.stringify(v) !== JSON.stringify(initial);
  const currentPlugin = plugins.data?.find((p) => p.name === v.auth_plugin);
  const missing = v.key_source === "plugin" ? (currentPlugin?.missing_gateways ?? []) : [];
  const parsed = parseSchema(currentPlugin?.params_schema);
  const schemaErrors = parsed
    ? Object.fromEntries(Object.entries(validateValues(parsed.fields, v.auth_params)).map(([path, message]) => [`params.${path}`, message]))
    : {};
  const pluginMode = v.key_source === "plugin" || !!existing?.auth;
  const saveFirst = pluginMode && (!existing || dirty) ? SAVE_FIRST : undefined;
  const test = useTestNow(existing, v, pluginMode, saveFirst);
  const save = useMut(
    (values: Values) =>
      existing
        ? api.updateUpstream(existing.id, body(values, existing, values.key_source === "plugin" && missing.length > 0 && values.force))
        : api.createUpstream(body(values, undefined, values.key_source === "plugin" && missing.length > 0 && values.force)),
    existing ? "Upstream saved" : "Upstream created",
  );
  return (
    <EditPage
      title={existing ? existing.name : "New upstream"}
      back="/upstreams"
      backLabel="Upstreams"
      dirty={dirty}
      submitLabel={existing ? "Save" : "Create"}
      actions={
        <>
          {existing && <HealthBadge health={existing.health} probe={existing.probe} />}
          {test.button}
          {test.hint && <HelpTip label="Test now" text={test.hint} />}
        </>
      }
      onSubmit={async () => {
        setSubmitted(true);
        if (!form.validate()) return false;
        if (v.key_source === "plugin" && (Object.keys(schemaErrors).length > 0 || jsonProblem)) return false;
        const ok = await form.run(() => save.mutateAsync(v), ALIAS);
        if (ok === false) return false;
        await navigate({ to: "/upstreams" });
      }}
    >
      {test.panel}

      <Section title="Endpoint">
        <TextField label="Name" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus={!existing} />
        <SelectField label="Kind" value={v.kind} onChange={(x) => form.set("kind", x as Values["kind"])} options={KINDS} />
        <TextField label="Base URL" type="url" value={v.base_url} onChange={(x) => form.set("base_url", x)} error={form.errors.base_url} placeholder="http://vllm:8000/v1" />
        <TextField label="Health URL" type="url" value={v.health_url} onChange={(x) => form.set("health_url", x)} error={form.errors.health_url} placeholder={defaultHealthUrl(v.base_url)} hint="Test now and the periodic probe call this URL (empty: the base URL plus /models), and a 2xx answer counts as healthy." />
        <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
          <NumberField label="Timeout (ms)" value={v.timeout_ms} onChange={(x) => form.set("timeout_ms", x)} error={form.errors.timeout_ms} min={1} />
          <SelectField label="Streaming usage" value={v.stream_usage_mode} onChange={(x) => form.set("stream_usage_mode", x as Values["stream_usage_mode"])} options={USAGE_MODES} />
        </div>
      </Section>

      <Section title="Credential" hint="Sent to the upstream on every request; the client's own credentials are never forwarded.">
        <SelectField label="Source" value={v.key_source} onChange={(x) => form.set("key_source", x as KeySource)} options={KEY_SOURCES} />
        {saveFirst && (
          <p className="text-xs text-muted-foreground" data-testid="test-now-hint">
            {saveFirst}
          </p>
        )}
        {v.key_source === "stored" && (
          <TextField
            label="API key"
            type="password"
            value={v.api_key}
            onChange={(x) => form.set("api_key", x)}
            error={form.errors.api_key}
            placeholder={existing?.api_key_set ? "Stored — type a new key to replace it" : "sk-…"}
            hint="Encrypted at rest and never shown again."
          />
        )}
        {v.key_source === "env" && (
          <TextField label="Variable name" value={v.api_key_ref} onChange={(x) => form.set("api_key_ref", x)} error={form.errors.api_key_ref} placeholder="OPENAI_API_KEY" hint="Every gateway must have it set." />
        )}
        {v.key_source === "plugin" && (
          <>
            <PluginRefFields
              kind="upstream-auth"
              wasmOnly
              value={{ plugin: v.auth_plugin, params: v.auth_params, timeout_ms: v.auth_timeout_ms ?? 0 }}
              onChange={(next) => {
                form.set("auth_plugin", next.plugin);
                form.set("auth_params", next.params as Params);
                form.set("auth_timeout_ms", next.timeout_ms || null);
              }}
              errors={
                submitted
                  ? definedErrors({
                      ...schemaErrors,
                      plugin: form.errors.auth_plugin,
                      params: form.errors.auth_params,
                      timeout_ms: form.errors.auth_timeout_ms,
                    })
                  : {}
              }
              onProblem={setJsonProblem}
            />
            {jsonProblem && (
              <Alert variant="destructive">
                <AlertDescription>{jsonProblem}</AlertDescription>
              </Alert>
            )}
            {missing.length > 0 && (
              <Alert variant="warn" data-testid="missing-gateways">
                <AlertDescription>
                  <p>
                    {missing.join(", ")} {missing.length === 1 ? "does" : "do"} not have this auth plugin. Saving sends them a config they reject, and they stop receiving updates, including key revocations, until it is fixed.
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
            {!currentPlugin && v.auth_plugin && plugins.data !== undefined && <p className="text-[13px] text-muted-foreground">No gateway reports this plugin.</p>}
          </>
        )}
      </Section>

      <Section
        title="Periodic health probe"
        hint="While the probe fails, the upstream gets no traffic if another candidate is available."
        toggle={{ label: "Probe periodically", checked: v.health_enabled, onChange: (x) => form.set("health_enabled", x) }}
      >
        {v.health_enabled && (
          <>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
              <NumberField label="Interval (ms)" value={v.health_interval_ms} onChange={(x) => form.set("health_interval_ms", x)} error={form.errors.health_interval_ms} min={1000} placeholder="10000" />
              <NumberField label="Timeout (ms)" value={v.health_timeout_ms} onChange={(x) => form.set("health_timeout_ms", x)} error={form.errors.health_timeout_ms} min={1} placeholder="2000" />
              <NumberField label="Failures to mark down" value={v.health_unhealthy} onChange={(x) => form.set("health_unhealthy", x)} error={form.errors.health_unhealthy} min={1} placeholder="3" />
              <NumberField label="Successes to mark up" value={v.health_healthy} onChange={(x) => form.set("health_healthy", x)} error={form.errors.health_healthy} min={1} placeholder="1" />
            </div>
          </>
        )}
      </Section>

      <Section
        title="Circuit breaker"
        hint="Stops sending to a model after repeated failures, then lets one request through to retry."
        toggle={{ label: "Circuit breaker", checked: v.breaker_enabled, onChange: (x) => form.set("breaker_enabled", x) }}
      >
        {v.breaker_enabled && (
          <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
            <NumberField label="Consecutive failures" value={v.breaker_failures} onChange={(x) => form.set("breaker_failures", x)} error={form.errors.breaker_failures} min={1} placeholder="5" />
            <NumberField label="Open time (ms)" value={v.breaker_open_ms} onChange={(x) => form.set("breaker_open_ms", x)} error={form.errors.breaker_open_ms} min={1} placeholder="30000" />
          </div>
        )}
      </Section>

      <Section title="Advanced">
        <TextField label="Metrics URL" type="url" value={v.metrics_url} onChange={(x) => form.set("metrics_url", x)} error={form.errors.metrics_url} placeholder="http://vllm:8000/metrics" hint="Only for the vLLM load router strategy. Empty: base URL without /v1 + /metrics." />
      </Section>
    </EditPage>
  );
}

/** The form as it stands, saved or not: a typed key wins, else the stored one, else the env variable. */
function testBody(v: Values, upstream: Upstream | undefined) {
  const key = v.key_source === "stored" && v.api_key.trim() ? { api_key: v.api_key.trim() } : v.key_source === "stored" && upstream?.api_key_set ? { upstream_id: upstream.id } : v.key_source === "env" && v.api_key_ref.trim() ? { api_key_ref: v.api_key_ref.trim() } : {};
  // The saved upstream's id lets the control plane keep the result when the form matches it;
  // it is left out when the form drops a stored key, which the id would otherwise send.
  const saved = upstream && (v.key_source === "stored" || !upstream.api_key_set) ? { upstream_id: upstream.id } : {};
  return { base_url: v.base_url.trim(), health_url: v.health_url.trim() || null, ...saved, ...key };
}

const CREDENTIAL: Record<UpstreamTest["credential"], string> = {
  plugin: "Tested through a connected gateway with the saved auth plugin.",
  entered: "Tested with the key typed above.",
  stored: "Tested with the stored key.",
  env: "Tested with the variable from the control plane's environment.",
  env_unset: "The variable is not set on the control plane, so this test sent no key; gateways use their own environment.",
  none: "Tested without a key.",
};

/** The control plane's own explanation for a saved-test refusal, kept as one inline sentence. */
export function savedTestError(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : String(error);
  if (error.code === "no_gateway_for_plugin") return `No connected gateway can run this probe: ${error.message}`;
  if (error.code === "probe_timeout") return "The gateway did not answer within the probe timeout.";
  if (error.code === "probe_gateway_gone") return "The gateway disconnected before it answered; test again.";
  return error.message;
}

const PROBE_ERROR: Record<string, string> = {
  not_found: "The gateway has not applied the saved upstream version yet; test again in a moment.",
  not_found_model: "The gateway has not applied the saved model version yet; test again in a moment.",
  config: "The gateway's current config does not include this plugin or target.",
  auth: "The auth plugin failed to produce a credential.",
  connect: "The gateway could not connect to the upstream.",
  timeout: "The upstream did not answer within its timeout.",
  protocol: "The upstream answered with an unexpected protocol.",
};

/** A fixed gateway probe error class as readable text; `null` when the status already says it. */
export function probeErrorText(error: string | null | undefined): string | undefined {
  return error ? (PROBE_ERROR[error] ?? error) : undefined;
}

/** Test now: the button goes in the page actions, the panel at the top of the form. */
function useTestNow(upstream: Upstream | undefined, values: Values, pluginMode: boolean, saveReason: string | undefined) {
  const [result, setResult] = useState<UpstreamTest | null>(null);
  const qc = useQueryClient();
  const run = useMutation({
    mutationFn: () => (pluginMode && upstream ? api.testSavedUpstream(upstream.id) : api.testUpstream(testBody(values, upstream))),
    onSuccess: setResult,
    onSettled: () => upstream && void qc.invalidateQueries({ queryKey: ["upstreams"] }),
  });
  useEffect(() => setResult(null), [upstream?.id, pluginMode, values.key_source]);
  const urlProblem = values.base_url.trim() ? validUrl(values.base_url.trim()) : "Enter the base URL first.";
  const blocked = pluginMode ? saveReason : urlProblem;
  const button = (
    <Button type="button" variant="outline" size="sm" title={blocked ?? undefined} disabled={!!blocked || run.isPending} onClick={() => run.mutate()}>
      {run.isPending ? "Testing…" : "Test now"}
    </Button>
  );
  const panel = (run.error || result) && (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
      {run.error && (
        <p className="text-sm text-destructive" data-testid="upstream-test-error">
          {savedTestError(run.error)}
        </p>
      )}
      {result && <p className={result.credential === "env_unset" ? "text-sm text-destructive" : "text-sm text-muted-foreground"}>{CREDENTIAL[result.credential]}</p>}
      {result && (
        <DescriptionList data-testid="upstream-test-result">
          <ProbeRow label="Probe" probe={result.probe} />
        </DescriptionList>
      )}
      {result && probeErrorText(result.probe.error) && (
        <p className="text-sm text-destructive" data-testid="upstream-probe-error">
          {probeErrorText(result.probe.error)}
        </p>
      )}
    </div>
  );
  return { button, hint: saveReason, panel };
}

export function ProbeRow({ label, probe }: { label: string; probe: UpstreamTest["probe"] }) {
  return (
    <>
      <DescriptionTerm>{label}</DescriptionTerm>
      <DescriptionDetails className="flex min-w-0 flex-wrap items-center gap-2">
        <Badge tone={probe.ok ? "ok" : "danger"}>{probe.ok ? "OK" : "Failed"}</Badge>
        <span>{probe.status !== null ? `HTTP ${probe.status}` : probe.error}</span>
        <span className="text-muted-foreground">{probe.latency_ms} ms</span>
        <code className="truncate text-muted-foreground">{probe.url}</code>
      </DescriptionDetails>
    </>
  );
}
