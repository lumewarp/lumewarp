import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, Outlet, useNavigate } from "@tanstack/react-router";
import {
  BarChart3,
  Bot,
  Boxes,
  Building2,
  Cable,
  CornerDownRight,
  Layers,
  History,
  KeyRound,
  ListOrdered,
  ChevronDown,
  Globe,
  LogOut,
  Menu,
  PanelLeft,
  MessageSquare,
  Monitor,
  Moon,
  Plug,
  Plus,
  Radio,
  Route,
  ScrollText,
  Server,
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
import { useZone, zoneColor, ZoneBadge } from "../zone";
import { AuthzNotice } from "../pages/Access";
import { useClearSecrets } from "./SecretProvider";
import { Badge } from "./Bits";
import { useToast } from "./Toast";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
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
      { to: "/model-groups", label: "Model groups", icon: Layers, access: "installationAdmin" },
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
      { to: "/requests", label: "Requests", icon: ListOrdered },
      { to: "/audit", label: "Audit log", icon: ScrollText, access: "teamAdmin" },
    ],
  },
  {
    group: "Data plane",
    items: [
      { to: "/gateways", label: "Gateways", icon: Radio, access: "installationAdmin" },
      { to: "/plugins", label: "Plugins", icon: Plug, access: "installationAdmin" },
      { to: "/config", label: "Config versions", icon: History, access: "installationAdmin" },
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

/** Phones only: opens the navigation drawer. From md up the sidebar has its own collapse button. */
function NavToggle() {
  const { openMobile, toggleSidebar } = useSidebar();
  return (
    <Button variant="outline" size="icon" className="size-8 md:hidden" aria-label="Toggle navigation" aria-expanded={openMobile} onClick={toggleSidebar}>
      <Menu className="size-4" aria-hidden="true" />
    </Button>
  );
}

/** Desktop: collapses the sidebar to its icons and back, at the bottom of the sidebar. */
function CollapseButton() {
  const { isMobile, open, toggleSidebar } = useSidebar();
  if (isMobile) return null;
  const label = open ? "Collapse navigation" : "Expand navigation";
  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <SidebarMenuButton aria-label={label} title={open ? undefined : label} aria-expanded={open} onClick={toggleSidebar} className="text-sidebar-foreground/80">
          <PanelLeft aria-hidden="true" />
          <span>Collapse</span>
        </SidebarMenuButton>
      </SidebarMenuItem>
    </SidebarMenu>
  );
}

// The desktop sidebar's expanded/collapsed choice is a per-browser convenience.
const SIDEBAR_KEY = "lumewarp.sidebar";
function readSidebarOpen(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) !== "collapsed";
  } catch {
    return true;
  }
}
function writeSidebarOpen(open: boolean) {
  try {
    localStorage.setItem(SIDEBAR_KEY, open ? "expanded" : "collapsed");
  } catch {
    // Storage blocked: the choice lasts until the page reloads.
  }
}

function NavSidebar({ installationAdmin, orgAdmin, teamAdmin, breakGlass }: { installationAdmin: boolean; orgAdmin: boolean; teamAdmin: boolean; breakGlass: boolean }) {
  const { isMobile, setOpenMobile, state } = useSidebar();
  // Collapsed to icons, a link names itself on hover.
  const collapsed = state === "collapsed" && !isMobile;
  const perms = { isInstallationAdmin: installationAdmin, isOrgAdmin: orgAdmin, isTeamAdmin: teamAdmin };
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-5 pt-4 pb-1 group-data-[collapsible=icon]:px-2">
        <div className="flex items-center gap-2.5 text-base font-bold group-data-[collapsible=icon]:justify-center">
          <img src="/favicon.svg" alt="" aria-hidden="true" className="size-6 shrink-0" />
          <span className="group-data-[collapsible=icon]:hidden">Lumewarp</span>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <nav aria-label="Main" className="flex min-h-0 flex-col gap-1 overflow-x-hidden overflow-y-auto">
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
                            <Link to={n.to} title={collapsed ? n.label : undefined} activeProps={{ "aria-current": "page" }} onClick={() => setOpenMobile(false)}>
                              <Icon aria-hidden="true" />
                              <span>{n.label}</span>
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
      <SidebarFooter className="p-2">
        <CollapseButton />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}

