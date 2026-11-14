import type { McpGrant, ModelAccess, PolicyOut, Team } from "../api";
import { nanoToUsd, usdToNano, fmtUsd } from "./Bits";
import { NumberField } from "./Form";
import { PermissionPicker } from "./PermissionPicker";
import { AgentGrants, McpGrants, type TargetScope } from "./TargetGrants";

/** Policy form state shared by teams and key owners; budgets are edited in USD. */
export interface PolicyValues {
  model_access: ModelAccess;
  allowed_models: string[];
  mcp_access: ModelAccess;
  mcp_servers: McpGrant[];
  a2a_access: ModelAccess;
  a2a_agents: string[];
  rpm_limit: number | null;
  tpm_limit: number | null;
  max_parallel: number | null;
  budget_usd: number | null;
}

export const blankPolicy = (isTeam = false): PolicyValues => ({
  model_access: isTeam ? "all" : "inherit",
  allowed_models: [],
  mcp_access: isTeam ? "all" : "inherit",
  mcp_servers: [],
  a2a_access: isTeam ? "all" : "inherit",
  a2a_agents: [],
  rpm_limit: null,
  tpm_limit: null,
  max_parallel: null,
  budget_usd: null,
});

/** Members and service accounts have no "All": it means the same as inheriting what the team allows. */
const accessFrom = (access: ModelAccess | undefined, isTeam: boolean): ModelAccess => {
  // A payload from before a grant kind existed reads as the level's default rather than as a crash.
  const value = access ?? (isTeam ? "all" : "inherit");
  return !isTeam && value === "all" ? "inherit" : value;
};

export const policyFrom = (p: PolicyOut, isTeam = false): PolicyValues => ({
  model_access: accessFrom(p.model_access, isTeam),
  allowed_models: p.allowed_models,
  mcp_access: accessFrom(p.mcp_access, isTeam),
  mcp_servers: (p.mcp_servers ?? []).map((grant) => ({ server: grant.server, all_tools: grant.all_tools, tools: [...grant.tools] })),
  a2a_access: accessFrom(p.a2a_access, isTeam),
  a2a_agents: [...(p.a2a_agents ?? [])],
  rpm_limit: p.rpm_limit,
  tpm_limit: p.tpm_limit,
  max_parallel: p.max_parallel,
  budget_usd: nanoToUsd(p.budget_nano_usd),
});

/**
 * Request fields for create/update; an explicit null clears a limit. A target list is only sent with
 * `mcp_access`/`a2a_access` "list": the API refuses it on any other level, and "all" keeps whatever the
 * parent allows.
 */
export const policyBody = (v: PolicyValues) => ({
  model_access: v.model_access,
  allowed_models: v.model_access === "list" ? v.allowed_models : [],
  mcp_access: v.mcp_access,
  mcp_servers:
    v.mcp_access === "list"
      ? v.mcp_servers.map((grant) => ({ server: grant.server, all_tools: grant.all_tools, tools: grant.all_tools ? [] : grant.tools }))
      : [],
  a2a_access: v.a2a_access,
  a2a_agents: v.a2a_access === "list" ? v.a2a_agents : [],
  rpm_limit: v.rpm_limit,
  tpm_limit: v.tpm_limit,
  max_parallel: v.max_parallel,
  budget_nano_usd: usdToNano(v.budget_usd),
});

/** Server field names -> form field names, for `useForm().run`. */
export const POLICY_ALIAS = { budget_nano_usd: "budget_usd" } as const;

interface Props {
  values: PolicyValues;
  errors: Partial<Record<keyof PolicyValues, string>>;
  set<K extends keyof PolicyValues>(k: K, v: PolicyValues[K]): void;
  /** Teams have no parent to inherit from. */
  isTeam?: boolean;
  /** The team a member or service account belongs to: its names bound what can be picked. */
  team?: Team;
  /** The level above a team: the org whose policy bounds it. Unset on an org's own policy. */
  parent?: PolicyOut;
}

