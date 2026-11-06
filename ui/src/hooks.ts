import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Key, type Me, type Schemas, type TeamAny } from "./api";
import { useMemo } from "react";
import { useToast } from "./components/Toast";
import { runSecretMutation } from "./lib/keySecret";
import { ownerResumableReason } from "./lib/keyState";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 5 * 60_000 });
}

/**
 * What the signed-in principal may do, mirroring the server's authorization matrix so the UI can hide or
 * disable actions. Purely cosmetic: the server enforces every request.
 */
export function can(me: Me | undefined, teams: TeamAny[] | undefined) {
  const isInstallationAdmin = !!me?.is_admin || me?.kind === "break_glass";
  // The server inherits team administration from an org: an org admin administers every team of its
  // orgs without being a member. `org_id` on the team DTO is what maps a team to that check.
  const adminOrgIds = new Set((me?.admin_orgs ?? []).map((org) => org.id));
  // An installation admin administers every org (and a break-glass session has no
  // `admin_orgs` rows), so the org layer is available to it as well.
  const isOrgAdmin = isInstallationAdmin || adminOrgIds.size > 0;
  const orgAdminOf = (orgId: string | undefined) =>
    !!orgId && (isInstallationAdmin || adminOrgIds.has(orgId));
  const teamOrgId = (teamId: string) => teams?.find((team) => team.id === teamId)?.org_id;
  const isOrgTeamAdmin = (teamId: string) => {
    const orgId = teamOrgId(teamId);
    return orgId !== undefined && adminOrgIds.has(orgId);
  };
  const isTeamAdmin = isOrgAdmin || !!me?.teams.some((team) => team.role === "admin");
  const teamRole = (teamId: string) => me?.teams.find((t) => t.team_id === teamId)?.role;
  const teamAdmin = (teamId: string) =>
    isInstallationAdmin || teamRole(teamId) === "admin" || isOrgTeamAdmin(teamId);
  const canManageKey = (key: Key) =>
    teamAdmin(key.team_id) ||
    (teamRole(key.team_id) !== undefined && key.owner_kind === "user" && key.user_id === me?.user_id);
  return {
    isInstallationAdmin,
    isOrgAdmin,
    orgAdminOf,
    canReadOrg: orgAdminOf,
    // Installation level only: create, rename/policy, archive and admin assignment.
    canManageOrg: isInstallationAdmin,
    // Team creation is org level: an org admin may create in its own org, a team admin not.
    canCreateTeam: isOrgAdmin,
    canCreateTeamInOrg: orgAdminOf,
    canDeleteTeam: (teamId: string) => isInstallationAdmin || isOrgTeamAdmin(teamId),
    isTeamAdmin,
    // Names and kinds only; every authenticated caller may read them.
    canReadCatalog: true,
    // Upstreams, models, aliases, routers and strategies: installation level.
    canReadRouting: isInstallationAdmin,
    teamRole,
    teamRead: (teamId: string) => teamAdmin(teamId) || teamRole(teamId) !== undefined,
    teamAdmin,
    teamMember: (teamId: string) => teamRole(teamId) !== undefined,
    // Team quota (models, limits, budget) and admin assignment are org-level: an org admin may set
    // them for its own org's teams, a team admin never.
    canSetTeamQuota: (teamId: string) => isInstallationAdmin || isOrgTeamAdmin(teamId),
    canAssignTeamAdmin: (teamId: string) => isInstallationAdmin || isOrgTeamAdmin(teamId),
    canUpdateTeamSettings: teamAdmin,
    // A key belongs to its owner, and only a direct membership can own one.
    canCreateOwnKey: (teamId: string) => teamRole(teamId) !== undefined,
    canManageKey,
    canResumeKey: (key: Key) =>
      // The API refuses to activate a key whose owner is inactive, even for a team admin.
      key.owner_active !== false &&
      canManageKey(key) &&
      (teamAdmin(key.team_id) || ownerResumableReason(key)),
  };
}

export function useOrgs(enabled = true) {
  return useQuery({ queryKey: ["orgs"], queryFn: () => api.orgs(), enabled, staleTime: 60_000 });
}

/**
 * Whether the UI shows the org layer (org columns, filters and selectors). A single-org
 * installation does not: the Orgs page itself always shows its org, as the plan says.
 */
export function useOrgLayer() {
  const perms = useCan();
  const orgs = useOrgs(perms.isInstallationAdmin || perms.isOrgAdmin);
  return { orgs, show: (orgs.data?.length ?? 0) > 1 };
}

