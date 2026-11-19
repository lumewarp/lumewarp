import { api, type McpGrant, type ModelAccess, type PolicyOut, type Team, type ZoneLimitsOut } from "../api";
import { useZone } from "../zone";
import { nanoToUsd, usdToNano, fmtUsd } from "./Bits";
import { NumberField } from "./Form";
import { HelpTip } from "./ui/help-tip";
import { PermissionPicker } from "./PermissionPicker";
import { AgentGrants, McpGrants, type TargetScope } from "./TargetGrants";

/** Policy form state shared by teams and key owners; budgets are edited in USD. */
export interface PolicyValues {
  model_access: ModelAccess;
  allowed_models: string[];
  allowed_groups: string[];
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
  allowed_groups: [],
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

/** An owner's own limits and budget in one zone (from `limits_by_zone`); none when the zone has no row. */
export const zoneLimits = (p: { limits_by_zone?: Record<string, ZoneLimitsOut> } | undefined, zone: string): ZoneLimitsOut =>
  p?.limits_by_zone?.[zone] ?? {};

export const policyFrom = (p: PolicyOut & { limits_by_zone?: Record<string, ZoneLimitsOut> }, isTeam = false, zone = ""): PolicyValues => ({
  model_access: accessFrom(p.model_access, isTeam),
  allowed_models: p.allowed_models,
  allowed_groups: [...(p.allowed_groups ?? [])],
  mcp_access: accessFrom(p.mcp_access, isTeam),
  mcp_servers: (p.mcp_servers ?? []).map((grant) => ({ server: grant.server, all_tools: grant.all_tools, tools: [...grant.tools] })),
  a2a_access: accessFrom(p.a2a_access, isTeam),
  a2a_agents: [...(p.a2a_agents ?? [])],
  rpm_limit: zoneLimits(p, zone).rpm_limit ?? null,
  tpm_limit: zoneLimits(p, zone).tpm_limit ?? null,
  max_parallel: zoneLimits(p, zone).max_parallel ?? null,
  budget_usd: nanoToUsd(zoneLimits(p, zone).budget_nano_usd ?? null),
});

/** Who a zone's limits belong to: exactly one shape, as `PUT /zones/{zone}/limits` takes it. */
export type LimitOwner = { org_id: string } | { team_id: string } | { team_id: string; user_id: string } | { team_id: string; service_account_id: string };

/** Write the limit and budget fields of the form as the owner's values in one zone (null clears one). */
export const saveZoneLimits = (zone: string, owner: LimitOwner, v: PolicyValues) =>
  api.putZoneLimits(zone, {
    ...owner,
    rpm_limit: v.rpm_limit,
    tpm_limit: v.tpm_limit,
    max_parallel: v.max_parallel,
    budget_nano_usd: usdToNano(v.budget_usd),
  });

/**
 * Request fields for create/update; an explicit null clears a limit. A target list is only sent with
 * `mcp_access`/`a2a_access` "list": the API refuses it on any other level, and "all" keeps whatever the
 * parent allows.
 */
export const policyBody = (v: PolicyValues) => ({
  model_access: v.model_access,
  allowed_models: v.model_access === "list" ? v.allowed_models : [],
  allowed_groups: v.model_access === "list" ? v.allowed_groups : [],
  mcp_access: v.mcp_access,
  mcp_servers:
    v.mcp_access === "list"
      ? v.mcp_servers.map((grant) => ({ server: grant.server, all_tools: grant.all_tools, tools: grant.all_tools ? [] : grant.tools }))
      : [],
  a2a_access: v.a2a_access,
  a2a_agents: v.a2a_access === "list" ? v.a2a_agents : [],
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
  parent?: PolicyOut & { effective_models?: string[] | null };
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
  // Names a level may hand down: a team's list is bounded by its org's effective models, a member's by the team's.
  const scope = (isTeam ? parent?.effective_models : team?.effective_models) ?? null;
  // A member or service account narrows its team; a team narrows its org.
  const targets: TargetScope = targetScope(isTeam ? parent : team);
  const zone = useZone();
  return (
    <>
      <PermissionPicker isTeam={isTeam} access={values.model_access} onAccess={(a) => set("model_access", a)} selected={values.allowed_models} onSelected={(names) => set("allowed_models", names)} selectedGroups={values.allowed_groups} onSelectedGroups={(groups) => set("allowed_groups", groups)} scope={scope} parentName={isTeam ? "the org" : "the team"} error={errors.model_access ?? errors.allowed_models} />
      <McpGrants isTeam={isTeam} access={values.mcp_access} onAccess={(a) => set("mcp_access", a)} selected={values.mcp_servers} onSelected={(grants) => set("mcp_servers", grants)} scope={targets.mcp} error={errors.mcp_access ?? errors.mcp_servers} />
      <AgentGrants isTeam={isTeam} access={values.a2a_access} onAccess={(a) => set("a2a_access", a)} selected={values.a2a_agents} onSelected={(names) => set("a2a_agents", names)} scope={targets.agents} error={errors.a2a_access ?? errors.a2a_agents} />
      <div className="flex items-center gap-1 border-t pt-3 text-sm font-semibold" data-testid="limits-heading">
        Limits in {zone.zone?.display_name || zone.name}
        <HelpTip text="Limits and budget apply to this zone only; switch zones in the header to set another." label="Limits" />
      </div>
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

/** "2 models", "1 model + 1 group": what a level's own list holds. */
function models(p: PolicyOut) {
  const names = p.allowed_models.length;
  const groups = p.allowed_groups?.length ?? 0;
  const text = `${names} model${names === 1 ? "" : "s"}`;
  return groups ? `${text} + ${groups} group${groups === 1 ? "" : "s"}` : text;
}

/** One-line summary of a level's policy for tables; limits are the header zone's. */
export function PolicySummary({ p }: { p: PolicyOut & { limits_by_zone?: Record<string, ZoneLimitsOut> } }) {
  const zone = useZone().name;
  const limits = zoneLimits(p, zone);
  const parts = [
    p.model_access === "list" ? models(p) : p.model_access === "all" ? "all models" : "inherits models",
    targets(p.mcp_access ?? "inherit", p.mcp_servers?.length ?? 0, "MCP server"),
    targets(p.a2a_access ?? "inherit", p.a2a_agents?.length ?? 0, "agent"),
    limits.rpm_limit != null && `${limits.rpm_limit} rpm`,
    limits.tpm_limit != null && `${limits.tpm_limit} tpm`,
    limits.max_parallel != null && `${limits.max_parallel} parallel`,
  ].filter(Boolean);
  return <span className="text-muted-foreground">{parts.join(" · ")}</span>;
}

export function Budget({ p }: { p: { limits_by_zone?: Record<string, ZoneLimitsOut> } }) {
  const zone = useZone().name;
  return <span className="tabular-nums">{fmtUsd(zoneLimits(p, zone).budget_nano_usd, "no limit")}</span>;
}

/** Whether the limit and budget fields differ, so a save writes them only when they were edited. */
export const limitsChanged = (a: PolicyValues, b: PolicyValues) =>
  a.rpm_limit !== b.rpm_limit || a.tpm_limit !== b.tpm_limit || a.max_parallel !== b.max_parallel || a.budget_usd !== b.budget_usd;
