import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Key, type Me, type Team } from "./api";
import { useToast } from "./components/Toast";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 5 * 60_000 });
}

/**
 * What the signed-in principal may do, mirroring the server's authorization matrix so the UI can hide or
 * disable actions. Purely cosmetic: the server enforces every request.
 */
export function can(me: Me | undefined) {
  const isSuper = !!me?.is_admin || me?.kind === "break_glass";
  const teamRole = (teamId: string) => me?.teams.find((t) => t.team_id === teamId)?.role;
  const canManageKey = (key: Key) =>
    isSuper ||
    teamRole(key.team_id) === "admin" ||
    (teamRole(key.team_id) !== undefined && key.owner_kind === "user" && key.user_id === me?.user_id);
  return {
    isSuper,
    teamRole,
    teamRead: (teamId: string) => isSuper || teamRole(teamId) !== undefined,
    teamAdmin: (teamId: string) => isSuper || teamRole(teamId) === "admin",
    teamMember: (teamId: string) => teamRole(teamId) !== undefined,
    canCreateOwnKey: (teamId: string) => teamRole(teamId) !== undefined,
    canManageKey,
    canResumeKey: (key: Key) =>
      canManageKey(key) &&
      (isSuper ||
        teamRole(key.team_id) === "admin" ||
        key.suspended_reason === null ||
        key.suspended_reason === "owner" ||
        // An owner who is back on the team may resume keys suspended by their removal.
        key.suspended_reason === "owner_removed"),
  };
}

export function useCan() {
  const { data } = useMe();
  return can(data);
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

export function useTeams() {
  return useQuery({
    queryKey: ["teams"],
    queryFn: () => api.teams(),
  });
}

export function useAllTeamKeys(teams: Team[] | undefined) {
  return useQuery({
    queryKey: ["team-keys", teams?.map((t) => t.id)],
    enabled: !!teams,
    queryFn: async () => (await Promise.all(teams!.map((t) => api.teamKeys(t.id)))).flat(),
  });
}

export function useTeamDirectory(teams: Team[] | undefined) {
  return useQuery({
    queryKey: ["team-directory", teams?.map((t) => t.id)],
    enabled: !!teams,
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

export function useRoutesQuery() {
  return useQuery({ queryKey: ["routes"], queryFn: () => api.routes(), retry: false });
}
