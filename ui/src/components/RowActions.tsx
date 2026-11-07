import { ArchiveIcon, PencilIcon, Trash2Icon, UserMinusIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "./ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";

const DENIED = "Not permitted for your role";

const DESTRUCTIVE_ICON: Record<string, typeof Trash2Icon> = { Archive: ArchiveIcon, Remove: UserMinusIcon };

/**
 * An icon button with a tooltip: the label, or the reason when disabled. A disabled action stays focusable
 * (aria-disabled, the click is ignored) so keyboard users can reach the reason too.
 */
export function IconAction({
  label,
  icon: Icon,
  onClick,
  disabled,
  reason,
  className,
  busy,
}: {
  label: string;
  icon: typeof Trash2Icon;
  onClick?: () => void;
  disabled?: boolean;
  reason?: string;
  className?: string;
  busy?: boolean;
}) {
  const off = disabled || busy;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className={`size-8 aria-disabled:cursor-not-allowed aria-disabled:opacity-50 ${className ?? ""}`}
          aria-label={label}
          aria-disabled={off || undefined}
          onClick={off ? undefined : onClick}
        >
          <Icon aria-hidden="true" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{disabled ? (reason ?? DENIED) : label}</TooltipContent>
    </Tooltip>
  );
}

/** Actions the principal cannot perform are disabled with an explanation; the server stays the authority. */
export function RowActions({
  onEdit,
  onDelete,
  canEdit = true,
  canDelete = true,
  deleteLabel = "Delete",
  deleteReason,
  extra,
}: {
  onEdit?: () => void;
  onDelete?: () => void;
  canEdit?: boolean;
  canDelete?: boolean;
  /** A destructive action that keeps the row (archiving a team or an org). */
  deleteLabel?: string;
  deleteReason?: string;
  extra?: ReactNode;
}) {
  return (
    <div className="flex justify-end gap-1 whitespace-nowrap">
      {extra}
      {onEdit && <IconAction label="Edit" icon={PencilIcon} disabled={!canEdit} onClick={onEdit} />}
      {onDelete && (
        <IconAction
          label={deleteLabel}
          icon={DESTRUCTIVE_ICON[deleteLabel] ?? Trash2Icon}
          className="text-destructive hover:bg-destructive/10 hover:text-destructive"
          disabled={!canDelete}
          reason={deleteReason}
          onClick={onDelete}
        />
      )}
    </div>
  );
}

export function CreateButton({ onClick, children, can = true }: { onClick: () => void; children: ReactNode; can?: boolean }) {
  return (
    <Button disabled={!can} title={can ? undefined : DENIED} onClick={onClick}>
      {children}
    </Button>
  );
}
