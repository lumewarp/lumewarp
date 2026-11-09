import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { api, type Key, type Me, type Schemas } from "../api";
import { Badge, fmtDate, fmtUsd, KeyBadge, PageHeader, SuspendedReason, TypeBadge } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { blankKeyLimits, KeyLimitFields, keyLimitsBody, keyLimitsFrom, type KeyLimitValues } from "../components/KeyLimits";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge as UiBadge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { HelpTip } from "../components/ui/help-tip";
import { useAction, useCan, useMe, useMut, useOwnKeys, useSecretMut } from "../hooks";
import { useShowSecret } from "../components/SecretProvider";
import { AuthzTemporary, BreakGlassNoKeys, CheckFailed, PageLoading } from "./Access";
import { RotateKeyDialog } from "./KeyDialogs";
import {
  expiryFromDay,
  fmtLifetime,
  lastExpiryDay,
  localDayOf,
  refusalText,
  rotationNote,
  rotationRowNote,
} from "./keyText";

type Team = Me["teams"][number];

/** A self-service page: every team the caller is on, with the caller's own keys in it. */
export function MyKeys() {
  const me = useMe();
  const keys = useOwnKeys();
  if (me.isPending) return <PageLoading label="Loading your access…" />;
  if (me.error && !me.data) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{me.error.message}</AlertDescription>
      </Alert>
    );
  }
  const teams = me.data?.teams ?? [];
  return (
    <>
      <PageHeader
        title="My keys"
        hint="A secret is shown only once, when a key is created or rotated."
      />
      {me.error && me.data && <CheckFailed onRetry={() => void me.refetch()} />}
      {me.data?.authz_unavailable ? (
        <AuthzTemporary onRetry={() => void me.refetch()} />
      ) : me.data?.kind === "break_glass" ? (
        <BreakGlassNoKeys />
      ) : teams.length === 0 ? (
        <NoTeams />
      ) : (
        <>
          {teams.map((team) => (
            <TeamSection
              key={team.team_id}
              team={team}
              keys={(keys.data ?? []).filter((key) => key.team_id === team.team_id)}
              loading={keys.isPending}
              error={keys.error}
              onRetry={() => void keys.refetch()}
            />
          ))}
          {me.data?.user_id && <MyUsage teams={teams} userId={me.data.user_id} />}
        </>
      )}
    </>
  );
}

function NoTeams() {
  return (
    <Card className="items-start gap-2 p-6" data-testid="no-teams">
      <h2 className="m-0">You are not on a team yet</h2>
      <p className="m-0 text-sm text-muted-foreground">
        Ask an administrator to add you to a team. The team, the models you may call and your API keys appear
        here afterwards.
      </p>
    </Card>
  );
}

function Fact({ label, hint, note, children }: { label: string; hint?: string; note?: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex items-center gap-1">
        <span className="text-xs font-medium tracking-[0.04em] text-muted-foreground uppercase">{label}</span>
        {hint && <HelpTip label={label} text={hint} />}
      </div>
      <div className="min-w-0 text-sm">{children}</div>
      {note && <span className="text-xs text-muted-foreground">{note}</span>}
    </div>
  );
}

/** The names this key may call: never the team's full list when the member policy is narrower. */
function CallableModels({ team }: { team: Team }) {
  if (team.all_models) {
    return (
      <span data-testid="all-models">
        All models <HelpTip label="All models" text="Includes names added later." />
      </span>
    );
  }
  if (team.models.length === 0) return <span className="text-muted-foreground">No models</span>;
  const typeOf = new Map((team.typed_models ?? []).map((m) => [m.name, m.model_type]));
  return (
    <div
      className="flex max-h-28 flex-wrap gap-1 overflow-y-auto pr-1"
      data-testid="callable-models"
      role="list"
      aria-label="Callable models"
    >
      {team.models.map((name) => (
        <span key={name} className="inline-flex items-center gap-1" role="listitem">
          <UiBadge variant="muted">{name}</UiBadge>
          {typeOf.get(name) && <TypeBadge type={typeOf.get(name)} />}
        </span>
      ))}
    </div>
  );
}

