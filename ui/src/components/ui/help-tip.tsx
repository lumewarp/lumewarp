import { CircleHelpIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "./button";
import { Tooltip, TooltipContent, TooltipTrigger } from "./tooltip";

/**
 * A "?" next to a label or title; the help text shows on hover and focus. The same text is kept
 * in the DOM (visually hidden) under `id`, so a control can point aria-describedby at it.
 */
function HelpTip({ text, id, className }: { text: string; id?: string; label?: string; className?: string }) {
  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button type="button" variant="ghost" size="icon" data-slot="help-tip" aria-label="Help" className={cn("size-5 rounded-full text-muted-foreground hover:text-foreground", className)}>
            <CircleHelpIcon className="size-3.5" aria-hidden="true" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>{text}</TooltipContent>
      </Tooltip>
      {id && (
        <span id={id} className="sr-only">
          {text}
        </span>
      )}
    </>
  );
}

export { HelpTip };
