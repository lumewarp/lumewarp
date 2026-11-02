import { useState } from "react";

import { ApiError } from "../api/errors";
import { Dialog } from "./Dialog";
import { useToast } from "./Toast";
import { Button } from "./ui/button";

interface Props {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => Promise<unknown>;
  onClose: () => void;
}

/** "'x' is referenced by: alias a, router b (fallback)" -> ["alias a", "router b (fallback)"]. */
export function parseReferrers(message: string): string[] | null {
  const m = /referenced by:\s*(.+)$/s.exec(message);
  return m ? m[1].split(/,\s*/).filter(Boolean) : null;
}

export function ConfirmDialog({ open, title, message, confirmLabel = "Delete", onConfirm, onClose }: Props) {
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState<ApiError | null>(null);
  const toast = useToast();
  const close = () => {
    setBlocked(null);
    onClose();
  };
  const run = async () => {
    setBusy(true);
    try {
      await onConfirm();
      close();
    } catch (e) {
      // A 409 means something still points at it: say what, and keep the dialog until it is read.
      if (e instanceof ApiError && e.status === 409) setBlocked(e);
      else {
        toast("error", e instanceof Error ? e.message : String(e));
        close();
      }
    } finally {
      setBusy(false);
    }
  };
  const referrers = blocked ? parseReferrers(blocked.message) : null;
  return (
    <Dialog
      open={open}
      onClose={close}
      title={blocked ? "Cannot delete" : title}
      footer={
        blocked ? (
          <Button type="button" onClick={close} autoFocus>
            Close
          </Button>
        ) : (
          <>
            <Button type="button" variant="outline" onClick={close} autoFocus>
              Cancel
            </Button>
            <Button type="button" variant="destructive" disabled={busy} onClick={run}>
              {busy ? "Working…" : confirmLabel}
            </Button>
          </>
        )
      }
    >
      {blocked ? (
        <div className="flex flex-col gap-3 text-sm" data-testid="delete-blocked" role="alert">
          <p>{referrers ? "Still in use. Remove these references first:" : blocked.message}</p>
          {referrers && (
            <ul className="list-disc pl-5">
              {referrers.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">{message}</p>
      )}
    </Dialog>
  );
}
