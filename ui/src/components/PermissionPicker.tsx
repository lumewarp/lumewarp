import { useMemo } from "react";

import type { ModelAccess, ModelGroup } from "../api";
import { useCatalog, useModelGroups } from "../hooks";
import { CatalogMultiSelect, type PickerGroup } from "./CatalogPicker";
import type { ModelGrant } from "./keyModels";
import { HelpTip } from "./ui/help-tip";
import { Label } from "./ui/label";
import { RadioGroup, RadioGroupItem } from "./ui/radio-group";


/**
 * The picker's groups and "via <group>" map for one level. A group with any member outside `scope` is
 * disabled (it could not be granted whole); one already chosen stays enabled so it can be unchecked.
 */
export function useGroupChoices(scope: string[] | null, chosen: string[], enabled = true) {
  const groups = useModelGroups(enabled);
  const catalog = useCatalog(enabled);
  return useMemo(() => {
    const catalogNames = new Set((catalog.data ?? []).map((entry) => entry.name));
    const allowed = scope ? new Set(scope) : null;
    const list: ModelGroup[] = groups.data ?? [];
    const picker: PickerGroup[] = list.map((group) => {
      const outside = allowed ? group.members.filter((member) => !allowed.has(member.name)).map((member) => member.name) : [];
      const blocked = outside.length > 0 && !chosen.includes(group.name);
      return {
        name: group.name,
        disabled: blocked,
        reason: blocked ? `Not offered: ${outside.join(", ")} ${outside.length === 1 ? "is" : "are"} outside what the level above allows.` : undefined,
      };
    });
    const covered: Record<string, string> = {};
    for (const group of list) {
      if (!chosen.includes(group.name)) continue;
      for (const member of group.members) covered[member.name] ??= group.name;
    }
    return { picker, covered, groupNames: new Set(list.map((group) => group.name)), catalogNames };
  }, [groups.data, catalog.data, scope, chosen]);
}

/** One list for the CatalogMultiSelect: groups come back out of it by name. */
function useGrantList(grant: ModelGrant, onChange: (grant: ModelGrant) => void, scope: string[] | null, enabled = true) {
  const choices = useGroupChoices(scope, grant.groups, enabled);
  const { groupNames, catalogNames } = choices;
  // A group whose name is also a catalog name is told apart by the catalog winning; the group page refuses such names.
  const isGroup = (name: string) => groupNames.has(name) && !catalogNames.has(name);
  const selected = [...grant.names, ...grant.groups];
  const change = (next: string[]) => {
    const groups = next.filter((name) => isGroup(name) || (grant.groups.includes(name) && !catalogNames.has(name)));
    onChange({ names: next.filter((name) => !groups.includes(name)), groups });
  };
  return { choices, selected, change };
}

interface Props {
  isTeam?: boolean;
  access: ModelAccess;
  onAccess: (a: ModelAccess) => void;
  selected: string[];
  onSelected: (names: string[]) => void;
  /** Groups granted at this level. */
  selectedGroups: string[];
  onSelectedGroups: (groups: string[]) => void;
  /** Names the level above allows (its effective models); null = every name. Lower levels may only narrow it. */
  scope: string[] | null;
  /** What "the level above" is called in help texts. */
  parentName?: string;
  error?: string;
}

/**
 * Who may call what, by name: team = All | Selected, member / service account = Inherit | Selected.
 * Selected shows every model, alias, router and model group the level above allows (CatalogMultiSelect).
 */
