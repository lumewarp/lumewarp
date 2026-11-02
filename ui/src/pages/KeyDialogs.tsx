import { useEffect, useState } from "react";

import { api, type Key, type Schemas } from "../api";
import { fmtDateTime, SecretOnce } from "../components/Bits";
import { Dialog } from "../components/Dialog";
import { SelectField } from "../components/Form";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { useMut } from "../hooks";
import { refusalText } from "./keyText";

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
      footer={<Button onClick={onClose}>I’ve saved it</Button>}
    >
      {secret && (
        <>
          <SecretOnce secret={secret} />
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
 * Rotate in place, with an optional grace for the old secret. The confirmation says what happens to the old
 * secret before anything is sent; a refusal (a 429 with a wait, a 422 past the team's lifetime) stays here.
 */
export function RotateKeyDialog({
  target,
  onClose,
  onRotated,
}: {
  target: Key | null;
  onClose: () => void;
  onRotated: (created: Schemas["KeyCreated"]) => void;
}) {
  const [grace, setGrace] = useState("0");
  const [error, setError] = useState<string | null>(null);
  const rotate = useMut(
    ({ key, seconds }: { key: Key; seconds: number }) => api.rotateKey(key.id, { grace_seconds: seconds }),
    (created) => onRotated(created),
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
      // The new secret is passed to the caller; drop it from the mutation cache as well.
      rotate.reset();
    } catch (e) {
      setError(refusalText(e));
    }
  };
  const until = new Date(Date.now() + seconds * 1000);
  return (
    <Dialog
      open={!!target}
      title={target ? `Rotate “${target.name}”` : "Rotate key"}
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
          {seconds === 0
            ? "The old secret stops working right away."
            : `The old secret keeps working until ${fmtDateTime(until.toISOString())}, or the key's expiry if sooner.`}
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
