import createClient from "openapi-fetch";
import { getCsrfToken, getToken, handleUnauthorized, setCsrfToken } from "../auth";
import { ApiError, fieldOf } from "./errors";
import type { components, paths } from "./schema";

export { ApiError } from "./errors";

export type Schemas = components["schemas"];
export type Team = Schemas["TeamOut"];
export type Key = Schemas["KeyOut"];
export type Member = Schemas["MemberOut"];
export type ServiceAccount = Schemas["ServiceAccountOut"];
export type Upstream = Schemas["UpstreamOut"];
export type Deployment = Schemas["DeploymentOut"];
export type Route = Schemas["RouteOut"];
export type User = Schemas["UserOut"];
export type Me = Schemas["MeOut"];
export type ModelAccess = Schemas["ModelAccess"];
export type PolicyOut = Pick<Team, "model_access" | "allowed_models" | "rpm_limit" | "tpm_limit" | "max_parallel" | "budget_nano_usd" | "soft_budget_nano_usd">;

export const client = createClient<paths>({ baseUrl: "", credentials: "same-origin" });

client.use({
  onRequest({ request }) {
    const token = getToken();
    if (token) {
      request.headers.set("Authorization", `Bearer ${token}`);
      request.headers.delete("X-Lumewarp-CSRF");
    } else {
      request.headers.delete("Authorization");
      if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method.toUpperCase())) {
        const csrfToken = getCsrfToken();
        if (csrfToken) request.headers.set("X-Lumewarp-CSRF", csrfToken);
      }
    }
    return request;
  },
});

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Resolve to the response body or throw an ApiError built from the `{error:{code,message}}` envelope. */
export async function unwrap<T>(p: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await p;
  if (response.ok) return data as T;
  if (response.status === 401) handleUnauthorized();
  const env = (error as { error?: { code?: string; message?: string } } | undefined)?.error;
  if (response.status === 403 && env?.code === "csrf_failed") {
    await api.session().catch(() => undefined);
  }
  const message = env?.message ?? `HTTP ${response.status}`;
  throw new ApiError(response.status, message, env?.code, response.status === 422 ? fieldOf(message) : undefined);
}

interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}

/** Follow `next_cursor` until the list is exhausted (admin lists are small in P1). */
export async function listAll<T>(fetchPage: (cursor: string | undefined) => Promise<Page<T>>): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | undefined;
  do {
    const page = await fetchPage(cursor);
    out.push(...page.items);
    cursor = page.next_cursor ?? undefined;
  } while (cursor);
  return out;
}

const q = (cursor: string | undefined) => ({ limit: 200, cursor });

