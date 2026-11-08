import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useState } from "react";

import { api, type McpDiscover, type McpServer, type Schemas } from "../api";
import { Badge } from "../components/Bits";
import { Dialog } from "../components/Dialog";
import { EditPage, Section } from "../components/EditPage";
import { CheckField, NumberField, SelectField, TextField, useForm } from "../components/Form";
import { PluginRefFields, targetNameProblem } from "../components/PluginRefFields";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { useMcpServers, useMut, usePlugins } from "../hooks";
import { parseSchema, sameJson, validateValues, type Values as Params } from "../lib/schema";
import { PageLoading } from "./Access";
import { McpDefinitions } from "./McpDefinitions";

const REVIEW_MODES: { value: McpServer["review_mode"]; label: string }[] = [
  { value: "tofu", label: "TOFU — serve a new name at once" },
  { value: "manual", label: "Manual — approve every definition first" },
  { value: "off", label: "Off — do not check definitions" },
];

type CredentialSource = "stored" | "env";
const CREDENTIALS: { value: CredentialSource; label: string }[] = [
  { value: "stored", label: "Token (stored encrypted)" },
  { value: "env", label: "Environment variable on the gateway" },
];

interface Values {
  name: string;
  url: string;
  plugin: string;
  params: Params;
  plugin_timeout_ms: number | null;
  credential: CredentialSource;
  /** The token typed in the form; it only ever leaves as a request field, never read back. */
  token: string;
  secret_ref: string;
  timeout_ms: number | null;
  review_mode: McpServer["review_mode"];
  force: boolean;
}

const blank: Values = {
  name: "",
  url: "",
  plugin: "none",
  params: {},
  plugin_timeout_ms: null,
  credential: "stored",
  token: "",
  secret_ref: "",
  timeout_ms: null,
  review_mode: "tofu",
  force: false,
};

const fromServer = (s: McpServer): Values => ({
  name: s.name,
  url: s.url,
  plugin: s.auth.plugin,
  params: s.auth.params ?? {},
  plugin_timeout_ms: s.auth.timeout_ms || null,
  // A token whose name is stored lives on the gateway; a sealed one here is write-only.
  credential: s.secret_ref ? "env" : "stored",
  token: "",
  secret_ref: s.secret_ref ?? "",
  timeout_ms: s.timeout_ms || null,
  review_mode: s.review_mode,
  force: false,
});

const validUrl = (v: string) => {
  try {
    const url = new URL(v);
    return url.protocol === "http:" || url.protocol === "https:" ? undefined : "Must be an http(s) URL";
  } catch {
    return "Must be a valid URL";
  }
};

/** Whether the settings that make up the auth are as loaded (the credential is compared separately). */
const sameAuthSettings = (v: Values, existing: McpServer) =>
  v.plugin === existing.auth.plugin && sameJson(v.params, existing.auth.params) && (v.plugin_timeout_ms ?? 0) === existing.auth.timeout_ms;

/**
 * Nothing about the auth changes: send no `auth` at all, so a sealed token stays sealed.
 *
 * A PATCH replaces the whole auth object, so sending it with an untouched "stored" credential would clear
 * the token (`_auth_columns` in the control plane). Only a save that changes the plugin or its settings has
 * to carry the token again, which the form asks for.
 */
const authKept = (v: Values, existing: McpServer) =>
  sameAuthSettings(v, existing) &&
  (v.plugin !== "static_bearer" || (!v.token.trim() && (v.credential === "env" ? (existing.secret_ref ?? "") === v.secret_ref.trim() : existing.secret_set)));

const authBody = (v: Values): Schemas["TargetAuthIn"] => {
  const credential =
    v.plugin !== "static_bearer"
      ? {}
      : v.credential === "env"
        ? { secret_ref: v.secret_ref.trim() }
        : v.token.trim()
          ? { secret: v.token.trim() }
          : {};
  return { plugin: v.plugin, params: v.params, timeout_ms: v.plugin_timeout_ms ?? 0, ...credential };
};

