import { Button } from "../ui/button";

/** Buttons that are one choice of several, e.g. the metric or the chart type. */
export function Toggle<T extends string>({ label, testId, value, options, onChange }: { label: string; testId: string; value: T; options: { value: T; label: string }[]; onChange: (value: T) => void }) {
  return (
    <div role="group" aria-label={label} data-testid={testId} className="inline-flex shrink-0 gap-0.5 rounded-md border bg-muted p-0.5">
      {options.map((option) => (
        <Button key={option.value} type="button" size="sm" variant={value === option.value ? "default" : "ghost"} aria-pressed={value === option.value} onClick={() => onChange(option.value)}>
          {option.label}
        </Button>
      ))}
    </div>
  );
}
