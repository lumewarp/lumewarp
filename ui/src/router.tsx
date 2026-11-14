import { createRootRoute, createRoute, createRouter, Navigate, Outlet, redirect, useParams } from "@tanstack/react-router";
import { lazy, Suspense, type ReactNode } from "react";

import { api } from "./api";
import { getCsrfToken } from "./auth";
import { Layout } from "./components/Layout";
import { Alert, AlertDescription } from "./components/ui/alert";
import { useCan, useMe, type Access } from "./hooks";
import { AuthzTemporary, CheckFailed, PageBoundary, PageLoading, RequireAccess, RequireOrgRead, RequireTeamRead } from "./pages/Access";
import { Login } from "./pages/Login";
import { MyKeys } from "./pages/MyKeys";
import { Overview } from "./pages/Overview";
import { Usage } from "./pages/Usage";

/*
 * The screens nobody needs on arrival are their own chunks. A visitor lands on Login, Overview or My keys, so
 * an admin page's code is downloaded when its route is opened and not before — and a caller without the right
 * to see it never mounts the page at all, so the chunk is never requested (the guard wraps the lazy element).
 */
const RoutersPage = lazy(() => import("./pages/Routers").then((module) => ({ default: module.Routers })));
const UpstreamsPage = lazy(() => import("./pages/Upstreams").then((module) => ({ default: module.Upstreams })));
const ModelsPage = lazy(() => import("./pages/Models").then((module) => ({ default: module.Models })));
const ModelEditPage = lazy(() => import("./pages/ModelEdit").then((module) => ({ default: module.ModelEdit })));
const UpstreamEditPage = lazy(() => import("./pages/UpstreamEdit").then((module) => ({ default: module.UpstreamEdit })));
const AliasEditPage = lazy(() => import("./pages/AliasEdit").then((module) => ({ default: module.AliasEdit })));
const RouterEditPage = lazy(() => import("./pages/RouterEdit").then((module) => ({ default: module.RouterEdit })));
const AliasesPage = lazy(() => import("./pages/Aliases").then((module) => ({ default: module.Aliases })));
const GatewaysPage = lazy(() => import("./pages/Gateways").then((module) => ({ default: module.Gateways })));
const GatewayDetailPage = lazy(() => import("./pages/GatewayDetail").then((module) => ({ default: module.GatewayDetail })));
const ConfigPage = lazy(() => import("./pages/Config").then((module) => ({ default: module.Config })));
const UsersPage = lazy(() => import("./pages/Users").then((module) => ({ default: module.Users })));
const AuditPage = lazy(() => import("./pages/Audit").then((module) => ({ default: module.Audit })));
const TeamsPage = lazy(() => import("./pages/Teams").then((module) => ({ default: module.Teams })));
const OrgsPage = lazy(() => import("./pages/Orgs").then((module) => ({ default: module.Orgs })));
const OrgDetailPage = lazy(() => import("./pages/Orgs").then((module) => ({ default: module.OrgDetail })));
const PlaygroundPage = lazy(() => import("./pages/Playground").then((module) => ({ default: module.Playground })));
const SettingsPage = lazy(() => import("./pages/Settings").then((module) => ({ default: module.Settings })));
const TeamDetailPage = lazy(() => import("./pages/TeamDetail").then((module) => ({ default: module.TeamDetail })));
const McpServersPage = lazy(() => import("./pages/McpServers").then((module) => ({ default: module.McpServers })));
const McpServerEditPage = lazy(() => import("./pages/McpServerEdit").then((module) => ({ default: module.McpServerEdit })));
const PluginsPage = lazy(() => import("./pages/Plugins").then((module) => ({ default: module.Plugins })));
const A2aAgentsPage = lazy(() => import("./pages/A2aAgents").then((module) => ({ default: module.A2aAgents })));
const A2aAgentEditPage = lazy(() => import("./pages/A2aAgentEdit").then((module) => ({ default: module.A2aAgentEdit })));

/** A chunk in flight shows the shell's loading line; a chunk that fails shows its own error, not a blank page. */
function suspended(Page: () => ReactNode) {
  return function SuspendedPage() {
    return (
      <PageBoundary>
        <Suspense fallback={<PageLoading />}>
          <Page />
        </Suspense>
      </PageBoundary>
    );
  };
}

const rootRoute = createRootRoute({ component: Outlet });

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: Login,
});

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  beforeLoad: async ({ location }) => {
    // A cookie session already discovered in this page load has its CSRF token in memory.
    if (getCsrfToken()) return;
    const session = await api.session();
    if (!session.authenticated) {
      throw redirect({ to: "/login", search: { next: location.href } });
    }
  },
  component: Layout,
});

