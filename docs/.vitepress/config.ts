import { defineConfig } from "vitepress";

// Sections follow the reader, not the component: someone holding an `lw-` key, a team
// admin and an operator each have their own entry point.
const sidebar = [
  {
    text: "Getting started",
    items: [
      { text: "What Lumewarp is", link: "/guide/" },
      { text: "Concepts", link: "/guide/concepts" },
      { text: "Quick start: standalone gateway", link: "/guide/quickstart-standalone" },
      { text: "Quick start: with the control plane", link: "/guide/quickstart-control-plane" },
    ],
  },
  {
    text: "Calling the gateway",
    items: [
      { text: "Authentication and keys", link: "/using/" },
      { text: "Endpoints", link: "/using/endpoints" },
      { text: "Errors, limits and retries", link: "/using/errors" },
      { text: "MCP", link: "/using/mcp" },
      { text: "A2A", link: "/using/a2a" },
    ],
  },
  {
    text: "Self-service",
    items: [
      { text: "Your keys", link: "/self-service/" },
      { text: "Playground", link: "/self-service/playground" },
    ],
  },
  {
    text: "Administration",
    items: [
      { text: "Roles", link: "/admin/" },
      { text: "Orgs, teams and members", link: "/admin/tenancy" },
      { text: "Keys, limits and budgets", link: "/admin/keys-and-limits" },
      { text: "Upstreams, models and aliases", link: "/admin/catalog" },
      { text: "Routers", link: "/admin/routers" },
      { text: "MCP servers and A2A agents", link: "/admin/mcp-a2a" },
      { text: "Usage and audit", link: "/admin/usage-and-audit" },
      { text: "Config versions and gateways", link: "/admin/config-and-gateways" },
    ],
  },
  {
    text: "Operations",
    items: [
      { text: "Install", link: "/operations/" },
      { text: "Control plane settings", link: "/operations/control-plane" },
      { text: "Gateway settings", link: "/operations/gateway" },
      { text: "Sign-in and authorization", link: "/operations/identity" },
      { text: "Metrics, alerts and tracing", link: "/operations/observability" },
      { text: "Upgrades and limits", link: "/operations/upgrades" },
      { text: "Troubleshooting", link: "/operations/troubleshooting" },
    ],
  },
  {
    text: "Extending",
    items: [
      { text: "Extension points", link: "/extending/" },
      { text: "Gateway plugins (WASM)", link: "/plugins" },
      { text: "Router strategies", link: "/router-strategies" },
      { text: "Control plane plugins (Python)", link: "/extending/python-plugins" },
      { text: "Plugin development skill", link: "/extending/skill" },
    ],
  },
  {
    text: "Reference",
    items: [
      { text: "Control plane API", link: "/reference/control-plane-api" },
      { text: "Gateway API", link: "/reference/gateway-api" },
      { text: "Gateway YAML", link: "/reference/gateway-yaml" },
      { text: "Error codes", link: "/reference/errors" },
      { text: "Metrics", link: "/reference/metrics" },
      { text: "Config stream protocol", link: "/config-protocol" },
    ],
  },
  {
    text: "Internals",
    items: [{ text: "Architecture (中文)", link: "/architecture" }],
  },
];

export default defineConfig({
  title: "Lumewarp",
  description: "A fast, manageable LLM gateway for OpenAI-compatible upstreams.",
  // Set LUMEWARP_DOCS_BASE to the path the site is served under (GitHub Pages: /lumewarp/).
  base: process.env.LUMEWARP_DOCS_BASE || "/",
  cleanUrls: true,
  lastUpdated: true,
  srcExclude: ["**/node_modules/**"],
  // One locale for now. A translation becomes a directory (`zh-tw/`) and an entry here.
  locales: {
    root: { label: "English", lang: "en" },
  },
  themeConfig: {
    nav: [
      { text: "Guide", link: "/guide/" },
      { text: "Administration", link: "/admin/" },
      { text: "Operations", link: "/operations/" },
      { text: "API", link: "/reference/control-plane-api" },
    ],
    sidebar,
    search: { provider: "local" },
    outline: { level: [2, 3] },
    socialLinks: [{ icon: "github", link: "https://github.com/lumewarp/lumewarp" }],
    editLink: {
      pattern: "https://github.com/lumewarp/lumewarp/edit/main/docs/:path",
    },
    footer: { message: "Apache-2.0" },
  },
});
