import { Search } from "lucide-react";

import { Input } from "./ui/input";

/** Search box above a list. `label` names what is matched, for screen readers and the placeholder. */
export function SearchBox({ value, onChange, label, placeholder }: { value: string; onChange: (v: string) => void; label: string; placeholder?: string }) {
  return (
    <div className="relative mb-4 max-w-md">
      <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      <Input type="search" className="pl-9" aria-label={label} placeholder={placeholder ?? label} value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