/** The parent's grants, or null when it places no restriction (every name may be handed down). */
function targetScope(p: PolicyOut | undefined) {
  if (!p) return { mcp: null, agents: null };
  return {
    mcp: p.mcp_access === "list" ? p.mcp_servers.map((grant) => ({ server: grant.server, all_tools: grant.all_tools, tools: [...grant.tools] })) : null,
    agents: p.a2a_access === "list" ? [...p.a2a_agents] : null,
  };
}

export function PolicyFields({ values, errors, set, isTeam, team, parent }: Props) {
  const scope = isTeam || !team || team.model_access !== "list" ? null : team.allowed_models;
  // A member or service account narrows its team; a team narrows its org.
  const targets: TargetScope = targetScope(isTeam ? parent : team);
  return (
    <>
      <PermissionPicker isTeam={isTeam} access={values.model_access} onAccess={(a) => set("model_access", a)} selected={values.allowed_models} onSelected={(names) => set("allowed_models", names)} scope={scope} error={errors.model_access ?? errors.allowed_models} />
      <McpGrants isTeam={isTeam} access={values.mcp_access} onAccess={(a) => set("mcp_access", a)} selected={values.mcp_servers} onSelected={(grants) => set("mcp_servers", grants)} scope={targets.mcp} error={errors.mcp_access ?? errors.mcp_servers} />
      <AgentGrants isTeam={isTeam} access={values.a2a_access} onAccess={(a) => set("a2a_access", a)} selected={values.a2a_agents} onSelected={(names) => set("a2a_agents", names)} scope={targets.agents} error={errors.a2a_access ?? errors.a2a_agents} />
      <NumberField label="Requests per minute" value={values.rpm_limit} onChange={(v) => set("rpm_limit", v)} error={errors.rpm_limit} hint="Shared across gateways that use the same Redis namespace; otherwise counted per gateway." />
      <NumberField label="Tokens per minute" value={values.tpm_limit} onChange={(v) => set("tpm_limit", v)} error={errors.tpm_limit} hint="Shared across gateways that use the same Redis namespace; otherwise counted per gateway." />
      <NumberField label="Max parallel requests" value={values.max_parallel} onChange={(v) => set("max_parallel", v)} error={errors.max_parallel} hint="Shared across gateways that use the same Redis namespace; otherwise counted per gateway." />
      <NumberField label="Monthly budget (USD)" value={values.budget_usd} onChange={(v) => set("budget_usd", v)} error={errors.budget_usd} step="any" hint="Requests are blocked once spend reaches the budget." />
    </>
  );
}

/** How one level's target grants read in a table: every name, none, or how many. */
function targets(access: ModelAccess, count: number, what: string) {
  if (access === "list") return `${count} ${what}${count === 1 ? "" : "s"}`;
  return access === "all" ? `all ${what}s` : `inherits ${what}s`;
}

/** One-line summary of a level's policy for tables. */
export function PolicySummary({ p }: { p: PolicyOut }) {
  const parts = [
    p.model_access === "list" ? `${p.allowed_models.length} model${p.allowed_models.length === 1 ? "" : "s"}` : p.model_access === "all" ? "all models" : "inherits models",
    targets(p.mcp_access ?? "inherit", p.mcp_servers?.length ?? 0, "MCP server"),
    targets(p.a2a_access ?? "inherit", p.a2a_agents?.length ?? 0, "agent"),
    p.rpm_limit !== null && `${p.rpm_limit} rpm`,
    p.tpm_limit !== null && `${p.tpm_limit} tpm`,
    p.max_parallel !== null && `${p.max_parallel} parallel`,
  ].filter(Boolean);
  return <span className="text-muted-foreground">{parts.join(" · ")}</span>;
}

export const Budget = ({ p }: { p: PolicyOut }) => (
  <span className="tabular-nums">
    {fmtUsd(p.budget_nano_usd, "no limit")}
  </span>
);
