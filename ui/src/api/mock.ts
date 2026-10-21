import { ApiError } from "./errors";
import { getToken, handleUnauthorized } from "../auth";
import type {
  Api,
  ApiKey,
  AuditEntry,
  Deployment,
  Org,
  Principal,
  Project,
  Resource,
  Route,
  Team,
  Upstream,
  UpstreamInput,
  UsageRow,
} from "./types";

const delay = (ms = 150) => new Promise((r) => setTimeout(r, ms));
const ago = (days: number) => new Date(Date.now() - days * 86_400_000).toISOString();
let seq = 100;
const nid = (p: string) => `${p}_${(seq++).toString(36)}`;

const orgs: Org[] = [
  { id: "org_acme", name: "Acme Corp", slug: "acme", budget_usd: 5000, spent_usd: 2310.4, created_at: ago(120) },
  { id: "org_lab", name: "Research Lab", slug: "lab", budget_usd: null, spent_usd: 312.9, created_at: ago(60) },
];
const teams: Team[] = [
  { id: "team_ml", org_id: "org_acme", name: "ML Platform", budget_usd: 2000, spent_usd: 1720.1, rpm: 600, allowed_models: ["gpt-4o", "llama-3-70b"], created_at: ago(110) },
  { id: "team_app", org_id: "org_acme", name: "Product Apps", budget_usd: 1500, spent_usd: 590.3, rpm: 300, allowed_models: [], created_at: ago(90) },
  { id: "team_nlp", org_id: "org_lab", name: "NLP", budget_usd: null, spent_usd: 312.9, rpm: null, allowed_models: ["llama-3-70b", "text-embedding-3-small"], created_at: ago(50) },
];
const projects: Project[] = [
  { id: "proj_chat", team_id: "team_app", name: "support-chatbot", budget_usd: 800, spent_usd: 402.7, created_at: ago(80) },
  { id: "proj_rag", team_id: "team_ml", name: "rag-pipeline", budget_usd: 1000, spent_usd: 990.5, created_at: ago(70) },
  { id: "proj_eval", team_id: "team_nlp", name: "eval-harness", budget_usd: null, spent_usd: 312.9, created_at: ago(40) },
];
const keys: ApiKey[] = [
  { id: "key_1", project_id: "proj_chat", name: "prod", prefix: "lw-3f9a", status: "active", budget_usd: 500, spent_usd: 402.7, rpm: 120, tpm: 200000, allowed_models: ["gpt-4o"], expires_at: null, last_used_at: ago(0), created_at: ago(70) },
  { id: "key_2", project_id: "proj_rag", name: "indexer", prefix: "lw-b71c", status: "budget_exceeded", budget_usd: 1000, spent_usd: 1000, rpm: null, tpm: null, allowed_models: [], expires_at: ago(-30), last_used_at: ago(1), created_at: ago(65) },
  { id: "key_3", project_id: "proj_eval", name: "ci-old", prefix: "lw-09de", status: "revoked", budget_usd: null, spent_usd: 12.2, rpm: 60, tpm: null, allowed_models: [], expires_at: null, last_used_at: ago(20), created_at: ago(35) },
  { id: "key_4", project_id: "proj_chat", name: "staging", prefix: "lw-a2c8", status: "suspended", budget_usd: 50, spent_usd: 3.1, rpm: 30, tpm: null, allowed_models: [], expires_at: ago(-5), last_used_at: null, created_at: ago(10) },
];
const upstreams: Upstream[] = [
  { id: "up_vllm", name: "vllm-a100", kind: "vllm", base_url: "http://vllm-a100.internal:8000/v1", credential_set: false, created_at: ago(100) },
  { id: "up_oai", name: "openai", kind: "openai", base_url: "https://api.openai.com/v1", credential_set: true, created_at: ago(100) },
];
const deployments: Deployment[] = [
  { id: "dep_llama", name: "llama-70b-a100", upstream_id: "up_vllm", upstream_model: "meta-llama/Llama-3-70B-Instruct", price_in: 0.4, price_out: 0.8, max_parallel: 64 },
  { id: "dep_4o", name: "openai-gpt-4o", upstream_id: "up_oai", upstream_model: "gpt-4o", price_in: 2.5, price_out: 10, max_parallel: null },
  { id: "dep_4o_mini", name: "openai-gpt-4o-mini", upstream_id: "up_oai", upstream_model: "gpt-4o-mini", price_in: 0.15, price_out: 0.6, max_parallel: null },
];
const routes: Route[] = [
  { id: "route_1", alias: "gpt-4o", strategy: "weighted", targets: [{ deployment_id: "dep_4o", weight: 100 }], fallback_alias: "gpt-4o-mini", max_retries: 1 },
  { id: "route_2", alias: "gpt-4o-mini", strategy: "least_inflight", targets: [{ deployment_id: "dep_4o_mini", weight: 1 }], fallback_alias: null, max_retries: 2 },
  { id: "route_3", alias: "llama-3-70b", strategy: "least_latency", targets: [{ deployment_id: "dep_llama", weight: 1 }], fallback_alias: "gpt-4o", max_retries: 1 },
];
const audit: AuditEntry[] = [
  { id: "a1", at: ago(0), actor: "andy@example.com", action: "key.create", target: "key_4 (staging)" },
  { id: "a2", at: ago(1), actor: "andy@example.com", action: "route.update", target: "gpt-4o" },
  { id: "a3", at: ago(3), actor: "system", action: "key.budget_exceeded", target: "key_2 (indexer)" },
  { id: "a4", at: ago(20), actor: "sam@example.com", action: "key.revoke", target: "key_3 (ci-old)" },
];