const body = (v: Values, existing: McpServer | undefined, missing: boolean): Schemas["McpServerUpdate"] => ({
  name: v.name.trim(),
  url: v.url.trim(),
  timeout_ms: v.timeout_ms ?? 0,
  review_mode: v.review_mode,
  ...(existing && authKept(v, existing) ? {} : { auth: authBody(v) }),
  ...(missing && v.force ? { force: true } : {}),
});

const createBody = (v: Values, missing: boolean): Schemas["McpServerCreate"] => ({
  name: v.name.trim(),
  url: v.url.trim(),
  auth: authBody(v),
  timeout_ms: v.timeout_ms ?? 0,
  review_mode: v.review_mode,
  ...(missing && v.force ? { force: true } : {}),
});

/** `errors` of `PluginRefFields`, without the keys nothing was reported for. */
const definedErrors = (values: Record<string, string | undefined>): Record<string, string> =>
  Object.fromEntries(Object.entries(values).filter((entry): entry is [string, string] => entry[1] !== undefined));

/** The `auth` fields a 422 message names, each mapped to the form field that shows it. */
const AUTH_ALIAS: Record<string, keyof Values> = { plugin: "plugin", params: "params", timeout_ms: "plugin_timeout_ms", secret: "token", secret_ref: "secret_ref" };
/** Leaf field names of 422 messages mapped to form fields; the control plane names them `auth.<leaf>`. */
const ALIAS: Record<string, keyof Values> = Object.fromEntries(Object.entries(AUTH_ALIAS).map(([leaf, field]) => [`auth.${leaf}`, field]));

export function McpServerEdit() {
  const { serverId } = useParams({ strict: false }) as { serverId?: string };
  const list = useMcpServers();
  const existing = serverId ? list.data?.find((s) => s.id === serverId) : undefined;
  if (serverId && list.isPending) return <PageLoading />;
  if (serverId && !existing) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{list.error?.message ?? "MCP server not found."}</AlertDescription>
      </Alert>
    );
  }
  return <McpServerForm key={existing?.id ?? "new"} existing={existing} />;
}

