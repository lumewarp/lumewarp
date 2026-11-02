import { useState } from "react";

import { ApiError } from "../api/errors";
import { useToast } from "./Toast";
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "./ui/alert-dialog";
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

/**
 * An alertdialog: focus starts on Cancel and a click on the backdrop does not dismiss it. The confirm
 * button is a plain button, not AlertDialogAction (that one closes on click without waiting): the dialog
 * closes only when the delete succeeded; a 409 turns it into the "Cannot delete" view with the referrers.
 */
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
    <AlertDialog open={open} onOpenChange={(next) => (!next ? close() : undefined)}>
      <AlertDialogContent {...(blocked ? { "aria-describedby": undefined } : {})}>
        <AlertDialogHeader>
          <AlertDialogTitle>{blocked ? "Cannot delete" : title}</AlertDialogTitle>
          {!blocked && <AlertDialogDescription>{message}</AlertDialogDescription>}
        </AlertDialogHeader>
        {blocked && (
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
        )}
        <AlertDialogFooter>
          {blocked ? (
            <Button type="button" onClick={close} autoFocus>
              Got it
            </Button>
          ) : (
            <>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <Button type="button" variant="destructive" disabled={busy} onClick={run}>
                {busy ? "Working…" : confirmLabel}
              </Button>
            </>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
