import { useNavigate, useParams } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";

import { api, type A2aAgent, type Schemas } from "../api";
import { Badge } from "../components/Bits";
import { EditPage, Section } from "../components/EditPage";
import { CheckField, Field, NumberField, SelectField, TextField, useForm } from "../components/Form";
import { NO_PLUGIN, PluginRefFields, targetNameProblem } from "../components/PluginRefFields";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { DescriptionDetails, DescriptionList, DescriptionTerm } from "../components/ui/description-list";
import { Textarea } from "../components/ui/textarea";
import { useA2aAgents, useMut, usePlugins } from "../hooks";
import { MAX_CARD_BYTES, cardView, clientPath, utf8Bytes } from "../lib/a2aCard";
import { PageLoading } from "./Access";
import { CardBadge } from "./A2aAgents";

/** `upstream-auth` and `a2a-middleware` settings as PluginRefFields edits them. */
interface PluginValue {
  plugin: string;
  params: Record<string, unknown>;
  timeout_ms: number;
}

type KeySource = "stored" | "env";
const KEY_SOURCES: { value: KeySource; label: string }[] = [
  { value: "stored", label: "Token (stored encrypted)" },
  { value: "env", label: "Environment variable on the gateway" },
];

interface Values {
  name: string;
  url: string;
  card_url: string;
  timeout_ms: number | null;
  auth: PluginValue;
  /** Only `static_bearer` carries a credential: a token sealed here, or a gateway env var name. */
  secret_source: KeySource;
  /** The token typed in the form; it only ever leaves as a request field, never read back. */
  token: string;
  secret_ref: string;
  middleware_on: boolean;
  middleware: PluginValue;
  card_json: string;
  force: boolean;
}

const NO_SETTINGS: PluginValue = { plugin: NO_PLUGIN, params: {}, timeout_ms: 0 };
const blank: Values = {
  name: "",
  url: "",
  card_url: "",
  timeout_ms: null,
  auth: { plugin: "none", params: {}, timeout_ms: 0 },
  secret_source: "stored",
  token: "",
  secret_ref: "",
  middleware_on: false,
  middleware: NO_SETTINGS,
  card_json: "",
  force: false,
};

/** The credential a saved agent has today: only its presence and the env var name come back from the API. */
const sourceOf = (agent: A2aAgent): KeySource => (agent.secret_ref ? "env" : "stored");

const fromAgent = (a: A2aAgent): Values => ({
  name: a.name,
  url: a.url,
  card_url: a.card_url,
  timeout_ms: a.timeout_ms || null,
  auth: { plugin: a.auth.plugin, params: a.auth.params, timeout_ms: a.auth.timeout_ms },
  secret_source: sourceOf(a),
  token: "",
  secret_ref: a.secret_ref ?? "",
  middleware_on: !!a.middleware,
  middleware: a.middleware
    ? { plugin: a.middleware.plugin, params: a.middleware.params, timeout_ms: a.middleware.timeout_ms }
    : NO_SETTINGS,
  card_json: a.card_json,
  force: false,
});

const ref = (v: PluginValue): Schemas["PluginRefIn"] => ({
  plugin: v.plugin,
  params: v.params,
  timeout_ms: v.timeout_ms,
});

/** The auth object; a PATCH that sends it replaces the whole thing, credential included. */
function authIn(v: Values): Schemas["TargetAuthIn"] {
  const credential =
    v.auth.plugin !== "static_bearer"
      ? {}
      : v.secret_source === "env"
        ? { secret_ref: v.secret_ref.trim() }
        : v.token.trim()
          ? { secret: v.token.trim() }
          : {};
  return { ...ref(v.auth), ...credential };
}

const sameParams = (a: Record<string, unknown>, b: Record<string, unknown>) => JSON.stringify(a) === JSON.stringify(b);

/** Whether this save has to replace the agent's auth (a PATCH without `auth` keeps the stored token). */
const authChanged = (v: Values, existing: A2aAgent) =>
  v.auth.plugin !== existing.auth.plugin ||
  v.auth.timeout_ms !== existing.auth.timeout_ms ||
  !sameParams(v.auth.params, existing.auth.params) ||
  (existing.auth.plugin === "static_bearer" &&
    (v.secret_source !== sourceOf(existing) || v.secret_ref.trim() !== (existing.secret_ref ?? "") || v.token.trim() !== ""));