export function PermissionPicker({ isTeam, access, onAccess, selected, onSelected, selectedGroups, onSelectedGroups, scope, parentName = isTeam ? "the org" : "the team", error }: Props) {
  const first = isTeam ? ({ value: "all", label: "All", hint: "Every name, including ones added later." } as const) : ({ value: "inherit", label: "Inherit", hint: `Whatever ${parentName} allows.` } as const);
  const { choices, selected: list, change } = useGrantList({ names: selected, groups: selectedGroups }, (grant) => {
    onSelected(grant.names);
    onSelectedGroups(grant.groups);
  }, scope, access === "list");
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" data-testid="permission-picker">
      <legend className="mb-1 text-sm font-semibold">
        <span className="flex items-center gap-1">
          Model access
          <HelpTip label="Model access" text={access === "list" ? `Only the names and groups ticked below, within what ${parentName} allows; none ticked denies everything.` : first.hint} />
        </span>
      </legend>
      <RadioGroup value={access === "list" ? "list" : first.value} onValueChange={(v) => onAccess(v as ModelAccess)} aria-label="Model access" className="flex flex-wrap gap-x-5 gap-y-2" aria-invalid={!!error}>
        {[first, { value: "list", label: "Selected", hint: "" } as const].map((o) => (
          <div key={o.value} className="flex items-center gap-2">
            <RadioGroupItem value={o.value} id={`access-${o.value}`} />
            <Label htmlFor={`access-${o.value}`} className="cursor-pointer font-normal">
              {o.label}
            </Label>
          </div>
        ))}
      </RadioGroup>
      {access === "list" && (
        <div className="rounded-md border p-3" data-testid="permission-options">
          <CatalogMultiSelect
            label="Allowed names"
            idPrefix="perm"
            scope={scope}
            selected={list}
            onChange={change}
            groups={choices.picker}
            covered={choices.covered}
            outsideText={`(outside ${parentName}'s scope)`}
            emptyText={`No models, aliases or routers ${isTeam ? "exist yet" : "are available here"}.`}
          />
        </div>
      )}
      {error && (
        <p className="text-[13px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </fieldset>
  );
}

/** The key's own models: inherit the owner's, or narrow to a list (names and groups) within the owner's set. */
export function KeyModelPicker({
  access,
  onAccess,
  grant,
  onGrant,
  scope,
  error,
}: {
  access: "inherit" | "list";
  onAccess: (access: "inherit" | "list") => void;
  grant: ModelGrant;
  onGrant: (grant: ModelGrant) => void;
  /** The owner's effective models; null = every name. */
  scope: string[] | null;
  error?: string;
}) {
  const { choices, selected, change } = useGrantList(grant, onGrant, scope, access === "list");
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" data-testid="key-model-picker">
      <legend className="mb-1 text-sm font-semibold">
        <span className="flex items-center gap-1">
          Models
          <HelpTip label="Models" text={access === "list" ? "Only what is ticked, and never more than the owner may call." : "Whatever the owner may call, including later changes."} />
        </span>
      </legend>
      <RadioGroup value={access} onValueChange={(v) => onAccess(v as "inherit" | "list")} aria-label="Key models" className="flex flex-wrap gap-x-5 gap-y-2" aria-invalid={!!error}>
        {(
          [
            ["inherit", "Inherit"],
            ["list", "Selected"],
          ] as const
        ).map(([value, label]) => (
          <div key={value} className="flex items-center gap-2">
            <RadioGroupItem value={value} id={`key-models-${value}`} />
            <Label htmlFor={`key-models-${value}`} className="cursor-pointer font-normal">
              {label}
            </Label>
          </div>
        ))}
      </RadioGroup>
      {access === "list" && (
        <div className="rounded-md border p-3" data-testid="key-model-options">
          <CatalogMultiSelect
            label="Key models"
            idPrefix="keymodel"
            scope={scope}
            selected={selected}
            onChange={change}
            groups={choices.picker}
            covered={choices.covered}
            outsideText="(outside the owner's models)"
            emptyText="The owner can call no models."
          />
        </div>
      )}
      {error && (
        <p className="text-[13px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </fieldset>
  );
}

export { blankKeyModels, KeyModels, keyModelsBody, keyModelsFrom, type KeyModelState, type ModelGrant } from "./keyModels";
