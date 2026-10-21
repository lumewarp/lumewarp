import { defineConfig } from "@playwright/test";

const PORT = process.env.E2E_HTTP_PORT ?? "8992";

// The control plane serves the built SPA (`pnpm build:cp`) from a fresh database (e2e/serve.py).
export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: {
    command: "uv run --project ../control-plane python e2e/serve.py",
    url: `http://127.0.0.1:${PORT}/readyz`,
    reuseExistingServer: false,
    timeout: 90_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
  },
});
