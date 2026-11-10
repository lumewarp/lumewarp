import { useQuery } from "@tanstack/react-query";
import { BanIcon, BotIcon, BoxesIcon, CableIcon, ChevronDownIcon, ChevronRightIcon, PauseIcon, PencilIcon, PlayIcon, PlusIcon, RefreshCwIcon } from "lucide-react";
import { useState, type ReactNode } from "react";

import { api, type Key, type Me, type Schemas } from "../api";
import { Badge, fmtDate, fmtUsd, InheritedBadge, KeyBadge, ModelPill, PageHeader, SuspendedReason } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { IconAction } from "../components/RowActions";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { blankKeyLimits, KeyLimitFields, keyLimitsBody, keyLimitsFrom, type KeyLimitValues } from "../components/KeyLimits";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge as UiBadge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "../components/ui/tooltip";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
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

/**
 * A self-service page: the caller's own keys in one table, then every team they may hold keys in
 * (memberships first, then teams administered by inheritance) with what a key there can do.
 */
export function MyKeys() {
  const me = useMe();
  const keys = useOwnKeys();
  const [creating, setCreating] = useState<{ team: Team | null } | null>(null);
  const showSecret = useShowSecret();
  if (me.isPending) return <PageLoading label="Loading your access…" />;
  if (me.error && !me.data) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{me.error.message}</AlertDescription>
      </Alert>
    );
  }
  const teams = orderTeams(me.data?.teams ?? []);
  const blocked = !!me.data?.authz_unavailable || me.data?.kind === "break_glass" || teams.length === 0;
  return (
    <>
      <PageHeader
        title="My keys"
        hint="A secret is shown only once, when a key is created or rotated."
        actions={blocked ? undefined : <Button onClick={() => setCreating({ team: teams.length === 1 ? teams[0] : null })}>New key</Button>}
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
          <KeysTable
            teams={teams}
            keys={keys.data}
            loading={keys.isPending}
            error={keys.error}
            onRetry={() => void keys.refetch()}
          />
          <TeamsTable teams={teams} onNewKey={(team) => setCreating({ team })} />
          {me.data?.user_id && <MyUsage teams={teams} userId={me.data.user_id} />}
          <CreateKeyDialog
            teams={teams}
            team={creating?.team ?? null}
            open={!!creating}
            onClose={() => setCreating(null)}
            onCreated={(value) => showSecret({ value, title: "API key created" })}
          />
        </>
      )}
    </>
  );
}

/** Memberships first, then teams held by inheritance; each group by name. */
function orderTeams(teams: Team[]): Team[] {
  return [...teams].sort((a, b) => Number(!a.direct) - Number(!b.direct) || a.team_name.localeCompare(b.team_name));
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
        <span key={name} role="listitem">
          <ModelPill name={name} type={typeOf.get(name)} />
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

function keyLimitText(team: Team) {
  const limits = team.key_limits;
  return limits.max_keys_per_member === null
    ? `${limits.key_count} non-revoked ${limits.key_count === 1 ? "key" : "keys"}, no cap`
    : `${limits.key_count} of ${limits.max_keys_per_member} non-revoked keys`;
}

const NONE = <span className="text-muted-foreground">—</span>;

/** Table cells: the short form; the details row spells each one out. */
function compactLimits(team: Team) {
  const parts = [
    team.limits.rpm_limit !== null ? `${team.limits.rpm_limit} rpm` : null,
    team.limits.tpm_limit !== null ? `${team.limits.tpm_limit.toLocaleString()} tpm` : null,
    team.limits.max_parallel !== null ? `${team.limits.max_parallel} parallel` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : NONE;
}

function compactBudget(team: Team) {
  return team.budget.remaining_nano_usd === null ? NONE : fmtUsd(team.budget.remaining_nano_usd);
}

function compactKeys(team: Team) {
  const limits = team.key_limits;
  return `${limits.key_count} / ${limits.max_keys_per_member ?? "∞"}`;
}

/** Every key the caller owns, across their teams, in one table. */
function KeysTable({
  teams,
  keys,
  loading,
  error,
  onRetry,
}: {
  teams: Team[];
  keys: Key[] | undefined;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
}) {
  const perms = useCan();
  const showSecret = useShowSecret();
  const [teamFilter, setTeamFilter] = useState("");
  const [editing, setEditing] = useState<Key | null>(null);
  const [rotating, setRotating] = useState<Key | null>(null);
  const [revoking, setRevoking] = useState<Key | null>(null);
  const setLifecycle = useAction(
    ({ key, lifecycle }: { key: Key; lifecycle: "active" | "suspended" }) =>
      api.updateKey(key.id, { lifecycle }),
    "Key updated",
  );
  const revoke = useMut((key: Key) => api.revokeKey(key.id), "Key revoked");
  const byId = new Map(teams.map((team) => [team.team_id, team]));
  const multiple = teams.length > 1;
  // Keys of a team the caller can no longer see (archived, or access lost) are not listed.
  // Newest first, so a key just made or rotated is on the first page.
  const own = keys?.filter((key) => byId.has(key.team_id) && (!teamFilter || key.team_id === teamFilter)).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const withKeys = teams.filter((team) => keys?.some((key) => key.team_id === team.team_id));
  const editingTeam = editing ? byId.get(editing.team_id) : undefined;
  return (
    <section className="mb-6" aria-label="Keys" data-testid="my-keys">
      {multiple && withKeys.length > 1 && (
        <div className="mb-3 w-full max-w-56">
          <SelectField
            label="Team"
            value={teamFilter}
            onChange={setTeamFilter}
            options={[{ value: "", label: "All teams" }, ...withKeys.map((team) => ({ value: team.team_id, label: team.team_name }))]}
          />
        </div>
      )}
      <DataTable
        rows={own}
        loading={loading}
        error={error}
        onRetry={onRetry}
        rowKey={(key) => key.id}
        empty="No keys yet."
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
          ...(multiple
            ? [
                {
                  key: "team",
                  header: "Team",
                  sort: (a: Key, b: Key) => (byId.get(a.team_id)?.team_name ?? "").localeCompare(byId.get(b.team_id)?.team_name ?? ""),
                  cell: (key: Key) => <span className="[overflow-wrap:anywhere]">{byId.get(key.team_id)?.team_name}</span>,
                },
              ]
            : []),
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
                <div className="flex justify-end gap-0.5 whitespace-nowrap">
                  <IconAction label="Edit" icon={PencilIcon} disabled={!canManage} onClick={() => setEditing(key)} />
                  <IconAction label="Rotate" icon={RefreshCwIcon} disabled={!canManage} onClick={() => setRotating(key)} />
                  <IconAction
                    label={suspended ? "Resume" : "Suspend"}
                    icon={suspended ? PlayIcon : PauseIcon}
                    disabled={suspended ? !canResume : !canManage}
                    reason={suspended && ownerInactive ? "Cannot resume because the owner is inactive" : undefined}
                    onClick={() => void setLifecycle({ key, lifecycle: suspended ? "active" : "suspended" })}
                  />
                  <IconAction
                    label="Revoke"
                    icon={BanIcon}
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    disabled={!canManage}
                    onClick={() => setRevoking(key)}
                  />
                </div>
              );
            },
          },
        ]}
      />
      {editing && editingTeam && (
        <EditKeyDialog key={editing.id} team={editingTeam} target={editing} onClose={() => setEditing(null)} />
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
    </section>
  );
}

