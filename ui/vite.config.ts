import { Agent } from "node:http";
import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built output is served by the control plane; dev server proxies its API.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // Keep-alive to the control plane: without it every proxied response ends with
    // `connection: close`, and a browser on Windows reaching WSL through its localhost relay
    // sometimes sees that close as a reset, cutting off the end of a streamed response.
    proxy: {
      "/api": { target: "http://127.0.0.1:8091", agent: new Agent({ keepAlive: true }) },
      "/healthz": { target: "http://127.0.0.1:8091", agent: new Agent({ keepAlive: true }) },
    },
  },
});
