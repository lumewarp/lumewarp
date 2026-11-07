import type { ModelAccess, PolicyOut, Team } from "../api";
import { nanoToUsd, usdToNano, fmtUsd } from "./Bits";
import { NumberField } from "./Form";
import { PermissionPicker } from "./PermissionPicker";

/** Policy form state shared by teams and key owners; budgets are edited in USD. */
export interface PolicyValues {
  model_access: ModelAccess;
  allowed_models: string[];
  rpm_limit: number | null;
  tpm_limit: number | null;
  max_parallel: number | null;
  budget_usd: number | null;
}

export const blankPolicy = (isTeam = false): PolicyValues => ({
  model_access: isTeam ? "all" : "inherit",
  allowed_models: [],
  rpm_limit: null,
  tpm_limit: null,
  max_parallel: null,
  budget_usd: null,
});

/** Members and service accounts have no "All": it means the same as inheriting what the team allows. */
export const policyFrom = (p: PolicyOut, isTeam = false): PolicyValues => ({
  model_access: !isTeam && p.model_access === "all" ? "inherit" : p.model_access,
  allowed_models: p.allowed_models,
  rpm_limit: p.rpm_limit,
  tpm_limit: p.tpm_limit,
  max_parallel: p.max_parallel,
  budget_usd: nanoToUsd(p.budget_nano_usd),
});

/** Request fields for create/update; an explicit null clears a limit. */
export const policyBody = (v: PolicyValues) => ({
  model_access: v.model_access,
  allowed_models: v.model_access === "list" ? v.allowed_models : [],
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
}

export function PolicyFields({ values, errors, set, isTeam, team }: Props) {
  const scope = isTeam || !team || team.model_access !== "list" ? null : team.allowed_models;
  return (
    <>
      <PermissionPicker isTeam={isTeam} access={values.model_access} onAccess={(a) => set("model_access", a)} selected={values.allowed_models} onSelected={(names) => set("allowed_models", names)} scope={scope} error={errors.model_access ?? errors.allowed_models} />
      <NumberField label="Requests per minute" value={values.rpm_limit} onChange={(v) => set("rpm_limit", v)} error={errors.rpm_limit} hint="Rate limits are counted per gateway instance." />
      <NumberField label="Tokens per minute" value={values.tpm_limit} onChange={(v) => set("tpm_limit", v)} error={errors.tpm_limit} hint="Rate limits are counted per gateway instance." />
      <NumberField label="Max parallel requests" value={values.max_parallel} onChange={(v) => set("max_parallel", v)} error={errors.max_parallel} hint="Limits are counted per gateway instance." />
      <NumberField label="Monthly budget (USD)" value={values.budget_usd} onChange={(v) => set("budget_usd", v)} error={errors.budget_usd} step="any" hint="Requests are blocked once spend reaches the budget." />
    </>
  );
}

/** One-line summary of a level's policy for tables. */
export function PolicySummary({ p }: { p: PolicyOut }) {
  const parts = [
    p.model_access === "list" ? `${p.allowed_models.length} model${p.allowed_models.length === 1 ? "" : "s"}` : p.model_access === "all" ? "all models" : "inherits models",
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
