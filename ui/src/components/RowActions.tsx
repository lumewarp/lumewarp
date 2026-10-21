import type { ReactNode } from "react";

/** Actions the principal cannot perform are disabled with an explanation; the server stays the authority. */
export function RowActions({ onEdit, onDelete, canEdit = true, canDelete = true, extra }: { onEdit?: () => void; onDelete?: () => void; canEdit?: boolean; canDelete?: boolean; extra?: ReactNode }) {
  return (
    <div className="row-actions">
      {extra}
      {onEdit && (
        <button className="btn btn-sm" disabled={!canEdit} title={canEdit ? undefined : "Not permitted for your role"} onClick={onEdit}>
          Edit
        </button>
      )}
      {onDelete && (
        <button className="btn btn-sm btn-danger-ghost" disabled={!canDelete} title={canDelete ? undefined : "Not permitted for your role"} onClick={onDelete}>
          Delete
        </button>
      )}
    </div>
  );
}

export function CreateButton({ onClick, children, can = true }: { onClick: () => void; children: ReactNode; can?: boolean }) {
  return (
    <button className="btn btn-primary" disabled={!can} title={can ? undefined : "Not permitted for your role"} onClick={onClick}>
      {children}
    </button>
  );
}
