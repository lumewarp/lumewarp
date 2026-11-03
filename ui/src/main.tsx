import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ApiError } from "./api";
import { setUnauthorizedHandler } from "./auth";
import { ToastProvider } from "./components/Toast";
import { router } from "./router";
import "./styles.css";

// A permission-dependent read that is refused may mean the cached roles are stale: revalidate `/users/me`
// once (throttled) so a guarded page can switch to its access state. Never react to `/users/me` itself.
let lastAccessCheck = 0;
const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      if (query.queryKey[0] === "me") return;
      if (!(error instanceof ApiError) || (error.status !== 403 && error.status !== 404)) return;
      const now = Date.now();
      if (now - lastAccessCheck < 1000) return;
      lastAccessCheck = now;
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      // A 401 already sent the user to login; other 4xx won't succeed on retry.
      retry: (n, err) => !(err instanceof ApiError && err.status < 500) && n < 2,
    },
  },
});

setUnauthorizedHandler(() => {
  queryClient.clear();
  void router.navigate({ to: "/login" });
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
