import { XIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Dialog as DialogPrimitive } from "radix-ui";

import { cn } from "../lib/utils";
import { Button } from "./ui/button";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  /** `drawer` slides in from the right. */
  variant?: "dialog" | "drawer" | "wide-drawer";
}

/** Radix dialog: focus trap, Esc, backdrop close and inert background. */
export function Dialog({ open, onClose, title, children, footer, variant = "dialog" }: Props) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => (!next ? onClose() : undefined)}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/50 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className={cn(
            "fixed z-50 flex flex-col border bg-background shadow-xl outline-none data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0",
            variant === "drawer" || variant === "wide-drawer"
              ? cn("inset-y-0 right-0 h-full w-full border-l data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right", variant === "wide-drawer" ? "max-w-xl" : "max-w-md")
              : "top-1/2 left-1/2 max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-xl data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
          )}
        >
          <header className="flex shrink-0 items-center justify-between gap-3 border-b px-5 py-3.5">
            <DialogPrimitive.Title className="text-base font-semibold">{title}</DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <Button type="button" variant="ghost" size="icon" className="size-8 text-muted-foreground" aria-label="Close">
                <XIcon aria-hidden="true" />
              </Button>
            </DialogPrimitive.Close>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && <footer className="flex shrink-0 justify-end gap-2 border-t px-5 py-3">{footer}</footer>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