/** Overview is the home of an installation admin or a team admin; everyone else's home is My keys. */
function Home() {
  const me = useMe();
  const perms = useCan();
  if (me.isPending) return <PageLoading />;
  if (me.error && !me.data) {
    return (
      <Alert variant="destructive" data-testid="state-error">
        <AlertDescription>{me.error.message}</AlertDescription>
      </Alert>
    );
  }
  const notice = me.error && me.data ? <CheckFailed onRetry={() => void me.refetch()} /> : null;
  if (me.data?.authz_unavailable) {
    return (
      <>
        {notice}
        <AuthzTemporary onRetry={() => void me.refetch()} />
      </>
    );
  }
  if (!perms.isInstallationAdmin && !perms.isTeamAdmin) return <Navigate to="/my-keys" replace />;
  return (
    <>
      {notice}
      <Overview />
    </>
  );
}

/** The guard renders the page (and so fetches its chunk) only when the caller may see it. */
function guarded(Page: () => ReactNode, need: Access) {
  const Page1 = suspended(Page);
  function GuardedPage() {
    return (
      <RequireAccess need={need}>
        <Page1 />
      </RequireAccess>
    );
  }
  return GuardedPage;
}

const page = <P extends string>(path: P, component: () => ReactNode, need?: Access) =>
  createRoute({
    getParentRoute: () => appRoute,
    path,
    component: need ? guarded(component, need) : component,
  });

const SuspendedTeamDetail = suspended(() => <TeamDetailPage />);

function TeamDetailRoute() {
  const { teamId } = useParams({ strict: false }) as { teamId: string };
  return (
    <RequireTeamRead key={teamId} teamId={teamId}>
      <SuspendedTeamDetail />
    </RequireTeamRead>
  );
}

const teamDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/teams/$teamId",
  component: TeamDetailRoute,
});

const SuspendedOrgDetail = suspended(() => <OrgDetailPage />);

function OrgDetailRoute() {
  const { orgId } = useParams({ strict: false }) as { orgId: string };
  return (
    <RequireOrgRead key={orgId} orgId={orgId}>
      <SuspendedOrgDetail />
    </RequireOrgRead>
  );
}

const orgDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/orgs/$orgId",
  component: OrgDetailRoute,
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    page("/", Home),
    page("/my-keys", MyKeys),
    page("/playground", () => <PlaygroundPage />),
    page("/orgs", () => <OrgsPage />, "orgAdmin"),
    orgDetailRoute,
    page("/teams", () => <TeamsPage />, "teamAdmin"),
    teamDetailRoute,
    page("/users", () => <UsersPage />, "installationAdmin"),
    page("/routers", () => <RoutersPage />, "installationAdmin"),
    page("/routers/new", () => <RouterEditPage />, "installationAdmin"),
    page("/routers/$routerId", () => <RouterEditPage />, "installationAdmin"),
    page("/models", () => <ModelsPage />, "installationAdmin"),
    page("/models/new", () => <ModelEditPage />, "installationAdmin"),
    page("/models/$modelId", () => <ModelEditPage />, "installationAdmin"),
    page("/aliases", () => <AliasesPage />, "installationAdmin"),
    page("/aliases/new", () => <AliasEditPage />, "installationAdmin"),
    page("/aliases/$aliasId", () => <AliasEditPage />, "installationAdmin"),
    page("/upstreams", () => <UpstreamsPage />, "installationAdmin"),
    page("/upstreams/new", () => <UpstreamEditPage />, "installationAdmin"),
    page("/upstreams/$upstreamId", () => <UpstreamEditPage />, "installationAdmin"),
    page("/mcp-servers", () => <McpServersPage />, "installationAdmin"),
    page("/mcp-servers/new", () => <McpServerEditPage />, "installationAdmin"),
    page("/mcp-servers/$serverId", () => <McpServerEditPage />, "installationAdmin"),
    page("/plugins", () => <PluginsPage />, "installationAdmin"),
    page("/a2a-agents", () => <A2aAgentsPage />, "installationAdmin"),
    page("/a2a-agents/new", () => <A2aAgentEditPage />, "installationAdmin"),
    page("/a2a-agents/$id", () => <A2aAgentEditPage />, "installationAdmin"),
    page("/usage", Usage),
    page("/audit", () => <AuditPage />, "teamAdmin"),
    page("/gateways", () => <GatewaysPage />, "installationAdmin"),
    page("/gateways/$gatewayId", () => <GatewayDetailPage />, "installationAdmin"),
    page("/config", () => <ConfigPage />, "installationAdmin"),
    page("/settings", suspended(() => <SettingsPage />)),
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
