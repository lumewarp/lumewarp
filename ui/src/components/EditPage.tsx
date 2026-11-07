import { Link, useBlocker } from "@tanstack/react-router";
import { ArrowLeftIcon } from "lucide-react";
import { Children, useId, useRef, useState, type FormEvent, type ReactNode } from "react";

import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "./ui/alert-dialog";
import { Alert, AlertDescription } from "./ui/alert";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { HelpTip } from "./ui/help-tip";
import { Switch } from "./ui/switch";

/**
 * A create/edit form on its own page: back link, sections, and a Save bar. Leaving with unsaved changes asks
 * first (in-app navigation and tab close). `onSubmit` returns false to stay (validation failed) and throws to
 * show a form-level error; on success the caller navigates away (the blocker is off while saving).
 */
export function EditPage({
  title,
  description,
  hint,
  back,
  backLabel,
  dirty,
  submitLabel = "Save",
  onSubmit,
  actions,
  children,
}: {
  title: string;
  /** Live subtitle (e.g. what the form currently points at); static help goes in `hint`. */
  description?: string;
  hint?: string;
  back: string;
  backLabel: string;
  dirty: boolean;
  submitLabel?: string;
  onSubmit: () => Promise<boolean | void>;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const formId = useId();
  // A ref, not state: the caller navigates from inside onSubmit, before a re-render could update a closure.
  const saving = useRef(false);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const blocker = useBlocker({ shouldBlockFn: () => dirtyRef.current && !saving.current, enableBeforeUnload: () => dirtyRef.current && !saving.current, withResolver: true });
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    saving.current = true;
    try {
      if ((await onSubmit()) === false) saving.current = false;
    } catch (err) {
      saving.current = false;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 pb-24">
      <div>
        <Button asChild variant="ghost" size="sm" className="-ml-2 text-muted-foreground">
          <Link to={back}>
            <ArrowLeftIcon aria-hidden="true" />
            {backLabel}
          </Link>
        </Button>
      </div>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1>{title}</h1>
            {hint && <HelpTip text={hint} label={title} />}
          </div>
          {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      <form id={formId} onSubmit={submit} noValidate className="flex flex-col gap-5">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {children}
      </form>
      <div className="sticky bottom-0 -mx-4 flex justify-end gap-2 border-t bg-background/95 px-4 py-3 backdrop-blur">
        <Button asChild variant="outline">
          <Link to={back}>Cancel</Link>
        </Button>
        <Button type="submit" form={formId} disabled={busy}>
          {busy ? "Saving…" : submitLabel}
        </Button>
      </div>
      <AlertDialog open={blocker.status === "blocked"}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard changes?</AlertDialogTitle>
            <AlertDialogDescription>Your changes on this page have not been saved.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => blocker.reset?.()}>Keep editing</AlertDialogCancel>
            <Button variant="destructive" onClick={() => blocker.proceed?.()}>
              Discard
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/**
 * One titled block of fields on an EditPage. `hint` is static help behind a "?" next to the title. `toggle`
 * puts a switch at the right of the header that turns the block's settings on or off (the caller renders those
 * fields only while it is on); `actions` (e.g. a Test now button) sit left of it. The body is left out when
 * there is nothing to show.
 */
export function Section({
  title,
  hint,
  toggle,
  actions,
  children,
}: {
  title: string;
  hint?: string;
  toggle?: { label: string; checked: boolean; onChange: (on: boolean) => void };
  actions?: ReactNode;
  children?: ReactNode;
}) {
  const body = Children.toArray(children).length > 0;
  return (
    <Card className="gap-4">
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-1">
          <CardTitle>{title}</CardTitle>
          {hint && <HelpTip text={hint} label={title} />}
        </div>
        {(actions || toggle) && (
          <div className="flex shrink-0 items-center gap-3">
            {actions}
            {toggle && <Switch aria-label={toggle.label} checked={toggle.checked} onCheckedChange={toggle.onChange} />}
          </div>
        )}
      </CardHeader>
      {body && <CardContent className="flex flex-col gap-3.5">{children}</CardContent>}
    </Card>
  );
}