/** How many models, MCP servers and A2A agents a key in this team reaches, one colored pill each. */
function AccessPills({ team }: { team: Team }) {
  const pills: [string, typeof BoxesIcon, boolean, number, "info" | "ok" | "warn"][] = [
    ["Models", BoxesIcon, team.all_models, team.models.length, "info"],
    ["MCP servers", CableIcon, team.all_mcp, team.mcp_servers.length, "ok"],
    ["A2A agents", BotIcon, team.all_agents, team.agents.length, "warn"],
  ];
  return (
    <span className="flex flex-wrap gap-1.5">
      {pills.map(([label, Icon, all, count, tone]) => {
        const text = all ? "all" : String(count);
        return (
          <Tooltip key={label}>
            <TooltipTrigger asChild>
              <UiBadge variant={!all && count === 0 ? "muted" : tone} aria-label={`${label}: ${text}`} tabIndex={0}>
                <Icon aria-hidden="true" />
                {all ? "All" : count}
              </UiBadge>
            </TooltipTrigger>
            <TooltipContent>{`${label}: ${text}`}</TooltipContent>
          </Tooltip>
        );
      })}
    </span>
  );
}

/**
 * One row per team with the facts most often needed; a row opens to the full version of each fact
 * under its own column. A lone team starts open.
 */
