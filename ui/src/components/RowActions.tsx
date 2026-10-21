import { useCanWrite } from "../hooks";

export function RowActions({ onEdit, onDelete, extra }: { onEdit?: () => void; onDelete?: () => void; extra?: React.ReactNode }) {
  const can = useCanWrite();
  return (
    <div className="row-actions">
      {extra}
      {onEdit && (
        <button className="btn btn-sm" disabled={!can} onClick={onEdit}>
          Edit
        </button>
      )}
      {onDelete && (
        <button className="btn btn-sm btn-danger-ghost" disabled={!can} onClick={onDelete}>
          Delete
        </button>
      )}
    </div>
  );
}

export function CreateButton({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  const can = useCanWrite();
  return (
    <button className="btn btn-primary" disabled={!can} title={can ? undefined : "Read-only role"} onClick={onClick}>
      {children}
    </button>
  );
}
