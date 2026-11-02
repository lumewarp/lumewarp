import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { api, type Key } from "../api";
import { fmtDate, KeyBadge, PageHeader, SuspendedReason } from "../components/Bits";
import { ConfirmDialog } from "../components/Confirm";
import { DataTable } from "../components/Table";
import { Button } from "../components/ui/button";
import { useAllTeamKeys, useCan, useMut, useTeamDirectory, useTeams } from "../hooks";
import { RotateKeyDialog, SecretDialog } from "./KeyDialogs";
import { rotationNote } from "./keyText";

/** The admin view: every key in the teams the caller administers. Members use My keys for their own. */
export function Keys() {
  const perms = useCan();
  const teams = useTeams();
  const adminTeams = (teams.data ?? []).filter((team) => perms.teamAdmin(team.id));
  const keys = useAllTeamKeys(teams.data ? adminTeams : undefined);
  const directory = useTeamDirectory(teams.data ? adminTeams : undefined);
  const [rotating, setRotating] = useState<Key | null>(null);
  const [revoking, setRevoking] = useState<Key | null>(null);
  const [secret, setSecret] = useState<{ value: string; title: string; note?: string } | null>(null);
  const revoke = useMut((key: Key) => api.revokeKey(key.id), "Key revoked");
  const teamName = (id: string) => teams.data?.find((team) => team.id === id)?.name ?? id.slice(0, 8);
  const ownerName = (key: Key) => {
    if (key.owner_kind === "service_account") {
      return (
        directory.data?.serviceAccounts.find((account) => account.id === key.service_account_id)?.name ??
        key.service_account_id?.slice(0, 8) ??
        "—"
      );
    }
    return (
      directory.data?.members.find((member) => member.user_id === key.user_id)?.email ??
      key.user_id?.slice(0, 8) ??
      "—"
    );
  };

  return (
    <>
      <PageHeader
        title="Keys"
        description="Keys in the teams you administer, service accounts included. Members manage their own keys on My keys; a team's policies live on the team page."
      />
      <DataTable
        rows={keys.data}
        loading={keys.isPending || teams.isPending || directory.isPending}
        error={keys.error ?? teams.error ?? directory.error}
        onRetry={() => {
          void keys.refetch();
          void teams.refetch();
          void directory.refetch();
        }}
        rowKey={(key) => key.id}
        empty="No keys in the teams you administer."
        columns={[
          {
            key: "name",
            header: "Name",
            sort: (a, b) => a.name.localeCompare(b.name),
            cell: (key) => (
              <>
                <strong>{key.name}</strong> <code className="text-muted-foreground">{key.display_prefix}…</code>
              </>
            ),
          },
          {
            key: "team",
            header: "Team",
            cell: (key) => (
              <Link to="/teams/$teamId" params={{ teamId: key.team_id }} className="[overflow-wrap:anywhere]">
                {teamName(key.team_id)}
              </Link>
            ),
          },
          {
            key: "owner",
            header: "Owner",
            hideBelow: "md",
            cell: (key) => (
              <>
                {key.owner_kind === "service_account" ? "Service account: " : ""}
                {ownerName(key)}
              </>
            ),
          },
          {
            key: "status",
            header: "Status",
            cell: (key) => (
              <>
                <KeyBadge k={key} />
                <SuspendedReason reason={key.suspended_reason} />
              </>
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
    </>
  );
}
