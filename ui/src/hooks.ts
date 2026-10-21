import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Me, type Org, type Project, type Team } from "./api";
import { useToast } from "./components/Toast";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 5 * 60_000 });
}

/**
 * What the signed-in principal may do, mirroring the server's RBAC matrix so the UI can hide or
 * disable actions. Purely cosmetic: the server enforces every request.
 */
export function can(me: Me | undefined) {
  const grants = me?.grants ?? [];
  const isSuper = !!me?.is_superuser;
  const orgAdmin = (orgId: string) => isSuper || grants.some((g) => g.org_id === orgId && g.team_id === null && (g.role === "owner" || g.role === "admin"));
  const teamRole = (teamId: string) => grants.find((g) => g.team_id === teamId)?.role;
  return {
    isSuper,
    orgAdmin,
    anyOrgAdmin: isSuper || grants.some((g) => g.team_id === null && (g.role === "owner" || g.role === "admin")),
    teamAdmin: (t: { id: string; org_id: string }) => orgAdmin(t.org_id) || teamRole(t.id) === "admin",
    keyWrite: (t: { id: string; org_id: string }) => orgAdmin(t.org_id) || ["admin", "member"].includes(teamRole(t.id) ?? ""),
    anyKeyWrite: isSuper || grants.some((g) => g.role !== "viewer"),
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
export function useAction<A, R>(fn: (arg: A) => Promise<R>, success: string) {
  const toast = useToast();
  const m = useMut(fn, success);
  return (arg: A) => m.mutateAsync(arg).catch((e: Error) => toast("error", e.message));
}

export interface Tree {
  orgs: Org[];
  teams: Team[];
  projects: Project[];
}

async function ignoreDenied<T>(p: Promise<T[]>): Promise<T[]> {
  try {
    return await p;
  } catch (e) {
    if (e instanceof ApiError && (e.status === 403 || e.status === 404)) return [];
    throw e;
  }
}

/** Orgs, their teams and their projects: the hierarchy is only listable parent by parent. */
export function useTree() {
  return useQuery({
    queryKey: ["tree"],
    queryFn: async (): Promise<Tree> => {
      const orgs = await api.orgs();
      const teams = (await Promise.all(orgs.map((o) => ignoreDenied(api.teams(o.id))))).flat();
      const projects = (await Promise.all(teams.map((t) => ignoreDenied(api.projects(t.id))))).flat();
      return { orgs, teams, projects };
    },
  });
}

export function useAllKeys(projects: Project[] | undefined) {
  return useQuery({
    queryKey: ["keys", projects?.map((p) => p.id)],
    enabled: !!projects,
    queryFn: async () => (await Promise.all(projects!.map((p) => ignoreDenied(api.keys(p.id))))).flat(),
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
