import { createRootRoute, createRoute, createRouter, Navigate, Outlet, redirect, useParams } from "@tanstack/react-router";
import { api } from "./api";
import { getCsrfToken } from "./auth";
import { Layout } from "./components/Layout";
import { Alert, AlertDescription } from "./components/ui/alert";
import { useCan, useMe, type Access } from "./hooks";
import { Aliases } from "./pages/Aliases";
import { Audit } from "./pages/Audit";
import { Config } from "./pages/Config";
import { Models } from "./pages/Models";
import { Gateways } from "./pages/Gateways";
import { Login } from "./pages/Login";
import { MyKeys } from "./pages/MyKeys";
import { Overview } from "./pages/Overview";
import { AuthzTemporary, CheckFailed, PageLoading, RequireAccess, RequireTeamRead } from "./pages/Access";
import { Routers } from "./pages/Routers";
import { Settings } from "./pages/Settings";
import { TeamDetail } from "./pages/TeamDetail";
import { Teams } from "./pages/Teams";
import { Upstreams } from "./pages/Upstreams";
import { Usage } from "./pages/Usage";
import { Users } from "./pages/Users";

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

function guarded(Page: () => React.ReactNode, need: Access) {
  function GuardedPage() {
    return (
      <RequireAccess need={need}>
        <Page />
      </RequireAccess>
    );
  }
  return GuardedPage;
}

const page = <P extends string>(path: P, component: () => React.ReactNode, need: Access = "member") =>
  createRoute({
    getParentRoute: () => appRoute,
    path,
    component: need === "member" ? component : guarded(component, need),
  });
function TeamDetailRoute() {
  const { teamId } = useParams({ strict: false }) as { teamId: string };
  return (
    <RequireTeamRead key={teamId} teamId={teamId}>
      <TeamDetail />
    </RequireTeamRead>
  );
}

const teamDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/teams/$teamId",
  component: TeamDetailRoute,
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    page("/", Home),
    page("/my-keys", MyKeys),
    page("/teams", Teams, "teamAdmin"),
    teamDetailRoute,
    page("/users", Users, "installationAdmin"),
    page("/routers", Routers, "installationAdmin"),
    page("/models", Models, "installationAdmin"),
    page("/aliases", Aliases, "installationAdmin"),
    page("/upstreams", Upstreams, "installationAdmin"),
    page("/usage", Usage),
    page("/audit", Audit, "teamAdmin"),
    page("/gateways", Gateways, "installationAdmin"),
    page("/config", Config, "installationAdmin"),
    page("/settings", Settings),
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
