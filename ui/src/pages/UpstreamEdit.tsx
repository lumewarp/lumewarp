import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api, type Upstream, type UpstreamTest } from "../api";
import { Badge } from "../components/Bits";
import { EditPage, Section } from "../components/EditPage";
import { NumberField, required, SelectField, TextField, useForm } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { useMut, useUpstreams } from "../hooks";
import { PageLoading } from "./Access";
import { HealthBadge, KINDS } from "./Upstreams";

const USAGE_MODES = [
  { value: "include_usage", label: "Request usage in the stream" },
  { value: "passthrough", label: "Pass through unchanged" },
];

type KeySource = "none" | "stored" | "env";
const KEY_SOURCES: { value: KeySource; label: string }[] = [
  { value: "none", label: "None" },
  { value: "stored", label: "API key (stored encrypted)" },
  { value: "env", label: "Environment variable on the gateway" },
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
}

const blank: Values = {
  name: "",
  kind: "vllm",
  base_url: "",
  key_source: "none",
  api_key: "",
  api_key_ref: "",
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
  key_source: u.api_key_set ? "stored" : u.api_key_ref ? "env" : "none",
  api_key: "",
  api_key_ref: u.api_key_ref ?? "",
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

/** The credential part of the body: a new key replaces, an untouched stored key is kept, switching source clears. */
function credential(v: Values, existing: Upstream | undefined) {
  if (v.key_source === "stored") return v.api_key.trim() ? { api_key: v.api_key.trim() } : existing?.api_key_set ? {} : { api_key: null };
  if (v.key_source === "env") return { api_key_ref: v.api_key_ref.trim() || null };
  return existing ? { api_key_ref: null, api_key: null } : {};
}

const body = (v: Values, existing: Upstream | undefined) => ({
  name: v.name,
  kind: v.kind,
  base_url: v.base_url,
  ...credential(v, existing),
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
} as const;

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

function UpstreamForm({ existing }: { existing?: Upstream }) {
  const navigate = useNavigate();
  const initial = existing ? fromUpstream(existing) : blank;
  const form = useForm<Values>(initial, {
    name: required("Name"),
    base_url: (v) => (v.trim() ? validUrl(v) : "Base URL is required"),
    api_key: (v, all) => (all.key_source === "stored" && !existing?.api_key_set && !v.trim() ? "Enter the API key" : undefined),
    api_key_ref: (v, all) => (all.key_source === "env" && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(v.trim()) ? "An environment variable name" : undefined),
    metrics_url: (v) => (v.trim() ? validUrl(v) : undefined),
    health_url: (v) => (v.trim() ? validUrl(v) : undefined),
    health_interval_ms: (v, all) => (all.health_enabled && (v ?? 0) < 1000 ? "At least 1000 ms" : undefined),
    health_timeout_ms: (v, all) => (all.health_enabled && ((v ?? 0) < 1 || (v ?? 0) >= (all.health_interval_ms ?? 0)) ? "Must be above 0 and below the interval" : undefined),
  });
  const dirty = JSON.stringify(form.values) !== JSON.stringify(initial);
  const test = useTestNow(existing, form.values);
  const save = useMut(
    (v: Values) => (existing ? api.updateUpstream(existing.id, body(v, existing)) : api.createUpstream(body(v, undefined))),
    existing ? "Upstream saved" : "Upstream created",
  );
  const v = form.values;
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
        </>
      }
      onSubmit={async () => {
        if (!form.validate()) return false;
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

      <Section title="Credential" hint="Sent to the upstream as a bearer token.">
        <SelectField label="Source" value={v.key_source} onChange={(x) => form.set("key_source", x as KeySource)} options={KEY_SOURCES} />
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
  entered: "Tested with the key typed above.",
  stored: "Tested with the stored key.",
  env: "Tested with the variable from the control plane's environment.",
  env_unset: "The variable is not set on the control plane, so this test sent no key; gateways use their own environment.",
  none: "Tested without a key.",
};

/** Test now of the form as it stands: the button goes in the page actions, the panel at the top of the form. */
function useTestNow(upstream: Upstream | undefined, values: Values) {
  const [result, setResult] = useState<UpstreamTest | null>(null);
  const qc = useQueryClient();
  const run = useMutation({ mutationFn: () => api.testUpstream(testBody(values, upstream)), onSuccess: setResult, onSettled: () => upstream && void qc.invalidateQueries({ queryKey: ["upstreams"] }) });
  useEffect(() => setResult(null), [upstream?.id]);
  const urlProblem = values.base_url.trim() ? validUrl(values.base_url.trim()) : "Enter the base URL first.";
  const button = (
    <Button type="button" variant="outline" size="sm" title={urlProblem ?? undefined} disabled={!!urlProblem || run.isPending} onClick={() => run.mutate()}>
      {run.isPending ? "Testing…" : "Test now"}
    </Button>
  );
  const panel = (run.error || result) && (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
      {run.error && <p className="text-sm text-destructive">{run.error.message}</p>}
      {result && <p className={result.credential === "env_unset" ? "text-sm text-destructive" : "text-sm text-muted-foreground"}>{CREDENTIAL[result.credential]}</p>}
      {result && (
        <DescriptionList data-testid="upstream-test-result">
          <ProbeRow label="Probe" probe={result.probe} />
        </DescriptionList>
      )}
    </div>
  );
  return { button, panel };
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