function createBody(v: Values): Schemas["A2aAgentCreate"] {
  return {
    name: v.name.trim(),
    url: v.url.trim(),
    card_url: v.card_url.trim() || null,
    auth: authIn(v),
    middleware: v.middleware_on ? ref(v.middleware) : null,
    timeout_ms: v.timeout_ms ?? 0,
    card_json: v.card_json.trim() || null,
    force: v.force,
  };
}

/** PATCH only what changed: an unedited agent must not lose its sealed token or its settings. */
function patchBody(existing: A2aAgent, v: Values): Schemas["A2aAgentUpdate"] {
  const out: Schemas["A2aAgentUpdate"] = {};
  if (v.name.trim() !== existing.name) out.name = v.name.trim();
  if (v.url.trim() !== existing.url) out.url = v.url.trim();
  if (v.card_url.trim() !== existing.card_url) out.card_url = v.card_url.trim() || null;
  if ((v.timeout_ms ?? 0) !== existing.timeout_ms) out.timeout_ms = v.timeout_ms ?? 0;
  if (v.card_json.trim() !== existing.card_json) out.card_json = v.card_json.trim() || null;
  const middleware = v.middleware_on ? ref(v.middleware) : null;
  const before = existing.middleware
    ? { plugin: existing.middleware.plugin, params: JSON.stringify(existing.middleware.params), timeout_ms: existing.middleware.timeout_ms }
    : null;
  const after = middleware ? { plugin: middleware.plugin, params: JSON.stringify(middleware.params), timeout_ms: middleware.timeout_ms } : null;
  if (JSON.stringify(after) !== JSON.stringify(before)) out.middleware = middleware;
  if (authChanged(v, existing)) out.auth = authIn(v);
  if (v.force) out.force = true;
  return out;
}

/** Leaf field names of 422 messages mapped to form fields. */
const ALIAS = {
  card_url: "card_url",
  card_json: "card_json",
  secret: "token",
  secret_ref: "secret_ref",
  middleware: "middleware_on",
} as const;

const validUrl = (v: string) => {
  try {
    const url = new URL(v);
    return url.protocol === "http:" || url.protocol === "https:" ? undefined : "Must be an http(s) URL";
  } catch {
    return "Must be a valid URL";
  }
};

/** The gateways that lack one of the two selected plugins: what a forced save is saving past. */
function missingGateways(values: Values, auth: { name: string; missing_gateways: string[] }[], middleware: { name: string; missing_gateways: string[] }[]) {
  const authPlugin = auth.find((plugin) => plugin.name === values.auth.plugin);
  const middlewarePlugin = values.middleware_on ? middleware.find((plugin) => plugin.name === values.middleware.plugin) : undefined;
  return [...new Set([...(authPlugin?.missing_gateways ?? []), ...(middlewarePlugin?.missing_gateways ?? [])])];
}

export function A2aAgentEdit() {
  const { id } = useParams({ strict: false }) as { id?: string };
  const list = useA2aAgents();
  const existing = id ? list.data?.find((agent) => agent.id === id) : undefined;
  if (id && list.isPending) return <PageLoading />;
  if (id && !existing) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{list.error?.message ?? "Agent not found."}</AlertDescription>
      </Alert>
    );
  }
  return <A2aAgentForm key={existing?.id ?? "new"} existing={existing} />;
}

