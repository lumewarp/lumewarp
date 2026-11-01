import { useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";
const KEY = "lumewarp.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

function stored(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark") return v;
  } catch {
    /* storage blocked */
  }
  return "system";
}

function prefersDark() {
  return window.matchMedia(DARK_QUERY).matches;
}

function apply(theme: Theme) {
  const root = document.documentElement;
  // `data-theme` is the public contract (tests and the pre-paint script read it);
  // `.dark` is what Tailwind and the shadcn tokens key off.
  root.classList.toggle("dark", theme === "dark" || (theme === "system" && prefersDark()));
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(stored);
  useEffect(() => {
    apply(theme);
    try {
      if (theme === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, theme);
    } catch {
      /* storage blocked */
    }
    if (theme !== "system") return;
    const media = window.matchMedia(DARK_QUERY);
    const sync = () => apply("system");
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, [theme]);
  const cycle = () => setTheme((t) => (t === "system" ? "light" : t === "light" ? "dark" : "system"));
  return { theme, cycle };
}