/** The zone this view is looking at, at the left of the header; admins also reach Manage zones and New zone here. */
function ZoneSwitcher({ installationAdmin }: { installationAdmin: boolean }) {
  const { name, zone, zones, isLoading, setZone } = useZone();
  const navigate = useNavigate();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="max-w-[240px]" data-testid="zone-switcher">
          {zone ? <ZoneBadge zone={zone} /> : <span className="truncate text-muted-foreground">{isLoading ? "Zone…" : name}</span>}
          <ChevronDown className="size-4" aria-hidden="true" />
          <span className="sr-only">Zone menu</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-60">
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">Zone</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={name} onValueChange={setZone}>
          {zones.map((z) => (
            <DropdownMenuRadioItem key={z.name} value={z.name} data-testid={`zone-option-${z.name}`}>
              <ZoneBadge zone={z} />
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        {installationAdmin && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => void navigate({ to: "/zones", search: { new: true } })}>
              <Plus aria-hidden="true" />
              New zone
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void navigate({ to: "/zones", search: {} })}>
              <Globe aria-hidden="true" />
              Manage zones
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Who is signed in, the theme and Sign out, in one menu at the top right. */
function AccountMenu({ me, theme, onTheme, onSignOut }: { me: Me | undefined; theme: keyof typeof THEME_LABEL; onTheme: (theme: keyof typeof THEME_LABEL) => void; onSignOut: () => void }) {
  const name = me ? me.display_name || me.email || "Bootstrap admin" : "Account";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="max-w-[260px]" title={me?.email ?? undefined} data-testid="principal">
          <span className="hidden min-w-0 truncate sm:inline">{name}</span>
          {me && <Badge tone="info">{roleLabel(me)}</Badge>}
          <ChevronDown className="size-4" aria-hidden="true" />
          <span className="sr-only">Account menu</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        {me && (
          <DropdownMenuLabel className="flex flex-col gap-0.5">
            <span className="truncate">{name}</span>
            {me.email && me.email !== name && <span className="truncate text-xs font-normal text-muted-foreground">{me.email}</span>}
          </DropdownMenuLabel>
        )}
        {me && (
          <div className="flex flex-wrap gap-1 px-2 pb-1.5" data-testid="session-roles">
            {me.installation_role && <Badge tone="info">{me.installation_role === "proxy_admin" ? "proxy admin" : "installation admin"}</Badge>}
            {!me.installation_role &&
              me.admin_orgs?.map((org) => (
                <Badge key={org.id} tone="info">
                  {org.name} · org admin
                </Badge>
              ))}
            {/* Direct memberships only: inherited teams follow from the roles above. */}
            {me.teams
              .filter((team) => team.direct)
              .map((team) => (
                <Badge key={team.team_id} tone="info">
                  {team.team_name} · {team.role}
                </Badge>
              ))}
            {!me.is_admin && !me.admin_orgs?.length && me.teams.length === 0 && <span className="text-xs text-muted-foreground">No team memberships.</span>}
          </div>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme} onValueChange={(value) => onTheme(value as keyof typeof THEME_LABEL)}>
          {(Object.keys(THEME_LABEL) as (keyof typeof THEME_LABEL)[]).map((key) => {
            const Icon = THEME_ICON[key];
            return (
              <DropdownMenuRadioItem key={key} value={key}>
                <Icon aria-hidden="true" />
                {THEME_LABEL[key]}
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={onSignOut}>
          <LogOut aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function Layout() {
  const me = useMe();
  const perms = useCan();
  const { theme, setTheme } = useTheme();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const clearSecrets = useClearSecrets();
  const [sidebarOpen, setSidebarOpen] = useState(readSidebarOpen);
  const zone = useZone();

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
    <SidebarProvider
      className="bg-background"
      open={sidebarOpen}
      onOpenChange={(open) => {
        setSidebarOpen(open);
        writeSidebarOpen(open);
      }}
    >
      <a
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[100] focus:rounded-md focus:border focus:bg-background focus:px-3 focus:py-2 focus:shadow"
        href="#main"
      >
        Skip to content
      </a>
      <NavSidebar installationAdmin={perms.isInstallationAdmin} orgAdmin={perms.isOrgAdmin} teamAdmin={perms.isTeamAdmin} breakGlass={me.data?.kind === "break_glass"} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header
          className={`sticky top-0 z-40 flex items-center gap-2 border-t-2 border-b bg-background/95 px-4 py-2.5 backdrop-blur md:px-6 ${zoneColor(zone.zone?.color).border}`}
          data-testid="app-header"
          data-zone={zone.name}
        >
          <NavToggle />
          <ZoneSwitcher installationAdmin={perms.isInstallationAdmin} />
          <span className="flex-1" />
          <AccountMenu me={me.data} theme={theme} onTheme={setTheme} onSignOut={() => void logout()} />
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
