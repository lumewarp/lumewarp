import { createRootRoute, createRoute, createRouter, Outlet, redirect } from "@tanstack/react-router";
import { getToken } from "./auth";
import { Layout } from "./components/Layout";
import { Audit } from "./pages/Audit";
import { Deployments } from "./pages/Deployments";
import { Keys } from "./pages/Keys";
import { Login } from "./pages/Login";
import { Orgs } from "./pages/Orgs";
import { Overview } from "./pages/Overview";
import { Projects } from "./pages/Projects";
import { Routes } from "./pages/Routes";
import { Settings } from "./pages/Settings";
import { Teams } from "./pages/Teams";
import { Upstreams } from "./pages/Upstreams";
import { Usage } from "./pages/Usage";

const rootRoute = createRootRoute({ component: Outlet });

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  beforeLoad: () => {
    if (getToken()) throw redirect({ to: "/" });
  },
  component: Login,
});

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  beforeLoad: () => {
    if (!getToken()) throw redirect({ to: "/login" });
  },
  component: Layout,
});

const page = <P extends string>(path: P, component: () => React.ReactNode) => createRoute({ getParentRoute: () => appRoute, path, component });

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    page("/", Overview),
    page("/orgs", Orgs),
    page("/teams", Teams),
    page("/projects", Projects),
    page("/keys", Keys),
    page("/routes", Routes),
    page("/deployments", Deployments),
    page("/upstreams", Upstreams),
    page("/usage", Usage),
    page("/audit", Audit),
    page("/settings", Settings),
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
