import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

import { SecretDialog } from "../pages/KeyDialogs";

export type PendingSecret = { value: string; title: string; note?: string };
type ShowSecret = (secret: PendingSecret) => void;
type SecretContextValue = { show: ShowSecret; clear: () => void };

const SecretContext = createContext<SecretContextValue | null>(null);

function useSecretContext() {
  const value = useContext(SecretContext);
  if (!value) throw new Error("useShowSecret and useClearSecrets must be used inside SecretProvider");
  return value;
}

/** The one way a flow shows a one-time secret. Throws outside SecretProvider. */
export function useShowSecret() {
  return useSecretContext().show;
}

/**
 * Explicit sign-out: empties the queue and advances the epoch, so a `show` handed out before the
 * sign-out (a create still in flight) is dropped instead of appearing on the login page.
 */
export function useClearSecrets() {
  return useSecretContext().clear;
}

/**
 * One dialog for every one-time secret, at the app root above the router and every route guard. A
 * background 401, a route change or a page unmount cannot take it away; an explicit sign-out calls
 * `clear`. A second secret arriving while one is open is queued and shown after the first is closed.
 */
export function SecretProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<PendingSecret[]>([]);
  const [epoch, setEpoch] = useState(0);
  const epochRef = useRef(0);
  const show = useCallback(
    (item: PendingSecret) => {
      // The callback captured the epoch of the render that handed it out; a sign-out since then wins.
      if (epochRef.current !== epoch) return;
      setQueue((items) => [...items, item]);
    },
    [epoch],
  );
  const clear = useCallback(() => {
    epochRef.current += 1;
    setEpoch(epochRef.current);
    setQueue([]);
  }, []);
  const close = useCallback(() => setQueue((items) => items.slice(1)), []);
  const value = useMemo(() => ({ show, clear }), [show, clear]);
  const current = queue[0] ?? null;
  return (
    <SecretContext.Provider value={value}>
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
