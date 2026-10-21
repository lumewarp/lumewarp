// Hand-written until W3 swaps these for types generated from the control plane's OpenAPI.

export type Role = "owner" | "admin" | "member" | "viewer";
export type KeyStatus = "active" | "revoked" | "budget_exceeded" | "suspended";
export type UpstreamKind = "vllm" | "openai";
export type Strategy = "weighted" | "least_latency" | "least_inflight";

export interface Principal {
  name: string;
  email: string;
  role: Role;
}

export interface Org {
  id: string;
  name: string;
  slug: string;
  budget_usd: number | null;
  spent_usd: number;
  created_at: string;
}

export interface Team {
  id: string;
  org_id: string;
  name: string;
  budget_usd: number | null;
  spent_usd: number;
  rpm: number | null;
  allowed_models: string[];
  created_at: string;
}

export interface Project {
  id: string;
  team_id: string;
  name: string;
  budget_usd: number | null;
  spent_usd: number;
  created_at: string;
}

export interface ApiKey {
  id: string;
  project_id: string;
  name: string;
  prefix: string;
  status: KeyStatus;
  budget_usd: number | null;
  spent_usd: number;
  rpm: number | null;
  tpm: number | null;
  allowed_models: string[];
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export interface Upstream {
  id: string;
  name: string;
  kind: UpstreamKind;
  base_url: string;
  credential_set: boolean;
  created_at: string;
}

export interface Deployment {
  id: string;
  name: string;
  upstream_id: string;
  upstream_model: string;
  /** USD per 1M tokens. */
  price_in: number;
  price_out: number;
  max_parallel: number | null;
}

export interface RouteTarget {
  deployment_id: string;
  weight: number;
}

export interface Route {
  id: string;
  alias: string;
  strategy: Strategy;
  targets: RouteTarget[];
  fallback_alias: string | null;
  max_retries: number;
}

export interface UsageRow {
  date: string;
  model: string;
  team: string;
  requests: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
}

export interface AuditEntry {
  id: string;
  at: string;
  actor: string;
  action: string;
  target: string;
}

export interface ConfigStatus {
  version: number;
  gateways: { id: string; version: number; connected: boolean; last_ack_at: string }[];
}

/** Writable fields of a resource: everything the server owns is left out. */
export type Input<T> = Omit<T, "id" | "created_at" | "spent_usd" | "prefix" | "status" | "last_used_at" | "credential_set">;

export interface Resource<T, C = Input<T>> {
  list(): Promise<T[]>;
  create(input: C): Promise<T>;
  update(id: string, input: Partial<C>): Promise<T>;
  remove(id: string): Promise<void>;
}

export interface UpstreamInput extends Input<Upstream> {
  /** Upstream credential; write-only. */
  credential?: string;
}

export interface CreatedKey {
  key: ApiKey;
  /** Plain secret, returned exactly once. */
  secret: string;
}

export interface Api {
  me(): Promise<Principal>;
  orgs: Resource<Org>;
  teams: Resource<Team>;
  projects: Resource<Project>;
  keys: Omit<Resource<ApiKey>, "create"> & { create(input: Input<ApiKey>): Promise<CreatedKey> };
  setKeyStatus(id: string, status: "active" | "revoked" | "suspended"): Promise<ApiKey>;
  upstreams: Resource<Upstream, UpstreamInput>;
  deployments: Resource<Deployment>;
  routes: Resource<Route>;
  usage(): Promise<UsageRow[]>;
  audit(): Promise<AuditEntry[]>;
  configStatus(): Promise<ConfigStatus>;
}
