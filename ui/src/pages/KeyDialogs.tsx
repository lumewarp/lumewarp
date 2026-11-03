import { useEffect, useState } from "react";

import { api, type Key } from "../api";
import { SecretOnce } from "../components/Bits";
import { Dialog } from "../components/Dialog";
import { SelectField } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { useCan, useSecretMut } from "../hooks";
import { refusalText, rotationConfirmation } from "./keyText";

/** The secret is in a dialog's children only while `secret` is set; closing drops it from React state. */
export function SecretDialog({
  secret,
  title,
  note,
  onClose,
}: {
  secret: string | null;
  title: string;
  note?: string | null;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={!!secret}
      title={title}
      onClose={onClose}
      dismissible={false}
      footer={<Button onClick={onClose}>I’ve saved it</Button>}
    >
      {secret && (
        <>
          <SecretOnce key={secret} secret={secret} />
          {note && (
            <p className="mt-3 text-sm text-muted-foreground" data-testid="secret-note">
              {note}
            </p>
          )}
        </>
      )}
    </Dialog>
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
