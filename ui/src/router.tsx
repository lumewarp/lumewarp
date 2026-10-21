import { useQuery } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";

const rootRoute = createRootRoute({
  component: () => (
    <main>
      <h1>Lumewarp</h1>
      <Outlet />
    </main>
  ),
});

function Home() {
  const health = useQuery({
    queryKey: ["healthz"],
    queryFn: async () => {
      const resp = await fetch("/healthz");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return (await resp.json()) as { status: string };
    },
  });
  return <p>Control plane: {health.isPending ? "…" : (health.data?.status ?? "unreachable")}</p>;
}

const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: Home });

export const router = createRouter({ routeTree: rootRoute.addChildren([indexRoute]) });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
