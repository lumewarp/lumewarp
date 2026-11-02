import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api/errors";
import { useReturnFocus } from "./Dialog";
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
  const onCloseAutoFocus = useReturnFocus(open);
  // One number per "round" of the dialog: closing or reopening starts a new one. A request answers into the round
  // it was sent in; an answer for an earlier round (Esc, then another row's delete) touches nothing here. The
  // request itself is not aborted, so its toast and the caller's cache updates still happen.
  const round = useRef(0);
  const close = () => {
    round.current++;
    setBlocked(null);
    setBusy(false);
    onClose();
  };
  useEffect(() => {
    round.current++;
    setBlocked(null);
    setBusy(false);
  }, [open]);
  const run = async () => {
    const mine = round.current;
    const current = () => round.current === mine;
    setBusy(true);
    try {
      await onConfirm();
      if (current()) close();
    } catch (e) {
      // A 409 means something still points at it: say what, and keep the dialog until it is read.
      if (e instanceof ApiError && e.status === 409) {
        if (current()) setBlocked(e);
      } else {
        toast("error", e instanceof Error ? e.message : String(e));
        if (current()) close();
      }
    } finally {
      if (current()) setBusy(false);
    }
  };
  const referrers = blocked ? parseReferrers(blocked.message) : null;
  return (
    <AlertDialog open={open} onOpenChange={(next) => (!next ? close() : undefined)}>
      {/* Header and footer keep their size; only the middle scrolls, so a long list of referrers never pushes Got it off screen. */}
      <AlertDialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col gap-4" onCloseAutoFocus={onCloseAutoFocus} {...(blocked ? { "aria-describedby": undefined } : {})}>
        <AlertDialogHeader className="shrink-0">
          <AlertDialogTitle className="m-0">{blocked ? "Cannot delete" : title}</AlertDialogTitle>
          {!blocked && <AlertDialogDescription className="[overflow-wrap:anywhere]">{message}</AlertDialogDescription>}
        </AlertDialogHeader>
        {blocked && (
          <div className="flex min-h-0 flex-col gap-3 overflow-y-auto text-sm [overflow-wrap:anywhere]" data-testid="delete-blocked" role="alert">
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
        <AlertDialogFooter className="shrink-0">
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
