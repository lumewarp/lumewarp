import { createRootRoute, createRoute, createRouter, Outlet, redirect } from "@tanstack/react-router";
import { api } from "./api";
import { getCsrfToken } from "./auth";
import { Layout } from "./components/Layout";
import { Audit } from "./pages/Audit";
import { Config } from "./pages/Config";
import { Deployments } from "./pages/Deployments";
import { Gateways } from "./pages/Gateways";
import { Keys } from "./pages/Keys";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Routes } from "./pages/Routes";
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

const page = <P extends string>(path: P, component: () => React.ReactNode) => createRoute({ getParentRoute: () => appRoute, path, component });
const teamDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/teams/$teamId",
  component: TeamDetail,
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    page("/", Overview),
    page("/teams", Teams),
    teamDetailRoute,
    page("/users", Users),
    page("/keys", Keys),
    page("/routes", Routes),
    page("/deployments", Deployments),
    page("/upstreams", Upstreams),
    page("/usage", Usage),
    page("/audit", Audit),
    page("/gateways", Gateways),
    page("/config", Config),
    page("/settings", Settings),
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
