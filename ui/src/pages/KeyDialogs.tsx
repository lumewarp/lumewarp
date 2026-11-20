import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, type Key, type Schemas } from "../api";
import { Dialog } from "../components/Dialog";
import { FormDialog, required, SelectField, TextField, useForm } from "../components/Form";
import { KeyLimitFields, keyLimitsBody, keyLimitsFrom, type KeyLimitCap, type KeyLimitValues } from "../components/KeyLimits";
import { KeyModelPicker, keyModelsBody, keyModelsFrom, type KeyModelState } from "../components/PermissionPicker";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { useCan, useMut, useSecretMut } from "../hooks";
import { expiryFromDay, fmtLifetime, lastExpiryDay, localDayOf, refusalText, rotationConfirmation } from "./keyText";

type EditValues = KeyLimitValues & { name: string; expires: string; models: KeyModelState };

/**
 * Editing an existing key, shared by the team Keys tab and the service-account table on My keys.
 * `lifetime` is the team's member-key cap, or null/undefined for a service-account key.
 */
export function KeyEditDialog({
  target,
  lifetime,
  cap,
  capNote,
  requiredExpiry = false,
  onClose,
}: {
  target: Key;
  lifetime?: number | null;
  /** The owner's effective limits, when the page knows them. */
  cap?: KeyLimitCap;
  capNote?: string;
  /** The team sets a member-key lifetime, so clearing the field is refused. */
  requiredExpiry?: boolean;
  onClose: () => void;
}) {
  const initialExpiry = localDayOf(target.expires_at);
  const initial: EditValues = { name: target.name, expires: initialExpiry, models: keyModelsFrom(target), ...keyLimitsFrom(target) };
  const form = useForm<EditValues>(initial, { name: required("Name") });
  const qc = useQueryClient();
  const update = useMut((values: EditValues) => {
    const { models, ...rest } = values;
    const body: Schemas["KeyUpdate"] = { name: values.name.trim(), ...keyLimitsBody(rest) };
    if (JSON.stringify(models) !== JSON.stringify(initial.models)) Object.assign(body, keyModelsBody(models));
    if (values.expires !== initialExpiry) body.expires_at = expiryFromDay(values.expires);
    return api.updateKey(target.id, body);
  }, "Key saved");
  const max = lifetime === null || lifetime === undefined ? undefined : lastExpiryDay(lifetime);
  const close = () => {
    form.reset(initial);
    onClose();
  };
  const submit = async () => {
    if (!form.validate()) return false;
    // Close only once the key lists have refetched: reopening Edit right away must show
    // the saved values, not the row from before the save.
    const save = async () => {
      const saved = await update.mutateAsync(form.values);
      await qc.refetchQueries({ type: "active" });
      return saved;
    };
    return form.run(save, { expires_at: "expires", model_access: "models", allowed_models: "models", allowed_groups: "models" });
  };
  return (
    <FormDialog open title={`Edit “${target.name}” (${target.display_prefix}…)`} submitLabel="Save" onClose={close} onSubmit={submit}>
      <TextField label="Name" value={form.values.name} onChange={(value) => form.set("name", value)} error={form.errors.name} autoFocus />
      <TextField
        label="Expires"
        type="date"
        value={form.values.expires}
        max={max}
        onChange={(value) => form.set("expires", value)}
        error={form.errors.expires}
        note={
          lifetime === null || lifetime === undefined
            ? "Empty means the key never expires."
            : `${requiredExpiry ? "Required. " : ""}This team limits member keys to ${fmtLifetime(lifetime)}; the latest day is ${max}.`
        }
      />
      <KeyLimitFields values={form.values} errors={form.errors} set={form.set} cap={cap} capNote={capNote} />
      <KeyModelPicker
        access={form.values.models.access}
        onAccess={(access) => form.set("models", { ...form.values.models, access })}
        grant={form.values.models.grant}
        onGrant={(grant) => form.set("models", { ...form.values.models, grant })}
        scope={target.owner_models ?? null}
        error={form.errors.models}
      />
    </FormDialog>
  );
}

const GRACE = [
  { value: "0", label: "No grace" },
  { value: "3600", label: "1 hour" },
  { value: "86400", label: "24 hours" },
];

/**
 * Rotate in place, with an optional grace for the old secret. The confirmation is built from the key's
 * real state: a suspended or expired key is not made usable by rotating, and a second rotation ends an
 * older grace at once. The secret goes to `onRotated` and is never a mutation result.
 */
export function RotateKeyDialog({
  target,
  onClose,
  onRotated,
}: {
  target: Key | null;
  onClose: () => void;
  onRotated: (secret: string, key: Key) => void;
}) {
  const [grace, setGrace] = useState("0");
  const [error, setError] = useState<string | null>(null);
  const perms = useCan();
  const canSelfResume = target ? perms.canResumeKey(target) : false;
  const rotate = useSecretMut(
    ({ key, seconds }: { key: Key; seconds: number }) => api.rotateKey(key.id, { grace_seconds: seconds }),
    (secret, key) => onRotated(secret, key),
  );
  useEffect(() => {
    setGrace("0");
    setError(null);
  }, [target?.id]);
  const seconds = Number(grace);
  const close = () => {
    setGrace("0");
    setError(null);
    onClose();
  };
  const run = async () => {
    if (!target) return;
    setError(null);
    try {
      await rotate.mutateAsync({ key: target, seconds });
    } catch (e) {
      setError(refusalText(e));
    }
  };
  return (
    <Dialog
      open={!!target}
      title={target ? `Rotate “${target.name}” (${target.display_prefix}…)` : "Rotate key"}
      onClose={close}
      footer={
        <>
          <Button type="button" variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button type="button" disabled={rotate.isPending} onClick={() => void run()}>
            {rotate.isPending ? "Rotating…" : "Rotate key"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3 text-sm">
        <SelectField
          label="Old secret"
          value={grace}
          onChange={setGrace}
          options={GRACE}
          hint="The new secret replaces it in place; usage stays on this key."
        />
        <p className="m-0" data-testid="rotate-notice">
          {target ? rotationConfirmation(target, seconds, canSelfResume) : ""}
        </p>
        {error && (
          <Alert variant="destructive" data-testid="rotate-error">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
      </div>
    </Dialog>
  );
}
