import { XIcon } from "lucide-react";
import { useLayoutEffect, useRef, type ReactNode } from "react";

import { Button } from "./ui/button";
import { Dialog as UiDialog, DialogClose, DialogContent, DialogTitle } from "./ui/dialog";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  /** False keeps Esc, an outside click and the header X from closing it. */
  dismissible?: boolean;
}

/**
 * Radix returns focus to a Dialog.Trigger and to nothing else; these dialogs are opened by ordinary buttons
 * (Edit, New …). Remember what had focus when it opened and give it back when it closes (if it is still there).
 */
export function useReturnFocus(open: boolean) {
  const opener = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (open) opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }, [open]);
  return (event: Event) => {
    event.preventDefault();
    const el = opener.current;
    opener.current = null;
    if (el?.isConnected) el.focus();
  };
}

const HEADER = "flex shrink-0 flex-row items-center justify-between gap-3 border-b px-5 py-3.5 text-left";

/**
 * Focus trap, Esc, backdrop close and inert background come from Radix (via ui/dialog). The
 * header and footer stay put and only the body scrolls, so a long form can always be cancelled or saved.
 */
export function Dialog({ open, onClose, title, children, footer, dismissible = true }: Props) {
  const onOpenChange = (next: boolean) => (!next ? onClose() : undefined);
  const onCloseAutoFocus = useReturnFocus(open);
  const body = (
    <>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      {footer && <footer className="flex shrink-0 justify-end gap-2 border-t px-5 py-3">{footer}</footer>}
    </>
  );
  const close = (
    <Button type="button" variant="ghost" size="icon" className="size-8 text-muted-foreground" aria-label="Close">
      <XIcon aria-hidden="true" />
    </Button>
  );
  return (
    <UiDialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
        onCloseAutoFocus={onCloseAutoFocus}
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 p-0 sm:max-w-lg"
        {...(dismissible
          ? {}
          : {
              onEscapeKeyDown: (event: KeyboardEvent) => event.preventDefault(),
              onInteractOutside: (event: Event) => event.preventDefault(),
            })}
      >
        <header className={HEADER}>
          <DialogTitle className="m-0">{title}</DialogTitle>
          {dismissible && <DialogClose asChild>{close}</DialogClose>}
        </header>
        {body}
      </DialogContent>
    </UiDialog>
  );
}