function A2aAgentForm({ existing }: { existing?: A2aAgent }) {
  const navigate = useNavigate();
  const initial = existing ? fromAgent(existing) : blank;
  // The same two queries PluginRefFields reads, so the missing-gateway check costs no extra request.
  const authPlugins = usePlugins("upstream-auth");
  const middlewarePlugins = usePlugins("a2a-middleware");
  const missingFor = (values: Values) =>
    missingGateways(values, authPlugins.data ?? [], middlewarePlugins.data ?? []);
  const form = useForm<Values>(initial, {
    name: (v) => (v.trim() ? targetNameProblem(v.trim()) : "Name is required"),
    url: (v) => (v.trim() ? validUrl(v.trim()) : "Agent URL is required"),
    card_url: (v) => (v.trim() ? validUrl(v.trim()) : undefined),
    token: (v, all) =>
      all.auth.plugin === "static_bearer" && all.secret_source === "stored" && !v.trim() && (!existing || authChanged(all, existing))
        ? existing?.secret_set
          ? "Type the token again: saving this auth section replaces the stored one"
          : "Enter the token"
        : undefined,
    secret_ref: (v, all) =>
      all.auth.plugin === "static_bearer" && all.secret_source === "env" && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(v.trim())
        ? "An environment variable name"
        : undefined,
    middleware: (v, all) => (all.middleware_on && !v.plugin.trim() ? "Pick a middleware plugin" : undefined),
    force: (v, all) => (missingFor(all).length > 0 && !v ? "Tick “Save anyway” to save while those gateways lack the plugin" : undefined),
    card_json: (v) => {
      const text = v.trim();
      if (!text) return undefined;
      if (utf8Bytes(text) > MAX_CARD_BYTES) return "At most 64 KiB";
      try {
        const parsed: unknown = JSON.parse(text);
        return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? undefined : "Must be a JSON object";
      } catch {
        return "Must be a JSON object";
      }
    },
  });
  const v = form.values;
  const missing = missingFor(v);
  const dirty = JSON.stringify(v) !== JSON.stringify(initial);
  const save = useMut(
    (values: Values) => (existing ? api.updateA2aAgent(existing.id, patchBody(existing, values)) : api.createA2aAgent(createBody(values))),
    existing ? "Agent saved" : "Agent created",
  );

  const authValue = { plugin: v.auth.plugin, params: v.auth.params as unknown, timeout_ms: v.auth.timeout_ms };
  const middlewareValue = { plugin: v.middleware.plugin, params: v.middleware.params as unknown, timeout_ms: v.middleware.timeout_ms };
  const middlewareOn = (on: boolean) => {
    form.set("middleware_on", on);
    form.set("force", false);
  };
  const changeAuth = (next: { plugin: string; params: unknown; timeout_ms: number }) => {
    const changed = next.plugin !== v.auth.plugin;
    // Settings belong to one plugin's schema; they do not carry over.
    form.set("auth", { plugin: next.plugin, params: changed ? {} : ((next.params ?? {}) as Record<string, unknown>), timeout_ms: next.timeout_ms });
    if (changed) form.set("force", false);
  };
  const changeMiddleware = (next: { plugin: string; params: unknown; timeout_ms: number }) => {
    const changed = next.plugin !== v.middleware.plugin;
    form.set("middleware", { plugin: next.plugin, params: changed ? {} : ((next.params ?? {}) as Record<string, unknown>), timeout_ms: next.timeout_ms });
    if (changed) form.set("force", false);
  };

  return (
    <EditPage
      title={existing ? existing.name : "New agent"}
      back="/a2a-agents"
      backLabel="A2A agents"
      dirty={dirty}
      submitLabel={existing ? "Save" : "Create"}
      actions={existing && <CardBadge agent={existing} />}
      onSubmit={async () => {
        if (!form.validate()) return false;
        const ok = await form.run(() => save.mutateAsync(v), ALIAS);
        if (ok === false) return false;
        await navigate({ to: "/a2a-agents" });
      }}
    >
      <Section title="Endpoint">
        <TextField
          label="Name"
          value={v.name}
          onChange={(x) => form.set("name", x)}
          error={form.errors.name}
          autoFocus={!existing}
          hint='Lowercase letters, digits, - and _, starting with a letter; no "__" (the tool name separator).'
        />
        <TextField
          label="Agent URL"
          type="url"
          value={v.url}
          onChange={(x) => form.set("url", x)}
          error={form.errors.url}
          placeholder="https://agent.example/a2a"
        />
        <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
          <TextField
            label="Card URL"
            type="url"
            value={v.card_url}
            onChange={(x) => form.set("card_url", x)}
            error={form.errors.card_url}
            placeholder="https://agent.example/a2a/.well-known/agent-card.json"
            hint="Empty: the origin of the Agent URL plus /.well-known/agent-card.json."
          />
          <NumberField
            label="Request timeout (ms)"
            value={v.timeout_ms}
            onChange={(x) => form.set("timeout_ms", x)}
            error={form.errors.timeout_ms}
            min={0}
            placeholder="0"
            hint="0 uses the gateway default (30 s)."
          />
        </div>
      </Section>

      {missing.length > 0 && (
        <Alert variant="warn" data-testid="missing-gateways">
          <TriangleAlert aria-hidden="true" />
          <AlertTitle>Not available on every gateway</AlertTitle>
          <AlertDescription>
            <p>
              {missing.join(", ")} {missing.length === 1 ? "does" : "do"} not have every plugin this agent uses. Saving sends them a config they
              reject, and they stop receiving updates, including key revocations, until it is fixed.
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

      <Section
        title="Auth"
        hint="static_bearer carries one token; passthrough forwards the client's Authorization."
      >
        <PluginRefFields kind="upstream-auth" value={authValue} onChange={changeAuth} />
        {v.auth.plugin === "static_bearer" && (
          <>
            <SelectField
              label="Credential source"
              value={v.secret_source}
              onChange={(x) => form.set("secret_source", x as KeySource)}
              options={KEY_SOURCES}
            />
            {v.secret_source === "stored" ? (
              <TextField
                label="Token"
                type="password"
                value={v.token}
                onChange={(x) => form.set("token", x)}
                error={form.errors.token}
                placeholder={existing?.secret_set ? "Stored — type a new token to replace it" : "Bearer token"}
                hint="Sealed at rest and never shown again; an auth change replaces the stored token, so type it again."
              />
            ) : (
              <TextField
                label="Variable name"
                value={v.secret_ref}
                onChange={(x) => form.set("secret_ref", x)}
                error={form.errors.secret_ref}
                placeholder="AGENT_TOKEN"
                hint="Every gateway must have it set."
              />
            )}
          </>
        )}
      </Section>

      <Section
        title="Middleware"
        hint="Runs once on every request to this agent, before it is forwarded."
        toggle={{ label: "Apply middleware", checked: v.middleware_on, onChange: middlewareOn }}
      >
        {v.middleware_on && (
          <>
            <PluginRefFields kind="a2a-middleware" value={middlewareValue} onChange={changeMiddleware} />
            {form.errors.middleware && (
              <p className="text-[13px] text-destructive" role="alert">
                {form.errors.middleware}
              </p>
            )}
          </>
        )}
      </Section>

      <CardSection values={v} existing={existing} error={form.errors.card_json} onChange={(text) => form.set("card_json", text)} />

      {existing?.config_invalid && (
        <Alert variant="warn" data-testid="config-invalid">
          <TriangleAlert aria-hidden="true" />
          <AlertTitle>Config invalid</AlertTitle>
          <AlertDescription>
            {existing.config_problems.length ? existing.config_problems.join("; ") : "The gateways cannot serve this agent as configured."}
          </AlertDescription>
        </Alert>
      )}
    </EditPage>
  );
}

/** The card: its URL, the pasted fallback and a plain-text preview of what a gateway serves. */
function CardSection({
  values,
  existing,
  error,
  onChange,
}: {
  values: Values;
  existing?: A2aAgent;
  error?: string;
  onChange: (text: string) => void;
}) {
  // A fetched card is what clients get; the pasted text is only previewed when there is none.
  const fetched = existing?.last_card_json ?? "";
  const view = cardView(fetched || values.card_json);
  const bytes = utf8Bytes(values.card_json.trim());
  const source = fetched
    ? `Fetched by a gateway${existing?.last_card_at ? ` at ${new Date(existing.last_card_at).toLocaleString()}` : ""}.`
    : values.card_json.trim()
      ? "From the pasted Card JSON below."
      : "";
  return (
    <Section title="Agent card" hint="The gateway fetches the card every few minutes and serves its own rewrite to clients.">
      <Field
        label="Pasted Card JSON"
        error={error}
        hint="Served while no gateway has fetched a card; an object of at most 64 KiB."
        note={values.card_json.trim() ? `${bytes.toLocaleString()} of 65,536 bytes` : "Empty: only a fetched card is served."}
      >
        {(p) => (
          <Textarea
            {...p}
            className="min-h-32 font-mono text-[13px]"
            spellCheck={false}
            value={values.card_json}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
      </Field>

      <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3" data-testid="card-preview">
        <div className="flex flex-wrap items-center gap-2">
          {existing && <CardBadge agent={existing} />}
          {source && <span className="text-sm text-muted-foreground">{source}</span>}
        </div>
        {existing?.last_card_error && (
          <Alert variant="destructive">
            <AlertDescription>{existing.last_card_error}</AlertDescription>
          </Alert>
        )}
        {view ? <CardFacts view={view} name={values.name} /> : <p className="text-sm text-muted-foreground">{values.card_json.trim() || fetched ? "The card is not a JSON object." : "No card yet."}</p>}
      </div>
    </Section>
  );
}

function CardFacts({ view, name }: { view: NonNullable<ReturnType<typeof cardView>>; name: string }) {
  const modes = (input: string[], output: string[]) =>
    input.length || output.length ? `${input.join(", ") || "—"} → ${output.join(", ") || "—"}` : "—";
  return (
    <DescriptionList>
      <DescriptionTerm>Clients call</DescriptionTerm>
      <DescriptionDetails className="flex min-w-0 flex-wrap items-baseline gap-2">
        <code data-testid="client-path">{clientPath(name)}</code>
        <span className="text-muted-foreground">relative to a gateway's public URL</span>
      </DescriptionDetails>
      <DescriptionTerm>Name</DescriptionTerm>
      <DescriptionDetails className="[overflow-wrap:anywhere]">{view.name || "—"}</DescriptionDetails>
      <DescriptionTerm>Version</DescriptionTerm>
      <DescriptionDetails className="[overflow-wrap:anywhere]">{view.version || "—"}</DescriptionDetails>
      <DescriptionTerm>Description</DescriptionTerm>
      <DescriptionDetails className="whitespace-pre-wrap [overflow-wrap:anywhere]">{view.description || "—"}</DescriptionDetails>
      <DescriptionTerm>MIME modes</DescriptionTerm>
      <DescriptionDetails className="[overflow-wrap:anywhere]">{modes(view.inputModes, view.outputModes)}</DescriptionDetails>
      <DescriptionTerm>Capabilities</DescriptionTerm>
      <DescriptionDetails className="flex flex-wrap gap-x-3 gap-y-1">
        {view.capabilities.length ? (
          view.capabilities.map((capability) => (
            <span key={capability.name}>
              <code>{capability.name}</code> {capability.on ? "yes" : "no"}
            </span>
          ))
        ) : (
          <span className="text-muted-foreground">None declared</span>
        )}
      </DescriptionDetails>
      <DescriptionTerm>Skills</DescriptionTerm>
      <DescriptionDetails className="flex flex-col gap-2">
        {view.skills.length ? (
          view.skills.map((skill, index) => (
            <div key={`${skill.id}-${index}`} className="flex min-w-0 flex-col gap-0.5">
              <span className="font-semibold [overflow-wrap:anywhere]">{skill.name || skill.id || "Unnamed skill"}</span>
              {skill.description && (
                <span className="whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{skill.description}</span>
              )}
              <span className="flex flex-wrap gap-x-2 gap-y-1 text-xs text-muted-foreground">
                {skill.tags.map((tag) => (
                  <Badge key={tag} tone="muted">
                    {tag}
                  </Badge>
                ))}
                {(skill.inputModes.length > 0 || skill.outputModes.length > 0) && (
                  <span>{modes(skill.inputModes, skill.outputModes)}</span>
                )}
              </span>
            </div>
          ))
        ) : (
          <span className="text-muted-foreground">None declared</span>
        )}
        {view.skillCount > view.skills.length && (
          <span className="text-muted-foreground">and {view.skillCount - view.skills.length} more</span>
        )}
      </DescriptionDetails>
    </DescriptionList>
  );
}