/** The MCP servers this key may reach, and the tools it is held to on each. */
function McpAccess({ team }: { team: Team }) {
  if (team.all_mcp) {
    return (
      <span data-testid="all-mcp">
        All servers <HelpTip label="All servers" text="Includes servers added later." />
      </span>
    );
  }
  if (team.mcp_servers.length === 0) return <span className="text-muted-foreground">No servers</span>;
  return (
    <div className="flex max-h-28 flex-col gap-1 overflow-y-auto pr-1" data-testid="mcp-servers" role="list" aria-label="MCP servers">
      {team.mcp_servers.map((grant) => (
        <span key={grant.server} role="listitem" className="[overflow-wrap:anywhere]">
          <UiBadge variant="muted">{grant.server}</UiBadge>{" "}
          <span className="text-xs text-muted-foreground">
            {grant.all_tools ? "all tools" : grant.tools.length ? grant.tools.join(", ") : "no tools"}
          </span>
        </span>
      ))}
    </div>
  );
}

/** The A2A agents this key may call: an agent is granted whole, so a name is the whole answer. */
function AgentAccess({ team }: { team: Team }) {
  if (team.all_agents) {
    return (
      <span data-testid="all-agents">
        All agents <HelpTip label="All agents" text="Includes agents added later." />
      </span>
    );
  }
  if (team.agents.length === 0) return <span className="text-muted-foreground">No agents</span>;
  return (
    <div className="flex max-h-28 flex-wrap gap-1 overflow-y-auto pr-1" data-testid="callable-agents" role="list" aria-label="A2A agents">
      {team.agents.map((name) => (
        <UiBadge key={name} variant="muted" role="listitem">
          {name}
        </UiBadge>
      ))}
    </div>
  );
}

/**
 * How a key of this team reaches the MCP and A2A endpoints: the paths, the header they take, and one server or
 * agent as an example. The key itself is a placeholder — this page never holds a secret.
 */
function connectionSnippet(team: Team): string {
  const servers = team.all_mcp ? ["<server>"] : team.mcp_servers.map((grant) => grant.server);
  const agents = team.all_agents ? ["<agent>"] : team.agents;
  const more = (count: number, what: string) => (count > 2 ? [`# and ${count - 2} more ${what}`, ""] : []);
  return [
    "# exactly one on every MCP and A2A request",
    "X-Lumewarp-Key: lw-YOUR-KEY",
    "",
    "# MCP: every server this key may use, then one server",
    "POST <gateway>/mcp",
    ...servers.slice(0, 2).map((name) => `POST <gateway>/mcp/servers/${name}`),
    ...more(servers.length, "servers"),
    "",
    "# A2A: the agents this key may use, then one agent",
    "GET  <gateway>/a2a",
    ...agents.slice(0, 2).map((name) => `POST <gateway>/a2a/${name}`),
    ...more(agents.length, "agents"),
  ]
    .join("\n")
    .trimEnd();
}

function Connection({ team }: { team: Team }) {
  const [copied, setCopied] = useState(false);
  const snippet = connectionSnippet(team);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
    } catch {
      /* clipboard blocked: the block is selectable */
    }
  };
  return (
    <section className="flex flex-col gap-2" aria-label={`MCP and A2A connection for ${team.team_name}`}>
      <div className="flex items-center gap-1 text-sm font-semibold">
        MCP and A2A
        <HelpTip label="MCP and A2A" text="Prefix the paths with the gateway's public URL; a key sees only what its grants allow." />
      </div>
      <div className="flex items-stretch gap-2">
        <code
          className="min-w-0 flex-1 overflow-x-auto rounded-md border bg-secondary px-2.5 py-2 font-mono text-xs whitespace-pre"
          data-testid="connection-snippet"
          tabIndex={0}
        >
          {snippet}
        </code>
        <Button type="button" variant="outline" className="self-start" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </section>
  );
}

function rateLimits(team: Team) {
  const parts = [
    team.limits.rpm_limit !== null ? `${team.limits.rpm_limit} requests/min` : null,
    team.limits.tpm_limit !== null ? `${team.limits.tpm_limit.toLocaleString()} tokens/min` : null,
    team.limits.max_parallel !== null ? `${team.limits.max_parallel} in parallel` : null,
  ].filter(Boolean);
  return parts.length ? <span>{parts.join(" · ")}</span> : <span className="text-muted-foreground">No limits</span>;
}

