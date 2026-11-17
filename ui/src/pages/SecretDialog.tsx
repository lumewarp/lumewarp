import { SecretOnce } from "../components/Bits";
import { Dialog } from "../components/Dialog";
import { Button } from "../components/ui/button";

// Kept apart from the key dialogs: the shell mounts it on every page, so it must not pull in
// the key forms and their model picker.
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