function TeamsTable({ teams, onNewKey }: { teams: Team[]; onNewKey: (team: Team) => void }) {
  const [open, setOpen] = useState<Set<string>>(() => new Set(teams.length === 1 ? [teams[0].team_id] : []));
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const shown = needle ? teams.filter((team) => team.team_name.toLowerCase().includes(needle)) : teams;
  const toggle = (id: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  const wide = "max-md:hidden";
  return (
    <section className="mb-6" aria-labelledby="my-teams-title">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
        <h2 id="my-teams-title" className="m-0 flex items-center gap-1">
          Teams
          <HelpTip label="Teams" text="An inherited team is one you administer from its org or the installation, without a membership." />
        </h2>
        {teams.length > 5 && (
          <div className="w-full max-w-64">
            <TextField label="Search teams" value={query} onChange={setQuery} />
          </div>
        )}
      </div>
      <Card className="gap-0 overflow-hidden p-0 shadow-none">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-10 pr-0" />
              <TableHead>Team</TableHead>
              <TableHead className={wide}>Access</TableHead>
              <TableHead className={wide}>Limits</TableHead>
              <TableHead className={wide}>Budget left</TableHead>
              <TableHead className={wide}>Keys</TableHead>
              <TableHead className="w-10" />
            </TableRow>
          </TableHeader>
          {shown.map((team) => {
            const expanded = open.has(team.team_id);
            const limits = team.key_limits;
            const atCap = limits.max_keys_per_member !== null && limits.key_count >= limits.max_keys_per_member;
            return (
              // One body per team, so a team's row and its details stay together.
              <TableBody key={team.team_id} data-testid={`team-${team.team_name}`}>
                <TableRow>
                  <TableCell className="w-10 pr-0">
                    <IconAction
                      label={expanded ? "Hide details" : "Details"}
                      icon={expanded ? ChevronDownIcon : ChevronRightIcon}
                      onClick={() => toggle(team.team_id)}
                    />
                  </TableCell>
                  <TableCell>
                    <span className="flex flex-wrap items-center gap-1.5">
                      <strong className="[overflow-wrap:anywhere]">{team.team_name}</strong>
                      <Badge tone="info">{team.role}</Badge>
                      <InheritedBadge from={team.inherited_from} />
                    </span>
                  </TableCell>
                  <TableCell className={wide}>
                    <AccessPills team={team} />
                  </TableCell>
                  <TableCell className={wide}>{compactLimits(team)}</TableCell>
                  <TableCell className={wide}>{compactBudget(team)}</TableCell>
                  <TableCell className={wide}>{compactKeys(team)}</TableCell>
                  <TableCell className="w-10 text-right">
                    <IconAction label="New key" icon={PlusIcon} onClick={() => onNewKey(team)} />
                  </TableCell>
                </TableRow>
                {expanded && (
                  <TableRow className="bg-muted/40 hover:bg-muted/40">
                    {/* A shaded band under the row, starting where the team name does. */}
                    <TableCell colSpan={7} className="py-5 pr-6 pl-[3.25rem] whitespace-normal">
                      <TeamDetails team={team} atCap={atCap} />
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            );
          })}
        </Table>
        {shown.length === 0 && <p className="m-0 p-4 text-sm text-muted-foreground">No team matches “{query.trim()}”.</p>}
      </Card>
    </section>
  );
}

/** The full version of each fact, in one row of six under the team (wrapping on narrow screens). */
function TeamDetails({ team, atCap }: { team: Team; atCap: boolean }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-4">
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
      <KeyLimitFact team={team} atCap={atCap} />
    </div>
  );
}

function KeyLimitFact({ team, atCap }: { team: Team; atCap: boolean }) {
  const limits = team.key_limits;
  return (
    <div className="flex flex-col gap-2">
      <Fact
        label="Key limit"
        hint="Revoked keys stay in the list but do not count toward the cap."
        note={`Longest lifetime: ${fmtLifetime(limits.max_member_key_lifetime_seconds)}`}
      >
        {keyLimitText(team)}
      </Fact>
      {atCap && (
        <p className="m-0 text-xs text-muted-foreground">
          At this team's maximum of {limits.max_keys_per_member}; revoke a key to make room.
        </p>
      )}
    </div>
  );
}

type CreateValues = KeyLimitValues & { name: string; expires: string };
const blankCreate: CreateValues = { name: "", expires: "", ...blankKeyLimits() };

function CreateKeyDialog({
  teams,
  team: given,
  open,
  onClose,
  onCreated,
}: {
  teams: Team[];
  /** The team to create in; null asks for one. */
  team: Team | null;
  open: boolean;
  onClose: () => void;
  onCreated: (secret: string) => void;
}) {
  const form = useForm<CreateValues>(blankCreate, { name: required("Name") });
  const [picked, setPicked] = useState("");
  const [teamError, setTeamError] = useState<string>();
  const team = given ?? teams.find((item) => item.team_id === picked) ?? null;
  const create = useSecretMut(
    (values: CreateValues) =>
      api.createKey(team!.team_id, {
        name: values.name.trim(),
        expires_at: expiryFromDay(values.expires),
        ...keyLimitsBody(values),
      }),
    (secret) => onCreated(secret),
  );
  const lifetime = team?.key_limits.max_member_key_lifetime_seconds ?? null;
  const max = lifetime === null ? undefined : lastExpiryDay(lifetime);
  const close = () => {
    form.reset(blankCreate);
    setPicked("");
    setTeamError(undefined);
    onClose();
  };
  const submit = async () => {
    const valid = form.validate();
    if (!team) setTeamError("Choose a team");
    if (!team || !valid) return false;
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
      title={given ? `New key in ${given.team_name}` : "New key"}
      submitLabel="Create key"
      onClose={close}
      onSubmit={submit}
    >
      {!given && (
        <SelectField
          label="Team"
          value={picked}
          onChange={(value) => {
            setPicked(value);
            setTeamError(undefined);
          }}
          error={teamError}
          note={team ? keyLimitText(team) : undefined}
          options={[{ value: "", label: "Choose a team" }, ...teams.map((item) => ({ value: item.team_id, label: item.inherited_from && !item.direct ? `${item.team_name} (inherit)` : item.team_name }))]}
        />
      )}
      <TextField
        label="Name"
        value={form.values.name}
        onChange={(value) => form.set("name", value)}
        error={form.errors.name}
        autoFocus={!!given}
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
      {team && <KeyLimitFields values={form.values} errors={form.errors} set={form.set} cap={team.limits} />}
      {team && (
        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-semibold">This key can call</span>
          <CallableModels team={team} />
        </div>
      )}
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