/**
 * `remaining_nano_usd` is the minimum across the team and member budgets that are set. A single
 * denominator would name one scope as if it were the binding one, so only the remaining amount is
 * shown; the fact's hint names both scopes.
 */
function remainingBudget(team: Team) {
  const budget = team.budget;
  if (budget.remaining_nano_usd === null) return <span className="text-muted-foreground">No budget</span>;
  return (
    <span>
      <strong>{fmtUsd(budget.remaining_nano_usd)}</strong> remaining
    </span>
  );
}

function TeamSection({
  team,
  keys,
  loading,
  error,
  onRetry,
}: {
  team: Team;
  keys: Key[];
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
}) {
  const perms = useCan();
  const showSecret = useShowSecret();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Key | null>(null);
  const [rotating, setRotating] = useState<Key | null>(null);
  const [revoking, setRevoking] = useState<Key | null>(null);
  const setLifecycle = useAction(
    ({ key, lifecycle }: { key: Key; lifecycle: "active" | "suspended" }) =>
      api.updateKey(key.id, { lifecycle }),
    "Key updated",
  );
  const revoke = useMut((key: Key) => api.revokeKey(key.id), "Key revoked");
  const limits = team.key_limits;
  const lifetime = limits.max_member_key_lifetime_seconds;
  const atCap = limits.max_keys_per_member !== null && limits.key_count >= limits.max_keys_per_member;
  return (
    <Card className="mb-5 gap-4 p-4 shadow-none" data-testid={`team-${team.team_name}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h2 className="m-0 flex flex-wrap items-center gap-2">
          {team.team_name}
          <Badge tone="info">{team.role}</Badge>
        </h2>
        <Button onClick={() => setCreating(true)}>New key</Button>
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3">
        <Fact label="Callable models">
          <CallableModels team={team} />
        </Fact>
        <Fact label="MCP servers" hint="A server with a tool list allows only those tools.">
          <McpAccess team={team} />
        </Fact>
        <Fact label="A2A agents">
          <AgentAccess team={team} />
        </Fact>
        <Fact label="Rate limits" hint="Counted per gateway instance.">
          {rateLimits(team)}
        </Fact>
        <Fact label="Remaining budget" hint="The remaining amount is capped by org, team and personal budgets for the current month.">
          {remainingBudget(team)}
        </Fact>
        <Fact
          label="Key limit"
          hint="Revoked keys stay in the list but do not count toward the cap."
          note={`Longest lifetime: ${fmtLifetime(lifetime)}`}
        >
          {limits.max_keys_per_member === null
            ? `${limits.key_count} non-revoked ${limits.key_count === 1 ? "key" : "keys"}, no cap`
            : `${limits.key_count} of ${limits.max_keys_per_member} non-revoked keys`}
        </Fact>
      </div>
      {atCap && (
        <p className="m-0 text-sm text-muted-foreground">
          You hold this team's maximum of {limits.max_keys_per_member} non-revoked keys. Revoke one to make room;
          until then the server refuses a new key.
        </p>
      )}
      <Connection team={team} />
      <DataTable
        rows={keys}
        loading={loading}
        error={error}
        onRetry={onRetry}
        rowKey={(key) => key.id}
        empty="No keys in this team yet."
        pageSize={5}
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (key) => (
              <>
                <strong className="[overflow-wrap:anywhere]">{key.name}</strong>{" "}
                <span className="block text-xs text-muted-foreground sm:ml-1 sm:inline sm:text-sm">
                  <code className="[overflow-wrap:anywhere]">{key.display_prefix}…</code>
                  <span className="sm:hidden" data-testid="phone-expiry"> · expires {fmtDate(key.expires_at)}</span>
                </span>
              </>
            ),
          },
          {
            key: "status",
            header: "Status",
            cell: (key) => {
              const note = rotationRowNote(key, perms.canResumeKey(key));
              return (
                <span className="flex flex-col gap-0.5 [overflow-wrap:anywhere]">
                  <span>
                    <KeyBadge k={key} />
                    <SuspendedReason reason={key.suspended_reason} />
                  </span>
                  {note && (
                    <span className="text-xs text-muted-foreground" data-testid={note.startsWith("Old secret") ? "grace" : "rotation-note"}>
                      {note}
                    </span>
                  )}
                </span>
              );
            },
          },
          { key: "expires", header: "Expires", hideBelow: "sm", cell: (key) => fmtDate(key.expires_at) },
          {
            key: "actions",
            header: "",
            cell: (key) => {
              if (key.lifecycle === "revoked") return <span className="text-muted-foreground">—</span>;
              const canManage = perms.canManageKey(key);
              const ownerInactive = key.owner_kind === "user" && key.owner_active === false;
              const suspended = key.lifecycle === "suspended";
              const canResume = suspended && perms.canResumeKey(key);
              return (
                <div className="flex flex-col items-end gap-1 whitespace-nowrap sm:flex-row sm:justify-end sm:gap-1.5">
                  <Button variant="outline" size="sm" disabled={!canManage} onClick={() => setEditing(key)}>
                    Edit
                  </Button>
                  <Button variant="outline" size="sm" disabled={!canManage} onClick={() => setRotating(key)}>
                    Rotate
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={suspended ? !canResume : !canManage}
                    title={suspended && ownerInactive ? "Cannot resume because the owner is inactive" : undefined}
                    onClick={() => void setLifecycle({ key, lifecycle: suspended ? "active" : "suspended" })}
                  >
                    {suspended ? "Resume" : "Suspend"}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    disabled={!canManage}
                    onClick={() => setRevoking(key)}
                  >
                    Revoke
                  </Button>
                </div>
              );
            },
          },
        ]}
      />
      <CreateKeyDialog
        team={team}
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(value) => showSecret({ value, title: "API key created" })}
      />
      {editing && (
        <EditKeyDialog key={editing.id} team={team} target={editing} onClose={() => setEditing(null)} />
      )}
      <RotateKeyDialog
        target={rotating}
        onClose={() => setRotating(null)}
        onRotated={(value, key) => {
          setRotating(null);
          showSecret({ value, title: "API key rotated", note: rotationNote(key, perms.canResumeKey(key)) });
        }}
      />
      <ConfirmDialog
        open={!!revoking}
        title="Revoke key"
        confirmLabel="Revoke"
        message={`Revoke “${revoking?.name}” (${revoking?.display_prefix}…)? Requests using it stop working immediately. This cannot be undone.`}
        onConfirm={() => revoke.mutateAsync(revoking!)}
        onClose={() => setRevoking(null)}
      />
    </Card>
  );
}

type CreateValues = KeyLimitValues & { name: string; expires: string };
const blankCreate: CreateValues = { name: "", expires: "", ...blankKeyLimits() };

function CreateKeyDialog({
  team,
  open,
  onClose,
  onCreated,
}: {
  team: Team;
  open: boolean;
  onClose: () => void;
  onCreated: (secret: string) => void;
}) {
  const form = useForm<CreateValues>(blankCreate, { name: required("Name") });
  const create = useSecretMut(
    (values: CreateValues) =>
      api.createKey(team.team_id, {
        name: values.name.trim(),
        expires_at: expiryFromDay(values.expires),
        ...keyLimitsBody(values),
      }),
    (secret) => onCreated(secret),
  );
  const lifetime = team.key_limits.max_member_key_lifetime_seconds;
  const max = lifetime === null ? undefined : lastExpiryDay(lifetime);
  const close = () => {
    form.reset(blankCreate);
    onClose();
  };
  const submit = async () => {
    if (!form.validate()) return false;
    try {
      return await form.run(() => create.mutateAsync(form.values), { expires_at: "expires" });
    } catch (e) {
      // A 409 (the cap) or a 429 (with its wait) is the server's own message, kept in the dialog.
      throw new Error(refusalText(e));
    }
  };
  return (
    <FormDialog
      open={open}
      title={`New key in ${team.team_name}`}
      submitLabel="Create key"
      onClose={close}
      onSubmit={submit}
    >
      <TextField
        label="Name"
        value={form.values.name}
        onChange={(value) => form.set("name", value)}
        error={form.errors.name}
        autoFocus
      />
      <TextField
        label="Expires"
        type="date"
        value={form.values.expires}
        max={max}
        onChange={(value) => form.set("expires", value)}
        error={form.errors.expires}
        note={
          lifetime === null
            ? "Empty means the key never expires."
            : `Optional. This team limits member keys to ${fmtLifetime(lifetime)}; empty uses that limit. The latest day is ${max}.`
        }
      />
      <KeyLimitFields values={form.values} errors={form.errors} set={form.set} cap={team.limits} />
      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-semibold">This key can call</span>
        <CallableModels team={team} />
      </div>
    </FormDialog>
  );
}

type EditValues = KeyLimitValues & { name: string; expires: string };

function EditKeyDialog({ team, target, onClose }: { team: Team; target: Key; onClose: () => void }) {
  const initialExpiry = localDayOf(target.expires_at);
  const initial: EditValues = { name: target.name, expires: initialExpiry, ...keyLimitsFrom(target) };
  const form = useForm<EditValues>(initial, { name: required("Name") });
  const update = useMut((values: EditValues) => {
    const body: Schemas["KeyUpdate"] = { name: values.name.trim(), ...keyLimitsBody(values) };
    if (values.expires !== initialExpiry) {
      body.expires_at = expiryFromDay(values.expires);
    }
    return api.updateKey(target.id, body);
  }, "Key saved");
  const lifetime = team.key_limits.max_member_key_lifetime_seconds;
  const max = lifetime === null ? undefined : lastExpiryDay(lifetime);
  const close = () => {
    form.reset(initial);
    onClose();
  };
  const submit = async () => {
    if (!form.validate()) return false;
    return form.run(() => update.mutateAsync(form.values), { expires_at: "expires" });
  };
  return (
    <FormDialog
      open
      title={`Edit “${target.name}” (${target.display_prefix}…)`}
      submitLabel="Save"
      onClose={close}
      onSubmit={submit}
    >
      <TextField
        label="Name"
        value={form.values.name}
        onChange={(value) => form.set("name", value)}
        error={form.errors.name}
        autoFocus
      />
      <TextField
        label="Expires"
        type="date"
        value={form.values.expires}
        max={max}
        onChange={(value) => form.set("expires", value)}
        error={form.errors.expires}
        note={
          lifetime === null
            ? "Empty means the key never expires."
            : `This team limits member keys to ${fmtLifetime(lifetime)}; the latest day is ${max}.`
        }
      />
      <KeyLimitFields values={form.values} errors={form.errors} set={form.set} cap={team.limits} />
    </FormDialog>
  );
}

function MyUsage({ teams, userId }: { teams: Team[]; userId: string }) {
  const [teamId, setTeamId] = useState(teams[0].team_id);
  const team = teams.find((item) => item.team_id === teamId) ?? teams[0];
  const now = new Date();
  const from = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString();
  const to = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1)).toISOString();
  const usage = useQuery({
    queryKey: ["my-usage", team.team_id, userId, from, to],
    queryFn: () => api.usage({ team_id: team.team_id, user_id: userId, from, to, group_by: "day,requested" }),
  });
  return (
    <section className="mt-6" aria-labelledby="my-usage-title">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="my-usage-title" className="m-0">
            My usage
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">Your requests this month in {team.team_name}.</p>
        </div>
        {teams.length > 1 && (
          <div className="w-full max-w-56">
            <SelectField
              label="Team"
              value={teamId}
              onChange={setTeamId}
              options={teams.map((item) => ({ value: item.team_id, label: item.team_name }))}
            />
          </div>
        )}
      </div>
      <DataTable
        rows={usage.data}
        loading={usage.isPending}
        error={usage.error}
        onRetry={() => void usage.refetch()}
        rowKey={(row) => [row.date, row.requested].join("|")}
        empty="No usage in this month yet."
        pageSize={5}
        columns={[
          { key: "date", header: "Date", cell: (row) => row.date ?? "—" },
          { key: "requested", header: "Requested", cell: (row) => row.requested ?? "—" },
          {
            key: "requests",
            header: "Requests",
            align: "right",
            cell: (row) => row.requests.toLocaleString(),
          },
          {
            key: "tokens",
            header: "Tokens",
            align: "right",
            cell: (row) => row.total_tokens.toLocaleString(),
          },
          { key: "cost", header: "Cost", align: "right", cell: (row) => fmtUsd(row.cost_nano_usd) },
        ]}
      />
    </section>
  );
}