function makeUsage(): UsageRow[] {
  const rows: UsageRow[] = [];
  const specs = [
    { model: "gpt-4o", team: "Product Apps", rate: 2.5 },
    { model: "llama-3-70b", team: "ML Platform", rate: 0.6 },
    { model: "gpt-4o-mini", team: "NLP", rate: 0.3 },
  ];
  for (let d = 0; d < 14; d++) {
    for (const [i, s] of specs.entries()) {
      const requests = 800 + ((d * 37 + i * 211) % 1500);
      const tokens_in = requests * (400 + ((d * 13 + i * 71) % 300));
      const tokens_out = Math.round(tokens_in * 0.35);
      rows.push({
        date: ago(d).slice(0, 10),
        model: s.model,
        team: s.team,
        requests,
        tokens_in,
        tokens_out,
        cost_usd: Math.round(((tokens_in + tokens_out) / 1e6) * s.rate * 100) / 100,
      });
    }
  }
  return rows;
}
const usage = makeUsage();

function guard(write = false) {
  const token = getToken();
  if (!token) handleUnauthorized();
  if (write && token!.startsWith("viewer")) throw new ApiError(403, "Viewers have read-only access");
}

function store<T extends { id: string }, C = Omit<T, "id">>(
  rows: T[],
  prefix: string,
  make: (input: C) => T,
  patch: (row: T, input: Partial<C>) => void = (row, input) => Object.assign(row, input),
  onRemove?: (id: string) => void,
): Resource<T, C> {
  return {
    async list() {
      await delay();
      guard();
      return rows.map((r) => ({ ...r }));
    },
    async create(input) {
      await delay();
      guard(true);
      const row = make(input);
      row.id = nid(prefix);
      rows.unshift(row);
      record("create", `${prefix} ${row.id}`);
      return { ...row };
    },
    async update(id, input) {
      await delay();
      guard(true);
      const row = rows.find((r) => r.id === id);
      if (!row) throw new ApiError(404, "Not found");
      patch(row, input);
      record("update", `${prefix} ${id}`);
      return { ...row };
    },
    async remove(id) {
      await delay();
      guard(true);
      const i = rows.findIndex((r) => r.id === id);
      if (i < 0) throw new ApiError(404, "Not found");
      rows.splice(i, 1);
      onRemove?.(id);
      record("delete", `${prefix} ${id}`);
    },
  };
}

function record(action: string, target: string) {
  audit.unshift({ id: nid("a"), at: new Date().toISOString(), actor: "you", action, target });
}

const created = () => new Date().toISOString();
const rand = (n: number) =>
  Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => (b % 36).toString(36)).join("");

export const mockApi: Api = {
  async me(): Promise<Principal> {
    await delay(80);
    guard();
    const token = getToken()!;
    return token.startsWith("viewer")
      ? { name: "Vera Viewer", email: "vera@example.com", role: "viewer" }
      : { name: "Andy", email: "andy@example.com", role: "owner" };
  },
  orgs: store<Org, Omit<Org, "id">>(orgs, "org", (i) => ({ ...i, id: "", spent_usd: 0, created_at: created() })),
  teams: store<Team, Omit<Team, "id">>(teams, "team", (i) => ({ ...i, id: "", spent_usd: 0, created_at: created() })),
  projects: store<Project, Omit<Project, "id">>(projects, "proj", (i) => ({ ...i, id: "", spent_usd: 0, created_at: created() })),
  keys: {
    ...store<ApiKey, Omit<ApiKey, "id">>(keys, "key", (i) => ({ ...i, id: "", spent_usd: 0, created_at: created() })),
    async create(input) {
      await delay();
      guard(true);
      const secret = `lw-${rand(40)}`;
      const key: ApiKey = {
        ...input,
        id: nid("key"),
        prefix: secret.slice(0, 7),
        status: "active",
        spent_usd: 0,
        last_used_at: null,
        created_at: created(),
      };
      keys.unshift(key);
      record("key.create", `${key.id} (${key.name})`);
      return { key: { ...key }, secret };
    },
  },
  async setKeyStatus(id, status) {
    await delay();
    guard(true);
    const key = keys.find((k) => k.id === id);
    if (!key) throw new ApiError(404, "Not found");
    key.status = status;
    record(`key.${status === "active" ? "activate" : status === "revoked" ? "revoke" : "suspend"}`, `${id} (${key.name})`);
    return { ...key };
  },
  upstreams: store<Upstream, UpstreamInput>(upstreams, "up", ({ credential, ...i }) => ({ ...i, id: "", credential_set: !!credential, created_at: created() }), (row, { credential, ...i }) => {
    Object.assign(row, i);
    if (credential) row.credential_set = true;
  }),
  deployments: store<Deployment>(deployments, "dep", (i) => ({ ...i, id: "" })),
  routes: store<Route>(routes, "route", (i) => ({ ...i, id: "" })),
  async usage() {
    await delay();
    guard();
    return usage;
  },
  async audit() {
    await delay();
    guard();
    return audit.map((a) => ({ ...a }));
  },
  async configStatus() {
    await delay();
    guard();
    return {
      version: 42,
      gateways: [
        { id: "gw-0", version: 42, connected: true, last_ack_at: ago(0) },
        { id: "gw-1", version: 41, connected: false, last_ack_at: ago(0.2) },
      ],
    };
  },
};
