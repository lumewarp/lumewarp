import { CheckIcon, CopyIcon } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/utils";
import { Button } from "./button";

/**
 * A monospace snippet with a copy button. `label` names it for screen readers and its copy button;
 * the text stays selectable when the clipboard is blocked.
 */
function CodeBlock({ code, label, className }: { code: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      /* clipboard blocked: the text is still selectable */
    }
  };
  return (
    <div className="min-w-0 overflow-hidden rounded-md border bg-secondary">
      <div className="flex justify-end border-b px-1.5 py-1">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 gap-1 px-1.5 text-xs text-muted-foreground"
          aria-label={copied ? `${label} copied` : `Copy ${label}`}
          onClick={copy}
        >
          {copied ? <CheckIcon aria-hidden="true" /> : <CopyIcon aria-hidden="true" />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre
        tabIndex={0}
        aria-label={label}
        className={cn("overflow-x-auto px-3 py-2.5 font-mono text-xs leading-relaxed", className)}
      >
        <code>{code}</code>
      </pre>
    </div>
  );
}

export { CodeBlock };
