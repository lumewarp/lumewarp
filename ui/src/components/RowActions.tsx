import type { ReactNode } from "react";

import { Button } from "./ui/button";

/** Actions the principal cannot perform are disabled with an explanation; the server stays the authority. */
export function RowActions({ onEdit, onDelete, canEdit = true, canDelete = true, extra }: { onEdit?: () => void; onDelete?: () => void; canEdit?: boolean; canDelete?: boolean; extra?: ReactNode }) {
  return (
    <div className="row-actions">
      {extra}
      {onEdit && (
        <Button variant="outline" size="sm" disabled={!canEdit} title={canEdit ? undefined : "Not permitted for your role"} onClick={onEdit}>
          Edit
        </Button>
      )}
      {onDelete && (
        <Button variant="ghost" size="sm" className="text-destructive hover:bg-destructive/10 hover:text-destructive" disabled={!canDelete} title={canDelete ? undefined : "Not permitted for your role"} onClick={onDelete}>
          Delete
        </Button>
      )}
    </div>
  );
}

export function CreateButton({ onClick, children, can = true }: { onClick: () => void; children: ReactNode; can?: boolean }) {
  return (
    <Button disabled={!can} title={can ? undefined : "Not permitted for your role"} onClick={onClick}>
      {children}
    </Button>
  );
}