export function useCan() {
  const { data: me } = useMe();
  // The team list supplies each team's org, needed to apply org inheritance locally.
  const { data: teams } = useTeams();
  return can(me, teams);
}

/** What a route or navigation entry requires; purely cosmetic, the API enforces every request. */
export type Access = "installationAdmin" | "orgAdmin" | "teamAdmin" | "member";

export function hasAccess(
  perms: Pick<ReturnType<typeof can>, "isInstallationAdmin" | "isOrgAdmin" | "isTeamAdmin">,
  need: Access,
) {
  if (need === "member") return true;
  if (need === "installationAdmin") return perms.isInstallationAdmin;
  if (need === "orgAdmin") return perms.isInstallationAdmin || perms.isOrgAdmin;
  return perms.isInstallationAdmin || perms.isTeamAdmin;
}

/** Every mutation refreshes all cached lists: what one page changes (names, policies) shows up in others. */
export function useMut<A, R>(fn: (arg: A) => Promise<R>, success?: string | ((r: R) => void)) {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: fn,
    onSuccess: (r) => {
      void qc.invalidateQueries();
      if (typeof success === "string") toast("success", success);
      else success?.(r);
    },
  });
}

/** For actions that are not in a form dialog: toast the failure. */
export function useAction<A, R>(
  fn: (arg: A) => Promise<R>,
  success: string | ((r: R) => void),
) {
  const toast = useToast();
  const m = useMut(fn, success);
  return (arg: A) => m.mutateAsync(arg).catch((e: Error) => toast("error", e.message));
}

/**
 * Create or rotate a key without keeping the secret in the mutation cache. `onSecret` receives it once;
 * the cached mutation result is the key.
 */
export function useSecretMut<A>(
  fn: (arg: A) => Promise<Schemas["KeyCreated"]>,
  onSecret: (secret: string, key: Key) => void,
  success?: string,
) {
  return useMut((arg: A) => runSecretMutation(() => fn(arg), onSecret), success);
}

export function useTeams() {
  return useQuery({
    queryKey: ["teams"],
    queryFn: () => api.teams(),
  });
}

export function useAllTeamKeys(teams: TeamAny[] | undefined) {
  return useQuery({
    queryKey: ["team-keys", teams?.map((t) => t.id)],
    enabled: !!teams,
    queryFn: async () => (await Promise.all(teams!.map((t) => api.teamKeys(t.id)))).flat(),
  });
}

export function useTeamDirectory(teams: TeamAny[] | undefined, enabled = true) {
  return useQuery({
    queryKey: ["team-directory", teams?.map((t) => t.id)],
    // Members and service accounts are the team-admin view; a plain member must not
    // request them (they answer 403).
    enabled: !!teams && enabled,
    queryFn: async () => {
      const directory = await Promise.all(
        teams!.map(async (team) => ({
          members: await api.members(team.id),
          serviceAccounts: await api.serviceAccounts(team.id),
        })),
      );
      return {
        members: directory.flatMap((team) => team.members),
        serviceAccounts: directory.flatMap((team) => team.serviceAccounts),
      };
    },
  });
}

/** id → name lookup for rendering foreign keys. */
export function useNames(rows: { id: string; name: string }[] | undefined) {
  const map = new Map((rows ?? []).map((r) => [r.id, r.name]));
  return (id: string | null | undefined) => (id ? (map.get(id) ?? id.slice(0, 8)) : "—");
}

export const useOwnKeys = () => useQuery({ queryKey: ["own-keys"], queryFn: () => api.ownKeys() });

export const useModels = (enabled = true) =>
  useQuery({ queryKey: ["models"], queryFn: () => api.models(), enabled });
export const useAliases = () => useQuery({ queryKey: ["aliases"], queryFn: () => api.aliases() });
// Settings and schemas may hold a `__proto__` key; structural sharing would rebuild them by assignment and lose it.
export const useRouters = () => useQuery({ queryKey: ["routers"], queryFn: () => api.routers(), structuralSharing: false });
export const useUpstreams = () => useQuery({ queryKey: ["upstreams"], queryFn: () => api.upstreams() });
export const useStrategies = () => useQuery({ queryKey: ["strategies"], queryFn: () => api.strategies(), structuralSharing: false });
export const useCatalog = (enabled = true) => useQuery({ queryKey: ["catalog"], queryFn: () => api.catalog(), enabled });

/** id → row lookup. */
export function useById<T extends { id: string }>(rows: T[] | undefined) {
  return useMemo(() => new Map((rows ?? []).map((r) => [r.id, r])), [rows]);
}
