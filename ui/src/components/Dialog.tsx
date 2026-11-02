import { XIcon } from "lucide-react";
import { useLayoutEffect, useRef, type ReactNode } from "react";

import { cn } from "../lib/utils";
import { Button } from "./ui/button";
import { Dialog as UiDialog, DialogClose, DialogContent, DialogTitle } from "./ui/dialog";
import { Sheet, SheetClose, SheetContent, SheetTitle } from "./ui/sheet";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  /** `drawer` slides in from the right. */
  variant?: "dialog" | "drawer" | "wide-drawer";
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
 * Focus trap, Esc, backdrop close and inert background come from Radix (via ui/dialog and ui/sheet). The
 * header and footer stay put and only the body scrolls, so a long form can always be cancelled or saved.
 */
export function Dialog({ open, onClose, title, children, footer, variant = "dialog" }: Props) {
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
  if (variant === "dialog") {
    return (
      <UiDialog open={open} onOpenChange={onOpenChange}>
        <DialogContent aria-describedby={undefined} onCloseAutoFocus={onCloseAutoFocus} showCloseButton={false} className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 p-0 sm:max-w-lg">
          <header className={HEADER}>
            <DialogTitle className="m-0">{title}</DialogTitle>
            <DialogClose asChild>{close}</DialogClose>
          </header>
          {body}
        </DialogContent>
      </UiDialog>
    );
  }
  // Sheet's own default is 75% wide and max-w-sm: a drawer is the full width on a phone and md / xl from sm up.
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent aria-describedby={undefined} onCloseAutoFocus={onCloseAutoFocus} showCloseButton={false} side="right" className={cn("w-full gap-0 p-0 data-[state=closed]:duration-200 data-[state=open]:duration-200", variant === "wide-drawer" ? "sm:max-w-xl" : "sm:max-w-md")}>
        <header className={HEADER}>
          <SheetTitle className="m-0 text-base">{title}</SheetTitle>
          <SheetClose asChild>{close}</SheetClose>
        </header>
        {body}
      </SheetContent>
    </Sheet>
  );
}
