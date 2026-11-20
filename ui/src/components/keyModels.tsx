import type { ModelAccess } from "../api";

// Key model helpers without the picker: eager pages (My keys) import these, and the picker
// itself (`PermissionPicker`) stays in a lazy chunk.

/** Granted names and granted groups, kept apart in the API and shown in one list. */
export interface ModelGrant {
  names: string[];
  groups: string[];
}

export type KeyModelState = { access: "inherit" | "list"; grant: ModelGrant };
export const blankKeyModels = (): KeyModelState => ({ access: "inherit", grant: { names: [], groups: [] } });
export const keyModelsFrom = (key: { model_access: ModelAccess; allowed_models: string[]; allowed_groups: string[] }): KeyModelState => ({
  access: key.model_access === "list" ? "list" : "inherit",
  grant: { names: [...key.allowed_models], groups: [...key.allowed_groups] },
});
/** Request fields for a key; lists are only sent with "list" (the API refuses them on inherit). */
export const keyModelsBody = (state: KeyModelState) =>
  state.access === "list"
    ? { model_access: "list" as const, allowed_models: state.grant.names, allowed_groups: state.grant.groups }
    : { model_access: "inherit" as const };

/** What a key can call: the owner's models for an inheriting key, else its grants within the owner's set. */
export function KeyModels({ keyRef }: { keyRef: { model_access: ModelAccess; granted_models: string[]; owner_models?: string[] | null } }) {
  if (keyRef.model_access !== "list") return <span className="text-muted-foreground">Inherits owner's</span>;
  const owner = keyRef.owner_models ? new Set(keyRef.owner_models) : null;
  const names = keyRef.granted_models.filter((name) => !owner || owner.has(name));
  return (
    <span title={names.join(", ")} data-testid="key-models">
      {names.length} model{names.length === 1 ? "" : "s"}
    </span>
  );
}
