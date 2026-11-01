import type { ModelAccess, PolicyOut } from "../api";
import { nanoToUsd, usdToNano, fmtUsd } from "./Bits";
import { ListField, NumberField, SelectField } from "./Form";

/** Policy form state shared by teams and key owners; budgets are edited in USD. */
export interface PolicyValues {
  model_access: ModelAccess;
  allowed_models: string[];
  rpm_limit: number | null;
  tpm_limit: number | null;
  max_parallel: number | null;
  budget_usd: number | null;
  soft_budget_usd: number | null;
}

export const blankPolicy = (isTeam = false): PolicyValues => ({
  model_access: isTeam ? "all" : "inherit",
  allowed_models: [],
  rpm_limit: null,
  tpm_limit: null,
  max_parallel: null,
  budget_usd: null,
  soft_budget_usd: null,
});

export const policyFrom = (p: PolicyOut): PolicyValues => ({
  model_access: p.model_access,
  allowed_models: p.allowed_models,
  rpm_limit: p.rpm_limit,
  tpm_limit: p.tpm_limit,
  max_parallel: p.max_parallel,
  budget_usd: nanoToUsd(p.budget_nano_usd),
  soft_budget_usd: nanoToUsd(p.soft_budget_nano_usd),
});

/** Request fields for create/update; an explicit null clears a limit. */
export const policyBody = (v: PolicyValues) => ({
  model_access: v.model_access,
  allowed_models: v.model_access === "list" ? v.allowed_models : [],
  rpm_limit: v.rpm_limit,
  tpm_limit: v.tpm_limit,
  max_parallel: v.max_parallel,
  budget_nano_usd: usdToNano(v.budget_usd),
  soft_budget_nano_usd: usdToNano(v.soft_budget_usd),
});

/** Server field names -> form field names, for `useForm().run`. */
export const POLICY_ALIAS = { budget_nano_usd: "budget_usd", soft_budget_nano_usd: "soft_budget_usd" } as const;

const ACCESS_HINT: Record<ModelAccess, string> = {
  inherit: "Use exactly what the parent level allows.",
  all: "No restriction at this level (the parent's limits still apply).",
  list: "Only the models listed below, intersected with the parent. An empty list denies everything.",
};

interface Props {
  values: PolicyValues;
  errors: Partial<Record<keyof PolicyValues, string>>;
  set<K extends keyof PolicyValues>(k: K, v: PolicyValues[K]): void;
  /** Teams have no parent to inherit from. */
  isTeam?: boolean;
  models?: string[];
}

export function PolicyFields({ values, errors, set, isTeam, models }: Props) {
  const options = [...(isTeam ? [] : [{ value: "inherit", label: "Inherit from team" }]), { value: "all", label: "All models" }, { value: "list", label: "Specific models" }];
  return (
    <>
      <SelectField label="Model access" value={values.model_access} onChange={(v) => set("model_access", v as ModelAccess)} options={options} error={errors.model_access} hint={ACCESS_HINT[values.model_access]} />
      {values.model_access === "list" && (
        <ListField
          label="Allowed models"
          value={values.allowed_models}
          onChange={(v) => set("allowed_models", v)}
          error={errors.allowed_models}
          hint={models?.length ? `Comma-separated aliases. Known: ${models.join(", ")}` : "Comma-separated model aliases."}
        />
      )}
      <NumberField label="Requests per minute" value={values.rpm_limit} onChange={(v) => set("rpm_limit", v)} error={errors.rpm_limit} />
      <NumberField label="Tokens per minute" value={values.tpm_limit} onChange={(v) => set("tpm_limit", v)} error={errors.tpm_limit} />
      <NumberField label="Max parallel requests" value={values.max_parallel} onChange={(v) => set("max_parallel", v)} error={errors.max_parallel} />
      <NumberField label="Monthly budget (USD)" value={values.budget_usd} onChange={(v) => set("budget_usd", v)} error={errors.budget_usd} step="any" hint="Requests are blocked once spend reaches the budget." />
      <NumberField label="Soft budget (USD)" value={values.soft_budget_usd} onChange={(v) => set("soft_budget_usd", v)} error={errors.soft_budget_usd} step="any" hint="Sends a notification only; must not exceed the budget." />
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
    {p.soft_budget_nano_usd !== null && <span className="text-muted-foreground"> (soft {fmtUsd(p.soft_budget_nano_usd)})</span>}
  </span>
);
