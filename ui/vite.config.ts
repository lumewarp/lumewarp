import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built output is served by the control plane; dev server proxies its API.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/healthz": "http://127.0.0.1:8080",
    },
  },
});
