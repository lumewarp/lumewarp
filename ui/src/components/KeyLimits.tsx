import type { Zone } from "../api";
import { fmtLifetime } from "../pages/keyText";
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
        hint="Shared across gateways that use the same Redis namespace; otherwise counted per gateway."
        note={limitNote(cap, cap?.rpm_limit, capNote)}
      />
      <NumberField
        label="Tokens per minute"
        value={values.tpm_limit}
        onChange={(v) => set("tpm_limit", v)}
        error={errors.tpm_limit}
        hint="Shared across gateways that use the same Redis namespace; otherwise counted per gateway."
        note={limitNote(cap, cap?.tpm_limit, capNote)}
      />
      <NumberField
        label="Max parallel requests"
        value={values.max_parallel}
        onChange={(v) => set("max_parallel", v)}
        error={errors.max_parallel}
        hint="Shared across gateways that use the same Redis namespace; otherwise counted per gateway."
        note={limitNote(cap, cap?.max_parallel, capNote)}
      />
    </>
  );
}

/** The rules a zone sets for keys created in it. */
export type ZoneKeyRules = Pick<Zone, "key_min_creator_role" | "key_max_lifetime_seconds" | "key_expiry_required" | "key_max_per_member" | "service_account_keys_allowed" | "playground_allowed">;

const ROLE_RANK = { member: 0, team_admin: 1, org_admin: 2 } as const;
const MIN_ROLE_TEXT = { member: "a member", team_admin: "a team admin", org_admin: "an org admin" } as const;

/**
 * Why no key can be created in the zone right now, or null when one can. `role` is the caller's role in the team,
 * `inherited` how an inherited administration reaches it ("org" | "installation" | null for a direct role).
 * The server enforces the same rules; this only explains a refusal before it happens.
 */
export function keyCreationBlock(
  rules: ZoneKeyRules | undefined,
  zoneLabel: string,
  { role, inherited, serviceAccount }: { role: "admin" | "member"; inherited?: string | null; serviceAccount: boolean },
): string | null {
  if (!rules) return null;
  const rank = inherited ? 2 : role === "admin" ? 1 : 0;
  if (serviceAccount && !rules.service_account_keys_allowed) return `${zoneLabel} does not allow service-account keys.`;
  if (rank < ROLE_RANK[rules.key_min_creator_role]) return `Keys in ${zoneLabel} can only be created by ${MIN_ROLE_TEXT[rules.key_min_creator_role]} or above.`;
  return null;
}

/** The zone's key rules worth telling the creator, one short sentence each. */
export function keyRuleNotes(rules: ZoneKeyRules | undefined, zoneLabel: string, perMemberCap: number | null | undefined = rules?.key_max_per_member): string[] {
  if (!rules) return [];
  return [
    rules.key_max_lifetime_seconds != null ? `Keys in ${zoneLabel} live at most ${fmtLifetime(rules.key_max_lifetime_seconds)}.` : null,
    rules.key_expiry_required ? `An expiry is required in ${zoneLabel}.` : null,
    perMemberCap != null ? `At most ${perMemberCap} non-revoked keys per member in ${zoneLabel}.` : null,
  ].filter((note): note is string => !!note);
}
