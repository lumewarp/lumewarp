import { useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useNavigate } from "@tanstack/react-router";
import {
  BarChart3,
  Bot,
  Boxes,
  Building2,
  Cable,
  CornerDownRight,
  History,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  Monitor,
  Moon,
  Plug,
  Radio,
  Route,
  ScrollText,
  Server,
  Settings,
  ShieldCheck,
  Sun,
  Users,
  type LucideIcon,
} from "lucide-react";

import { api } from "../api";
import { clearCsrfToken } from "../auth";
import type { Me } from "../api";
import { hasAccess, useCan, useMe, type Access } from "../hooks";
import { useTheme } from "../theme";
import { AuthzNotice } from "../pages/Access";
import { useClearSecrets } from "./SecretProvider";
import { Badge } from "./Bits";
import { useToast } from "./Toast";
import { Button } from "./ui/button";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  useSidebar,
} from "./ui/sidebar";

type NavItem = { to: string; label: string; icon: LucideIcon; access?: Access; /** Hidden in the emergency session, which owns no keys. */ noBreakGlass?: boolean };

// My keys is first and for everyone. Admin pages are shown to an installation admin, or to a team admin
// when the page is about a team they can administer.
const NAV: { group: string | null; items: NavItem[] }[] = [
  {
    group: null,
    items: [
      { to: "/my-keys", label: "My keys", icon: KeyRound },
      { to: "/playground", label: "Playground", icon: MessageSquare, noBreakGlass: true },
      { to: "/", label: "Overview", icon: LayoutDashboard, access: "teamAdmin" },
    ],
  },
  {
    group: "Access",
    items: [
      { to: "/orgs", label: "Orgs", icon: Building2, access: "orgAdmin" },
      { to: "/teams", label: "Teams", icon: Users, access: "teamAdmin" },
      { to: "/users", label: "Users", icon: ShieldCheck, access: "installationAdmin" },
    ],
  },
  {
    group: "Routing",
    items: [
      { to: "/upstreams", label: "Upstreams", icon: Server, access: "installationAdmin" },
      { to: "/models", label: "Models", icon: Boxes, access: "installationAdmin" },
      { to: "/aliases", label: "Aliases", icon: CornerDownRight, access: "installationAdmin" },
      { to: "/routers", label: "Routers", icon: Route, access: "installationAdmin" },
    ],
  },
  {
    group: "MCP & A2A",
    items: [
      { to: "/mcp-servers", label: "MCP servers", icon: Cable, access: "installationAdmin" },
      { to: "/a2a-agents", label: "A2A agents", icon: Bot, access: "installationAdmin" },
    ],
  },
  {
    group: "Activity",
    items: [
      { to: "/usage", label: "Usage", icon: BarChart3 },
      { to: "/audit", label: "Audit log", icon: ScrollText, access: "teamAdmin" },
    ],
  },
  {
    group: "Data plane",
    items: [
      { to: "/gateways", label: "Gateways", icon: Radio, access: "installationAdmin" },
      { to: "/plugins", label: "Plugins", icon: Plug, access: "installationAdmin" },
      { to: "/config", label: "Config versions", icon: History, access: "installationAdmin" },
      { to: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

function roleLabel(me: Me) {
  if (me.kind === "break_glass") return "break-glass";
  if (me.installation_role === "proxy_admin") return "proxy admin";
  if (me.is_admin) return "admin";
  if (me.admin_orgs?.length) return "org admin";
  if (me.teams.some((team) => team.role === "admin")) return "team admin";
  return me.teams.length ? "member" : "no team access";
}

const THEME_LABEL = { system: "Auto", light: "Light", dark: "Dark" } as const;
const THEME_ICON = { system: Monitor, light: Sun, dark: Moon } as const;

function NavToggle() {
  const { openMobile, toggleSidebar } = useSidebar();
  return (
    <Button variant="outline" size="icon" className="size-8 md:hidden" aria-label="Toggle navigation" aria-expanded={openMobile} onClick={toggleSidebar}>
      <Menu className="size-4" aria-hidden="true" />
    </Button>
  );
}

function NavSidebar({ installationAdmin, orgAdmin, teamAdmin, breakGlass }: { installationAdmin: boolean; orgAdmin: boolean; teamAdmin: boolean; breakGlass: boolean }) {
  const { setOpenMobile } = useSidebar();
  const perms = { isInstallationAdmin: installationAdmin, isOrgAdmin: orgAdmin, isTeamAdmin: teamAdmin };
  return (
    <Sidebar>
      <SidebarHeader className="px-5 pt-4 pb-1">
        <div className="flex items-center gap-2.5 text-base font-bold">
          <span className="size-5 rounded-md bg-[linear-gradient(135deg,var(--primary),#22d3ee)]" aria-hidden="true" />
          Lumewarp
        </div>
      </SidebarHeader>
      <SidebarContent>
        <nav aria-label="Main" className="flex min-h-0 flex-col gap-1 overflow-y-auto">
          {NAV.map((section) => {
            const items = section.items.filter((n) => hasAccess(perms, n.access ?? "member") && !(breakGlass && n.noBreakGlass));
            if (!items.length) return null;
            return (
              <SidebarGroup key={section.group ?? "top"} role="group" aria-label={section.group ?? undefined}>
                {section.group && <SidebarGroupLabel>{section.group}</SidebarGroupLabel>}
                <SidebarGroupContent>
                  <SidebarMenu>
                    {items.map((n) => {
                      const Icon = n.icon;
                      return (
                        <SidebarMenuItem key={n.to}>
                          <SidebarMenuButton
                            asChild
                            className="text-sidebar-foreground/80 data-[status=active]:bg-sidebar-primary data-[status=active]:text-sidebar-primary-foreground data-[status=active]:hover:bg-sidebar-primary data-[status=active]:hover:text-sidebar-primary-foreground"
                          >
                            <Link to={n.to} activeProps={{ "aria-current": "page" }} activeOptions={{ exact: n.to === "/" }} onClick={() => setOpenMobile(false)}>
                              <Icon aria-hidden="true" />
                              {n.label}
                            </Link>
                          </SidebarMenuButton>
                        </SidebarMenuItem>
                      );
                    })}
                  </SidebarMenu>
                </SidebarGroupContent>
              </SidebarGroup>
            );
          })}
        </nav>
      </SidebarContent>
    </Sidebar>
  );
}

export function Layout() {
  const me = useMe();
  const perms = useCan();
  const { theme, cycle } = useTheme();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const clearSecrets = useClearSecrets();
  const ThemeIcon = THEME_ICON[theme];

  const logout = async () => {
    try {
      await api.logoutSession();
    } catch (error) {
      toast("error", error instanceof Error ? error.message : "Unable to sign out");
      return;
    }
    clearSecrets();
    clearCsrfToken();
    qc.clear();
    void navigate({ to: "/login" });
  };

  return (
    <SidebarProvider className="bg-background">
      <a
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[100] focus:rounded-md focus:border focus:bg-background focus:px-3 focus:py-2 focus:shadow"
        href="#main"
      >
        Skip to content
      </a>
      <NavSidebar installationAdmin={perms.isInstallationAdmin} orgAdmin={perms.isOrgAdmin} teamAdmin={perms.isTeamAdmin} breakGlass={me.data?.kind === "break_glass"} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 flex items-center gap-2 border-b bg-background/95 px-4 py-2.5 backdrop-blur md:px-6">
          <NavToggle />
          <span className="flex-1" />
          <Button variant="outline" size="sm" onClick={cycle} title="Cycle theme">
            <ThemeIcon className="size-4" aria-hidden="true" />
            <span>Theme: {THEME_LABEL[theme]}</span>
          </Button>
          {me.data && (
            <span className="flex min-w-0 items-center gap-2" title={me.data.email ?? undefined} data-testid="principal">
              <span className="hidden max-w-[180px] truncate sm:inline">{me.data.display_name || me.data.email || "Bootstrap admin"}</span>
              <Badge tone="info">{roleLabel(me.data)}</Badge>
            </span>
          )}
          <Button variant="outline" size="sm" onClick={() => void logout()}>
            <LogOut className="size-4 sm:hidden" aria-hidden="true" />
            <span className="sr-only sm:not-sr-only">Sign out</span>
          </Button>
        </header>
        {me.data?.authz_unavailable && (
          <div className="border-b bg-background px-4 py-3 md:px-6">
            <div className="mx-auto w-full max-w-[1200px]">
              <AuthzNotice />
            </div>
          </div>
        )}
        <main id="main" className="mx-auto w-full max-w-[1200px] flex-1 px-4 py-6 outline-none md:px-6" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </SidebarProvider>
  );
}
