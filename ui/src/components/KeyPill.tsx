import { BotIcon, KeyRoundIcon, UserIcon } from "lucide-react";

import { Badge } from "./ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";

/** What the pill needs of a key; KeyOut, KeyRefOut and an adapted PlaygroundKey all fit. */
export type KeyLike = {
  id: string;
  name: string;
  display_prefix: string;
  team_name?: string | null;
  org_name?: string | null;
  owner_kind?: string | null;
  service_account_name?: string | null;
};

/**
 * Every API key shown in the Admin UI: a pill with the key name. Hovering or focusing it shows two lines:
 * where the key lives (org › team · owner or service account), then the `lw-` prefix and the id. A null `keyRef` is a key whose row is gone; `fallbackId` is all that is left.
 */
export function KeyPill({
  keyRef,
  owner,
  fallbackId,
  tooltip = true,
}: {
  keyRef: KeyLike | null | undefined;
  /** The owning user's display name or email, when the page knows it. */
  owner?: string | null;
  fallbackId?: string | null;
  tooltip?: boolean;
}) {
  const name = keyRef?.name ?? "Deleted key";
  const pill = (
    <Badge
      variant="muted"
      className="max-w-full"
      data-testid="key-pill"
      tabIndex={tooltip ? 0 : undefined}
    >
      <KeyRoundIcon aria-hidden="true" />
      <span className="truncate">{name}</span>
    </Badge>
  );
  if (!tooltip) return pill;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{pill}</TooltipTrigger>
      <TooltipContent className="max-w-[min(24rem,calc(100vw-2rem))]">
        <div className="grid gap-1 text-left">
          {keyRef ? (
            <div className="flex flex-wrap items-center gap-x-1.5 [overflow-wrap:anywhere]">
              {keyRef.org_name && <span className="text-background/60">{keyRef.org_name} ›</span>}
              <span className="font-semibold">{keyRef.team_name ?? "No team"}</span>
              {(keyRef.service_account_name || owner) && (
                <span className="inline-flex items-center gap-1 text-background/80">
                  <span aria-hidden="true" className="text-background/40">·</span>
                  {keyRef.service_account_name ? (
                    <BotIcon aria-label="Service account" className="size-3" />
                  ) : (
                    <UserIcon aria-label="Owner" className="size-3" />
                  )}
                  {keyRef.service_account_name ?? owner}
                </span>
              )}
            </div>
          ) : (
            <div className="font-semibold">This key no longer exists</div>
          )}
          <div className="flex flex-wrap items-baseline gap-x-2 font-mono [overflow-wrap:anywhere]">
            {keyRef?.display_prefix && (
              <span className="font-semibold" data-testid="key-pill-prefix">
                {keyRef.display_prefix}…
              </span>
            )}
            <span className="text-[0.6875rem] text-background/60">{keyRef?.id ?? fallbackId}</span>
          </div>
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
