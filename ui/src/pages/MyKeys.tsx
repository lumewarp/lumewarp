import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { api, type Key, type Me } from "../api";
import { Badge, fmtDate, fmtUsd, KeyBadge, PageHeader, SuspendedReason } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { DataTable } from "../components/Table";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge as UiBadge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { useMe, useMut, useOwnKeys } from "../hooks";
import { PageLoading } from "./Access";
import { RotateKeyDialog, SecretDialog } from "./KeyDialogs";
import { expiryFromDay, fmtLifetime, graceText, inGrace, refusalText, rotationNote } from "./keyText";

type Team = Me["teams"][number];

/** A self-service page: every team the caller is on, with the caller's own keys in it. */
export function MyKeys() {
  const me = useMe();
  const keys = useOwnKeys();
  if (me.isPending) return <PageLoading label="Loading your access…" />;
  if (me.error) {
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
        description="Your teams, the models you can call, and the API keys you own. A secret is shown once, when a key is created or rotated."
      />
      {teams.length === 0 ? (
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

function Fact({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-xs font-medium tracking-[0.04em] text-muted-foreground uppercase">{label}</span>
      <div className="min-w-0 text-sm">{children}</div>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  );
}

/** The names this key may call: never the team's full list when the member policy is narrower. */
function CallableModels({ team }: { team: Team }) {
  if (team.all_models) {
    return (
      <span data-testid="all-models">
        All models <span className="text-muted-foreground">(including names added later)</span>
      </span>
    );
  }
  if (team.models.length === 0) return <span className="text-muted-foreground">No models</span>;
  return (
    <div
      className="flex max-h-28 flex-wrap gap-1 overflow-y-auto pr-1"
      data-testid="callable-models"
      role="list"
      aria-label="Callable models"
    >
      {team.models.map((name) => (
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
  const [creating, setCreating] = useState(false);
  const [rotating, setRotating] = useState<Key | null>(null);
  const [revoking, setRevoking] = useState<Key | null>(null);
  const [secret, setSecret] = useState<{ value: string; title: string; note?: string } | null>(null);
  const revoke = useMut((key: Key) => api.revokeKey(key.id), "Key revoked");
  const limits = team.key_limits;
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
        <Fact label="Rate limits">{rateLimits(team)}</Fact>
        <Fact label="Remaining budget" hint="This month, for the team and your own budget">
          {remainingBudget(team)}
        </Fact>
        <Fact label="Key limit" hint={`Longest lifetime: ${fmtLifetime(limits.max_member_key_lifetime_seconds)}`}>
          {limits.max_keys_per_member === null
            ? `${limits.key_count} ${limits.key_count === 1 ? "key" : "keys"}, no cap`
            : `${limits.key_count} of ${limits.max_keys_per_member} keys`}
        </Fact>
      </div>
      {atCap && (
        <p className="m-0 text-sm text-muted-foreground">
          You hold this team's maximum of {limits.max_keys_per_member} keys. Revoke one to make room; until then the
          server refuses a new key.
        </p>
      )}
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
                <code className="hidden text-muted-foreground sm:inline">{key.display_prefix}…</code>
              </>
            ),
          },
          {
            key: "status",
            header: "Status",
            cell: (key) => (
              <span className="flex flex-col gap-0.5 [overflow-wrap:anywhere]">
                <span>
                  <KeyBadge k={key} />
                  <SuspendedReason reason={key.suspended_reason} />
                </span>
                {inGrace(key) && (
                  <span className="text-xs text-muted-foreground" data-testid="grace">
                    {graceText(key)}
                  </span>
                )}
              </span>
            ),
          },
          { key: "expires", header: "Expires", hideBelow: "sm", cell: (key) => fmtDate(key.expires_at) },
          {
            key: "actions",
            header: "",
            cell: (key) =>
              key.lifecycle === "revoked" ? (
                <span className="text-muted-foreground">—</span>
              ) : (
                <div className="flex flex-col items-end gap-1 whitespace-nowrap sm:flex-row sm:justify-end sm:gap-1.5">
                  <Button variant="outline" size="sm" onClick={() => setRotating(key)}>
                    Rotate
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => setRevoking(key)}
                  >
                    Revoke
                  </Button>
                </div>
              ),
          },
        ]}
      />
      <CreateKeyDialog
        team={team}
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(value) => setSecret({ value, title: "API key created" })}
      />
      <RotateKeyDialog
        target={rotating}
        onClose={() => setRotating(null)}
        onRotated={(created) => {
          setRotating(null);
          setSecret({ value: created.secret, title: "API key rotated", note: rotationNote(created.key) });
        }}
      />
      <SecretDialog
        secret={secret?.value ?? null}
        title={secret?.title ?? ""}
        note={secret?.note}
        onClose={() => setSecret(null)}
      />
      <ConfirmDialog
        open={!!revoking}
        title="Revoke key"
        confirmLabel="Revoke"
        message={`Revoke “${revoking?.name}”? Requests using it stop working immediately. This cannot be undone.`}
        onConfirm={() => revoke.mutateAsync(revoking!)}
        onClose={() => setRevoking(null)}
      />
    </Card>
  );
}

type CreateValues = { name: string; expires: string };
const blankCreate: CreateValues = { name: "", expires: "" };

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
  const create = useMut(
    (values: CreateValues) =>
      api.createKey(team.team_id, { name: values.name.trim(), expires_at: expiryFromDay(values.expires) }),
    (created) => onCreated(created.secret),
  );
  const lifetime = team.key_limits.max_member_key_lifetime_seconds;
  const close = () => {
    form.reset(blankCreate);
    onClose();
  };
  const submit = async () => {
    if (!form.validate()) return false;
    try {
      const result = await form.run(() => create.mutateAsync(form.values), { expires_at: "expires" });
      // The secret is handed to the dialog's own state; do not also keep it in the mutation cache.
      create.reset();
      return result;
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
        onChange={(value) => form.set("expires", value)}
        error={form.errors.expires}
        hint={
          lifetime === null
            ? "Empty means the key never expires."
            : `Optional. This team limits member keys to ${fmtLifetime(lifetime)}; leave empty to use that limit.`
        }
      />
      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-semibold">This key can call</span>
        <CallableModels team={team} />
      </div>
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