function McpServerForm({ existing }: { existing?: McpServer }) {
  const navigate = useNavigate();
  const plugins = usePlugins("upstream-auth");
  const initial = existing ? fromServer(existing) : blank;
  const [submitted, setSubmitted] = useState(false);
  const [jsonProblem, setJsonProblem] = useState<string | null>(null);
  const form = useForm<Values>(initial, {
    name: (v) => (v.trim() ? targetNameProblem(v.trim()) : "Name is required"),
    url: (v) => (v.trim() ? validUrl(v.trim()) : "URL is required"),
    timeout_ms: (v) => (v !== null && v < 0 ? "Cannot be negative" : undefined),
    plugin_timeout_ms: (v) => (v !== null && v < 0 ? "Cannot be negative" : undefined),
    secret_ref: (v, all) => (all.plugin === "static_bearer" && all.credential === "env" && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(v.trim()) ? "An environment variable name" : undefined),
    token: (v, all) => {
      if (all.plugin !== "static_bearer" || all.credential !== "stored" || v.trim()) return undefined;
      // Nothing typed: the stored token is kept only while the rest of the auth is untouched.
      if (!existing || !existing.secret_set) return "Enter the token";
      return sameAuthSettings(all, existing) ? undefined : "Re-enter the token: changing the plugin or its settings replaces the stored one.";
    },
    force: (v, all) =>
      (plugins.data?.find((p) => p.name === all.plugin)?.missing_gateways.length ?? 0) > 0 && !v ? "Tick “Save anyway” to save while those gateways lack this plugin" : undefined,
  });
  const v = form.values;
  const dirty = JSON.stringify(form.values) !== JSON.stringify(initial);
  const current = plugins.data?.find((p) => p.name === v.plugin);
  const missing = current?.missing_gateways ?? [];

  const parsed = parseSchema(current?.params_schema);
  const schemaErrors = parsed ? Object.fromEntries(Object.entries(validateValues(parsed.fields, v.params)).map(([path, message]) => [`params.${path}`, message])) : {};

  const save = useMut(
    (values: Values) => (existing ? api.updateMcpServer(existing.id, body(values, existing, missing.length > 0)) : api.createMcpServer(createBody(values, missing.length > 0))),
    existing ? "MCP server saved" : "MCP server created",
  );

  return (
    <EditPage
      title={existing ? existing.name : "New MCP server"}
      back="/mcp-servers"
      backLabel="MCP servers"
      dirty={dirty}
      submitLabel={existing ? "Save" : "Create"}
      actions={<DiscoverNow url={v.url} serverId={existing?.id} />}
      onSubmit={async () => {
        setSubmitted(true);
        if (!form.validate()) return false;
        if (Object.keys(schemaErrors).length || jsonProblem) return false;
        const ok = await form.run(() => save.mutateAsync(v), ALIAS);
        if (ok === false) return false;
        await navigate({ to: "/mcp-servers" });
      }}
    >
      <Section title="Endpoint">
        <TextField label="Name" value={v.name} onChange={(x) => form.set("name", x)} error={form.errors.name} autoFocus={!existing} hint="Clients reach it at /mcp/<name>; two underscores in a row are not allowed." />
        <TextField label="URL" type="url" value={v.url} onChange={(x) => form.set("url", x)} error={form.errors.url} placeholder="https://mcp.example/mcp" hint="The server's Streamable HTTP endpoint." />
        <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
          <NumberField label="Upstream timeout (ms)" value={v.timeout_ms} onChange={(x) => form.set("timeout_ms", x)} error={form.errors.timeout_ms} min={0} placeholder="default" hint="0 uses the gateway's 30 second default." />
          <SelectField label="Review mode" value={v.review_mode} onChange={(x) => form.set("review_mode", x as Values["review_mode"])} options={REVIEW_MODES} error={form.errors.review_mode} hint="TOFU serves a name the first time it is seen and withholds a changed definition of a known name until it is approved." />
        </div>
      </Section>

      <Section title="Auth" hint="Sent to the upstream on every request; the client's own credentials are never forwarded.">
        <PluginRefFields
          kind="upstream-auth"
          value={{ plugin: v.plugin, params: v.params, timeout_ms: v.plugin_timeout_ms ?? 0 }}
          onChange={(next) => {
            form.set("plugin", next.plugin);
            form.set("params", next.params as Params);
            form.set("plugin_timeout_ms", next.timeout_ms || null);
          }}
          errors={submitted ? definedErrors({ ...schemaErrors, plugin: form.errors.plugin, params: form.errors.params, timeout_ms: form.errors.plugin_timeout_ms }) : {}}
          onProblem={setJsonProblem}
        />
        {v.plugin === "static_bearer" && (
          <>
            <SelectField label="Credential" value={v.credential} onChange={(x) => form.set("credential", x as CredentialSource)} options={CREDENTIALS} />
            {v.credential === "stored" ? (
              <TextField
                label="Token"
                type="password"
                value={v.token}
                onChange={(x) => form.set("token", x)}
                error={submitted ? form.errors.token : undefined}
                placeholder={existing?.secret_set ? "Stored — type a new token to replace it" : "Paste the token"}
                hint="Encrypted at rest and never shown again."
              />
            ) : (
              <TextField label="Variable name" value={v.secret_ref} onChange={(x) => form.set("secret_ref", x)} error={submitted ? form.errors.secret_ref : undefined} placeholder="MCP_TOKEN" hint="Every gateway must have it set." />
            )}
          </>
        )}
        {jsonProblem && (
          <Alert variant="destructive">
            <AlertDescription>{jsonProblem}</AlertDescription>
          </Alert>
        )}
        {missing.length > 0 && (
          <Alert variant="warn" data-testid="missing-gateways">
            <AlertTitle>Not available on every gateway</AlertTitle>
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
        {!current && v.plugin !== "none" && plugins.data !== undefined && <p className="text-[13px] text-muted-foreground">No gateway reports this plugin.</p>}
      </Section>

      {existing && <McpDefinitions server={existing} />}
    </EditPage>
  );
}

/**
 * One `tools/list` and `prompts/list` from the control plane, with a token typed into this dialog and held in
 * this dialog's state only: closing it drops the token, and nothing is cached or put in the URL.
 */
function DiscoverNow({ url, serverId }: { url: string; serverId?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button type="button" variant="outline" size="sm" disabled={!url.trim()} title={url.trim() ? undefined : "Enter the URL first."} onClick={() => setOpen(true)}>
        Discover now
      </Button>
      {open && <DiscoverDialog url={url.trim()} serverId={serverId} onClose={() => setOpen(false)} />}
    </>
  );
}

const CREDENTIAL: Record<McpDiscover["credential"], string> = {
  entered: "Used the token typed here; it was not stored.",
  stored: "Used the saved server's stored token.",
  env: "Used the variable from the control plane's own environment.",
  env_unset: "The variable is not set on the control plane, so this call sent no token; the gateways use their own environment.",
  none: "Sent no token.",
};

function DiscoverDialog({ url, serverId, onClose }: { url: string; serverId?: string; onClose: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<McpDiscover | null>(null);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const run = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const discovered = await api.discoverMcp({ url, timeout_ms: 10000, ...(serverId ? { server_id: serverId } : {}), ...(token.trim() ? { token: token.trim() } : {}) });
      setResult(discovered);
      // What was recorded changes the queue and the server's counts.
      if (discovered.recorded) void qc.invalidateQueries();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Discover now"
      footer={
        <>
          <Button type="button" variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button type="button" disabled={busy} onClick={() => void run()}>
            {busy ? "Asking…" : result ? "Ask again" : "Discover"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">
          Calls <code>{url}</code> once from the control plane, records what it lists, and stores no token.
        </p>
        <TextField label="Token" type="password" value={token} onChange={setToken} placeholder={serverId ? "Empty: use the saved credential" : "Empty: no credential"} hint="Sent once and never stored or audited." />
        {error && (
          <Alert variant="destructive" data-testid="discover-error">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {result && (
          <div className="flex flex-col gap-3" data-testid="discover-result">
            <p className={result.credential === "env_unset" ? "text-sm text-destructive" : "text-sm text-muted-foreground"}>{CREDENTIAL[result.credential]}</p>
            <p className="text-sm">
              Protocol <code>{result.protocol_version}</code>
              {result.recorded ? " · recorded against this server" : " · nothing was recorded (save the server first)"}
              {result.truncated ? " · the listing was cut short by the server's limits" : ""}
            </p>
            <Discovered title="Tools" items={result.tools} />
            <Discovered title="Prompts" items={result.prompts} />
          </div>
        )}
      </div>
    </Dialog>
  );
}

function Discovered({ title, items }: { title: string; items: McpDiscover["tools"] }) {
  return (
    <div className="flex flex-col gap-1">
      <h3 className="text-sm font-semibold">
        {title} ({items.length})
      </h3>
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">None listed.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {items.map((item) => (
            <li key={`${item.kind}:${item.name}`} className="flex flex-wrap items-center gap-2">
              <code className="[overflow-wrap:anywhere]">{item.name}</code>
              <Badge tone={item.state === "approved" ? "ok" : item.state === "pending" ? "warn" : item.state === "rejected" ? "danger" : "muted"}>{item.state}</Badge>
              <code className="text-xs text-muted-foreground" title={item.hash}>
                {item.hash.slice(0, 12)}
              </code>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
