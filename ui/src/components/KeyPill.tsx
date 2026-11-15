import { KeyRoundIcon } from "lucide-react";

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

function Row({ term, value, mono }: { term: string; value?: string | null; mono?: boolean }) {
  if (!value) return null;
  return (
    <>
      <dt className="text-background/60">{term}</dt>
      <dd className={mono ? "font-mono [overflow-wrap:anywhere]" : "[overflow-wrap:anywhere]"}>{value}</dd>
    </>
  );
}

/**
 * Every API key shown in the Admin UI: a pill with the key name; hovering or focusing it lists org, team,
 * owner, name, prefix and id. A null `keyRef` is a key whose row is gone; `fallbackId` is all that is left.
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
      <TooltipContent className="max-w-[min(20rem,calc(100vw-2rem))]">
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-left">
          {keyRef ? (
            <>
              <Row term="Org" value={keyRef.org_name} />
              <Row term="Team" value={keyRef.team_name} />
              {keyRef.service_account_name ? (
                <Row term="Service account" value={keyRef.service_account_name} />
              ) : (
                <Row term="Owner" value={owner} />
              )}
              <Row term="Name" value={keyRef.name} />
              <Row term="Prefix" value={keyRef.display_prefix ? `${keyRef.display_prefix}…` : null} mono />
              <Row term="Id" value={keyRef.id} mono />
            </>
          ) : (
            <Row term="Id" value={fallbackId} mono />
          )}
        </dl>
      </TooltipContent>
    </Tooltip>
  );
}