export const api = {
  session: async () => {
    const session = await unwrap(client.GET("/api/v1/session"));
    setCsrfToken(session.authenticated ? (session.csrf_token ?? null) : null);
    return session;
  },
  logoutSession: () => unwrap(client.POST("/api/v1/session/logout")),

  me: () => unwrap(client.GET("/api/v1/users/me")),

  teams: () => listAll((c) => unwrap(client.GET("/api/v1/teams", { params: { query: q(c) } }))),
  team: (id: string) => unwrap(client.GET("/api/v1/teams/{team_id}", { params: { path: { team_id: id } } })),
  createTeam: (body: Schemas["TeamCreate"]) => unwrap(client.POST("/api/v1/teams", { body })),
  updateTeam: (id: string, body: Schemas["TeamUpdate"]) => unwrap(client.PATCH("/api/v1/teams/{team_id}", { params: { path: { team_id: id } }, body })),
  deleteTeam: (id: string) => unwrap(client.DELETE("/api/v1/teams/{team_id}", { params: { path: { team_id: id } } })),

  members: (teamId: string) => listAll((c) => unwrap(client.GET("/api/v1/teams/{team_id}/members", { params: { path: { team_id: teamId }, query: q(c) } }))),
  createMember: (teamId: string, body: Schemas["MemberCreate"]) =>
    unwrap(client.POST("/api/v1/teams/{team_id}/members", { params: { path: { team_id: teamId } }, body })),
  putMember: (teamId: string, userId: string, body: Schemas["MemberUpsert"]) =>
    unwrap(client.PUT("/api/v1/teams/{team_id}/members/{user_id}", { params: { path: { team_id: teamId, user_id: userId } }, body })),
  updateMember: (teamId: string, userId: string, body: Schemas["MemberUpdate"]) =>
    unwrap(client.PATCH("/api/v1/teams/{team_id}/members/{user_id}", { params: { path: { team_id: teamId, user_id: userId } }, body })),
  deleteMember: (teamId: string, userId: string) =>
    unwrap(client.DELETE("/api/v1/teams/{team_id}/members/{user_id}", { params: { path: { team_id: teamId, user_id: userId } } })),

  serviceAccounts: (teamId: string) => listAll((c) => unwrap(client.GET("/api/v1/teams/{team_id}/service-accounts", { params: { path: { team_id: teamId }, query: q(c) } }))),
  createServiceAccount: (teamId: string, body: Schemas["ServiceAccountCreate"]) =>
    unwrap(client.POST("/api/v1/teams/{team_id}/service-accounts", { params: { path: { team_id: teamId } }, body })),
  updateServiceAccount: (id: string, body: Schemas["ServiceAccountUpdate"]) =>
    unwrap(client.PATCH("/api/v1/service-accounts/{account_id}", { params: { path: { account_id: id } }, body })),
  deleteServiceAccount: (id: string) =>
    unwrap(client.DELETE("/api/v1/service-accounts/{account_id}", { params: { path: { account_id: id } } })),

  teamKeys: (teamId: string) => listAll((c) => unwrap(client.GET("/api/v1/teams/{team_id}/keys", { params: { path: { team_id: teamId }, query: q(c) } }))),
  createKey: (teamId: string, body: Schemas["KeyCreate"]) =>
    unwrap(client.POST("/api/v1/teams/{team_id}/keys", { params: { path: { team_id: teamId } }, body })),
  ownKeys: () => listAll((c) => unwrap(client.GET("/api/v1/users/me/keys", { params: { query: q(c) } }))),
  updateKey: (id: string, body: Schemas["KeyUpdate"]) => unwrap(client.PATCH("/api/v1/keys/{key_id}", { params: { path: { key_id: id } }, body })),
  revokeKey: (id: string) => unwrap(client.POST("/api/v1/keys/{key_id}/revoke", { params: { path: { key_id: id } } })),
  rotateKey: (id: string) => unwrap(client.POST("/api/v1/keys/{key_id}/rotate", { params: { path: { key_id: id } } })),

  upstreams: () => listAll((c) => unwrap(client.GET("/api/v1/upstreams", { params: { query: q(c) } }))),
  createUpstream: (body: Schemas["UpstreamCreate"]) => unwrap(client.POST("/api/v1/upstreams", { body })),
  updateUpstream: (id: string, body: Schemas["UpstreamUpdate"]) => unwrap(client.PATCH("/api/v1/upstreams/{upstream_id}", { params: { path: { upstream_id: id } }, body })),
  deleteUpstream: (id: string) => unwrap(client.DELETE("/api/v1/upstreams/{upstream_id}", { params: { path: { upstream_id: id } } })),

  deployments: () => listAll((c) => unwrap(client.GET("/api/v1/deployments", { params: { query: q(c) } }))),
  createDeployment: (body: Schemas["DeploymentCreate"]) => unwrap(client.POST("/api/v1/deployments", { body })),
  updateDeployment: (id: string, body: Schemas["DeploymentUpdate"]) => unwrap(client.PATCH("/api/v1/deployments/{deployment_id}", { params: { path: { deployment_id: id } }, body })),
  deleteDeployment: (id: string) => unwrap(client.DELETE("/api/v1/deployments/{deployment_id}", { params: { path: { deployment_id: id } } })),

  routes: () => listAll((c) => unwrap(client.GET("/api/v1/routes", { params: { query: q(c) } }))),
  createRoute: (body: Schemas["RouteCreate"]) => unwrap(client.POST("/api/v1/routes", { body })),
  updateRoute: (id: string, body: Schemas["RouteUpdate"]) => unwrap(client.PATCH("/api/v1/routes/{route_id}", { params: { path: { route_id: id } }, body })),
  deleteRoute: (id: string) => unwrap(client.DELETE("/api/v1/routes/{route_id}", { params: { path: { route_id: id } } })),

  users: () => listAll((c) => unwrap(client.GET("/api/v1/users", { params: { query: q(c) } }))),
  usersPage: (cursor?: string) =>
    unwrap(client.GET("/api/v1/users", { params: { query: { limit: 50, cursor } } })),
  createUser: (body: Schemas["UserCreate"]) => unwrap(client.POST("/api/v1/users", { body })),
  updateUser: (id: string, body: Schemas["UserUpdate"]) => unwrap(client.PATCH("/api/v1/users/{user_id}", { params: { path: { user_id: id } }, body })),
  deleteUser: (id: string) => unwrap(client.DELETE("/api/v1/users/{user_id}", { params: { path: { user_id: id } } })),

  tokens: () => listAll((c) => unwrap(client.GET("/api/v1/users/me/tokens", { params: { query: q(c) } }))),
  createToken: (body: Schemas["TokenCreate"]) => unwrap(client.POST("/api/v1/users/me/tokens", { body })),
  revokeToken: (id: string) => unwrap(client.POST("/api/v1/users/me/tokens/{token_id}/revoke", { params: { path: { token_id: id } } })),

  usage: (query: { from?: string; to?: string; group_by?: string; team_id?: string; user_id?: string; service_account_id?: string; key_id?: string; model?: string }) =>
    listAll((c) => unwrap(client.GET("/api/v1/usage", { params: { query: { ...query, ...q(c) } } }))),
  audit: (query: { team_id?: string; actor_id?: string; action?: string; resource_type?: string; resource_id?: string; from?: string; to?: string } = {}) =>
    listAll((c) => unwrap(client.GET("/api/v1/audit", { params: { query: { ...query, ...q(c) } } }))),

  gateways: () => listAll((c) => unwrap(client.GET("/api/v1/gateways", { params: { query: q(c) } }))),
  createGatewayToken: (body: Schemas["GatewayTokenCreate"]) => unwrap(client.POST("/api/v1/gateways", { body })),
  revokeGatewayToken: (gatewayId: string, tokenId: string) =>
    unwrap(client.POST("/api/v1/gateways/{gateway_id}/tokens/{token_id}/revoke", { params: { path: { gateway_id: gatewayId, token_id: tokenId } } })),

  configVersions: () => listAll((c) => unwrap(client.GET("/api/v1/config/versions", { params: { query: q(c) } }))),
  rollback: (version: number) => unwrap(client.POST("/api/v1/config/rollback", { body: { version } })),
};
