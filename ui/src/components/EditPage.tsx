import { Link, useBlocker } from "@tanstack/react-router";
import { ArrowLeftIcon } from "lucide-react";
import { useId, useRef, useState, type FormEvent, type ReactNode } from "react";

import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "./ui/alert-dialog";
import { Alert, AlertDescription } from "./ui/alert";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

/**
 * A create/edit form on its own page: back link, sections, and a Save bar. Leaving with unsaved changes asks
 * first (in-app navigation and tab close). `onSubmit` returns false to stay (validation failed) and throws to
 * show a form-level error; on success the caller navigates away (the blocker is off while saving).
 */
export function EditPage({
  title,
  description,
  back,
  backLabel,
  dirty,
  submitLabel = "Save",
  onSubmit,
  actions,
  children,
}: {
  title: string;
  description?: string;
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
          <h1>{title}</h1>
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

/** One titled block of fields on an EditPage. */
export function Section({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent className="flex flex-col gap-3.5">{children}</CardContent>
    </Card>
  );
}
