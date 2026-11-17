import type { ModelAccess } from "../api";
import { CatalogMultiSelect } from "./CatalogPicker";
import { HelpTip } from "./ui/help-tip";
import { Label } from "./ui/label";
import { RadioGroup, RadioGroupItem } from "./ui/radio-group";

interface Props {
  isTeam?: boolean;
  access: ModelAccess;
  onAccess: (a: ModelAccess) => void;
  selected: string[];
  onSelected: (names: string[]) => void;
  /** Names the parent level allows; null = every name. Members and service accounts may only narrow it. */
  scope: string[] | null;
  error?: string;
}

/**
 * Who may call what, by name: team = All | Selected, member / service account = Inherit | Selected.
 * Selected shows every model, alias and router the parent level allows (CatalogMultiSelect).
 */
export function PermissionPicker({ isTeam, access, onAccess, selected, onSelected, scope, error }: Props) {
  const first = isTeam ? ({ value: "all", label: "All", hint: "Every name, including ones added later." } as const) : ({ value: "inherit", label: "Inherit", hint: "Whatever the team allows." } as const);
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" data-testid="permission-picker">
      <legend className="mb-1 text-sm font-semibold">
        <span className="flex items-center gap-1">
          Model access
          <HelpTip label="Model access" text={access === "list" ? isTeam ? "Only the names ticked below." : "Only the names ticked below, within what the team allows; none ticked denies everything." : first.hint} />
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
            selected={selected}
            onChange={onSelected}
            outsideText="(outside the team's scope)"
            emptyText={`No models, aliases or routers ${isTeam ? "exist yet" : "are available in this team"}.`}
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
