import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { toast as sonner } from "sonner";

import { Toaster } from "./ui/sonner";

type Kind = "success" | "error";

const Ctx = createContext<(kind: Kind, message: string) => void>(() => {});
export const useToast = () => useContext(Ctx);

/** Follows the `.dark` class so toasts match an explicit light/dark choice, not only the OS. */
function useResolvedTheme(): "light" | "dark" {
  const [mode, setMode] = useState<"light" | "dark">(() =>
    document.documentElement.classList.contains("dark") ? "dark" : "light",
  );
  useEffect(() => {
    const root = document.documentElement;
    const sync = () => setMode(root.classList.contains("dark") ? "dark" : "light");
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return mode;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const theme = useResolvedTheme();
  const push = useCallback((kind: Kind, message: string) => {
    if (kind === "success") sonner.success(message);
    else sonner.error(message);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      <Toaster theme={theme} position="bottom-right" duration={5000} closeButton />
    </Ctx.Provider>
  );
}
