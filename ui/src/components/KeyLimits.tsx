import { NumberField } from "./Form";

/** The key level of the quota hierarchy: rpm, tpm and parallelism, all optional. */
export interface KeyLimitValues {
  rpm_limit: number | null;
  tpm_limit: number | null;
  max_parallel: number | null;
}

export const blankKeyLimits = (): KeyLimitValues => ({ rpm_limit: null, tpm_limit: null, max_parallel: null });

export const keyLimitsFrom = (key: KeyLimitValues): KeyLimitValues => ({
  rpm_limit: key.rpm_limit,
  tpm_limit: key.tpm_limit,
  max_parallel: key.max_parallel,
});

/** Request fields for create/update; an explicit null clears a key limit. */
export const keyLimitsBody = (v: KeyLimitValues): KeyLimitValues => ({
  rpm_limit: v.rpm_limit,
  tpm_limit: v.tpm_limit,
  max_parallel: v.max_parallel,
});

/**
 * The owner's effective limits, when the API provides them (`/users/me` for the caller's
 * own membership; the owner's policy clamped by the team otherwise). `undefined` means the
 * UI does not know the cap and leaves validation to the server.
 */
export type KeyLimitCap = KeyLimitValues | undefined;

const limitNote = (cap: KeyLimitCap, value: number | null | undefined, capNote: string) => {
  if (!cap) return undefined;
  if (value == null) return "The owner has no limit at this level.";
  return `At most ${value.toLocaleString()} (${capNote}).`;
};

/**
 * The three key limit fields, with the owner's effective limit as the cap shown in the
 * form. The server still decides: a 422 above the cap is shown on the field.
 */
export function KeyLimitFields({
  values,
  errors,
  set,
  cap,
  capNote = "the owner's effective limit",
}: {
  values: KeyLimitValues;
  errors: Partial<Record<keyof KeyLimitValues, string>>;
  set<K extends keyof KeyLimitValues>(k: K, v: KeyLimitValues[K]): void;
  cap?: KeyLimitCap;
  /** What the cap is, when it is not the exact `/users/me` value. */
  capNote?: string;
}) {
  return (
    <>
      <NumberField
        label="Requests per minute"
        value={values.rpm_limit}
        onChange={(v) => set("rpm_limit", v)}
        error={errors.rpm_limit}
        hint="Rate limits are counted per gateway instance."
        note={limitNote(cap, cap?.rpm_limit, capNote)}
      />
      <NumberField
        label="Tokens per minute"
        value={values.tpm_limit}
        onChange={(v) => set("tpm_limit", v)}
        error={errors.tpm_limit}
        hint="Rate limits are counted per gateway instance."
        note={limitNote(cap, cap?.tpm_limit, capNote)}
      />
      <NumberField
        label="Max parallel requests"
        value={values.max_parallel}
        onChange={(v) => set("max_parallel", v)}
        error={errors.max_parallel}
        hint="Limits are counted per gateway instance."
        note={limitNote(cap, cap?.max_parallel, capNote)}
      />
    </>
  );
}
