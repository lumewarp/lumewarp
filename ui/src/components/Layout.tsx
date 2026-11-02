import { useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useNavigate } from "@tanstack/react-router";
import {
  BarChart3,
  Boxes,
  CornerDownRight,
  History,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  Monitor,
  Moon,
  Radio,
  Route,
  ScrollText,
  Server,
  Settings,
  ShieldCheck,
  Sun,
  Users,
} from "lucide-react";
import { useState } from "react";

import { api } from "../api";
import { clearCsrfToken } from "../auth";
import type { Me } from "../api";
import { useCan, useMe } from "../hooks";
import { cn } from "../lib/utils";
import { useTheme } from "../theme";
import { Badge } from "./Bits";
import { useToast } from "./Toast";
import { Button } from "./ui/button";

const NAV = [
  { group: null, items: [{ to: "/", label: "Overview", icon: LayoutDashboard }] },
  {
    group: "Access",
    items: [
      { to: "/keys", label: "My keys", icon: KeyRound },
      { to: "/teams", label: "Teams", icon: Users },
      { to: "/users", label: "Users", icon: ShieldCheck, superOnly: true },
    ],
  },
  {
    group: "Routing",
    items: [
      { to: "/upstreams", label: "Upstreams", icon: Server },
      { to: "/models", label: "Models", icon: Boxes },
      { to: "/aliases", label: "Aliases", icon: CornerDownRight },
      { to: "/routers", label: "Routers", icon: Route },
    ],
  },
  {
    group: "Activity",
    items: [
      { to: "/usage", label: "Usage", icon: BarChart3 },
      { to: "/audit", label: "Audit log", icon: ScrollText },
    ],
  },
  {
    group: "Data plane",
    items: [
      { to: "/gateways", label: "Gateways", icon: Radio, superOnly: true },
      { to: "/config", label: "Config versions", icon: History, superOnly: true },
      { to: "/settings", label: "Settings", icon: Settings },
    ],
  },
] as const;

function roleLabel(me: Me) {
  if (me.kind === "break_glass") return "break-glass";
  if (me.is_admin) return "admin";
  if (me.teams.some((team) => team.role === "admin")) return "team admin";
  return me.teams.length ? "member" : "no team access";
}

const THEME_LABEL = { system: "Auto", light: "Light", dark: "Dark" } as const;
const THEME_ICON = { system: Monitor, light: Sun, dark: Moon } as const;

export function Layout() {
  const me = useMe();
  const perms = useCan();
  const { theme, cycle } = useTheme();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [navOpen, setNavOpen] = useState(false);
  const ThemeIcon = THEME_ICON[theme];

  const logout = async () => {
    try {
      await api.logoutSession();
    } catch (error) {
      toast("error", error instanceof Error ? error.message : "Unable to sign out");
      return;
    }
    clearCsrfToken();
    qc.clear();
    void navigate({ to: "/login" });
  };

  return (
    <div className="flex min-h-screen bg-background">
      <a
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[100] focus:rounded-md focus:border focus:bg-background focus:px-3 focus:py-2 focus:shadow"
        href="#main"
      >
        Skip to content
      </a>
      {navOpen && (
        <button
          type="button"
          aria-hidden="true"
          tabIndex={-1}
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          onClick={() => setNavOpen(false)}
        />
      )}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-60 flex-col border-r bg-sidebar px-3 pt-16 pb-4 text-sidebar-foreground shadow-lg transition-transform duration-200 md:sticky md:top-0 md:z-30 md:h-screen md:translate-x-0 md:pt-4 md:shadow-none",
          navOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center gap-2.5 px-2 pb-5 text-base font-bold">
          <span className="brand-mark" aria-hidden="true" />
          Lumewarp
        </div>
        <nav aria-label="Main" className="flex min-h-0 flex-col gap-1 overflow-y-auto">
          {NAV.map((section) => {
            const items = section.items.filter((n) => !("superOnly" in n) || perms.isSuper);
            if (!items.length) return null;
            return (
              <div key={section.group ?? "top"} role="group" aria-label={section.group ?? undefined} className="flex flex-col gap-1">
                {section.group && <span className="mt-3 px-2.5 pb-0.5 text-[11px] font-semibold tracking-wider text-sidebar-foreground/55 uppercase">{section.group}</span>}
                {items.map((n) => {
                  const Icon = n.icon;
                  return (
                    <Link
                      key={n.to}
                      to={n.to}
                      className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring/50 data-[status=active]:bg-sidebar-primary data-[status=active]:text-sidebar-primary-foreground data-[status=active]:hover:bg-sidebar-primary data-[status=active]:hover:text-sidebar-primary-foreground"
                      activeProps={{
                        "aria-current": "page",
                      }}
                      activeOptions={{ exact: n.to === "/" }}
                      onClick={() => setNavOpen(false)}
                    >
                      <Icon className="size-4 opacity-80" aria-hidden="true" />
                      {n.label}
                    </Link>
                  );
                })}
              </div>
            );
          })}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-50 flex items-center gap-2 border-b bg-background/95 px-4 py-2.5 backdrop-blur md:px-6">
          <Button
            variant="outline"
            size="icon"
            className="size-8 md:hidden"
            aria-label="Toggle navigation"
            aria-expanded={navOpen}
            onClick={() => setNavOpen((o) => !o)}
          >
            <Menu className="size-4" aria-hidden="true" />
          </Button>
          <span className="flex-1" />
          <Button variant="outline" size="sm" onClick={cycle} title="Cycle theme">
            <ThemeIcon className="size-4" aria-hidden="true" />
            <span>Theme: {THEME_LABEL[theme]}</span>
          </Button>
          {me.data && (
            <span className="principal flex min-w-0 items-center gap-2" title={me.data.email ?? undefined} data-testid="principal">
              <span className="hidden max-w-[180px] truncate sm:inline">{me.data.display_name || me.data.email || "Bootstrap admin"}</span>
              <Badge tone="info">{roleLabel(me.data)}</Badge>
            </span>
          )}
          <Button variant="outline" size="sm" onClick={() => void logout()}>
            <LogOut className="size-4 sm:hidden" aria-hidden="true" />
            <span className="sr-only sm:not-sr-only">Sign out</span>
          </Button>
        </header>
        <main id="main" className="mx-auto w-full max-w-[1200px] flex-1 px-4 py-6 outline-none md:px-6" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
