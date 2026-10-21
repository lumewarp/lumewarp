import { handleUnauthorized, getToken } from "../auth";
import { ApiError } from "./errors";
import type { Api, ApiKey, CreatedKey, Input, Resource } from "./types";

const BASE = "/api/v1";

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = getToken();
  const resp = await fetch(BASE + path, {
    method,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401) handleUnauthorized();
  if (!resp.ok) {
    let message = `HTTP ${resp.status}`;
    try {
      const data = (await resp.json()) as { detail?: unknown };
      if (typeof data.detail === "string") message = data.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, message);
  }
  return resp.status === 204 ? (undefined as T) : ((await resp.json()) as T);
}

function resource<T, C, R = T>(path: string): Omit<Resource<T, C>, "create"> & { create(input: C): Promise<R> } {
  return {
    list: () => call("GET", path),
    create: (input) => call("POST", path, input),
    update: (id, input) => call("PATCH", `${path}/${id}`, input),
    remove: (id) => call("DELETE", `${path}/${id}`),
  };
}

export const httpApi: Api = {
  me: () => call("GET", "/me"),
  orgs: resource("/orgs"),
  teams: resource("/teams"),
  projects: resource("/projects"),
  keys: resource<ApiKey, Input<ApiKey>, CreatedKey>("/keys"),
  setKeyStatus: (id, status) => call("POST", `/keys/${id}/status`, { status }),
  upstreams: resource("/upstreams"),
  deployments: resource("/deployments"),
  routes: resource("/routes"),
  usage: () => call("GET", "/usage"),
  audit: () => call("GET", "/audit"),
  configStatus: () => call("GET", "/config/status"),
};
