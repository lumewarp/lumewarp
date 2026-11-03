import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

import { SecretDialog } from "../pages/KeyDialogs";

export type PendingSecret = { value: string; title: string; note?: string };
type ShowSecret = (secret: PendingSecret) => void;

const SecretContext = createContext<ShowSecret | null>(null);

/** The one way a flow shows a one-time secret. It is a no-op only outside a signed-in layout. */
export function useShowSecret() {
  const show = useContext(SecretContext);
  if (!show) throw new Error("useShowSecret must be used inside SecretProvider");
  return show;
}

/**
 * One dialog for every one-time secret, above the route guards. A background access check, a route change
 * or a page unmount cannot take it away; signing out unmounts the provider. A second secret arriving while
 * one is open is queued and shown after the first is closed, never silently replacing it.
 */
export function SecretProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<PendingSecret[]>([]);
  const show = useCallback((item: PendingSecret) => setQueue((items) => [...items, item]), []);
  const close = useCallback(() => setQueue((items) => items.slice(1)), []);
  const current = queue[0] ?? null;
  return (
    <SecretContext.Provider value={show}>
      {children}
      <SecretDialog
        secret={current?.value ?? null}
        title={current?.title ?? ""}
        note={current?.note}
        onClose={close}
      />
    </SecretContext.Provider>
  );
}
