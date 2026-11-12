// Generates the reference files the manual embeds. They are build products (gitignored):
//   public/openapi/control-plane.json   from the control plane's own `openapi` command
//   public/openapi/gateway.json         from the gateway's `openapi` command
// A generator that cannot run (no uv, no cargo) fails the build: a manual with an empty
// API reference must not be published.
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const docs = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repo = resolve(docs, "..");
const out = resolve(docs, "public/openapi");
mkdirSync(out, { recursive: true });

function run(command, args, options = {}) {
  return execFileSync(command, args, {
    cwd: repo,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    stdio: ["ignore", "pipe", "inherit"],
    ...options,
  });
}

// Building the app does not connect to the database; the URL only has to parse.
const controlPlane = run(
  "uv",
  ["run", "--project", "control-plane", "python", "-W", "ignore", "-m", "lumewarp_control_plane", "openapi"],
  {
    env: {
      ...process.env,
      LUMEWARP_DATABASE_URL: "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
    },
  },
);
JSON.parse(controlPlane);
writeFileSync(resolve(out, "control-plane.json"), controlPlane);
console.log("gen: public/openapi/control-plane.json");

const gateway = run("cargo", ["run", "--locked", "--quiet", "-p", "lumewarp-gateway", "--", "openapi"], {
  cwd: resolve(repo, "gateway"),
});
JSON.parse(gateway);
writeFileSync(resolve(out, "gateway.json"), gateway);
console.log("gen: public/openapi/gateway.json");

// The error-code table, from the gateway document, for pages that include it.
const doc = JSON.parse(gateway);
const rows = new Map();
for (const item of Object.values(doc.paths)) {
  for (const operation of Object.values(item)) {
    for (const [status, response] of Object.entries(operation.responses ?? {})) {
      if (!/^[45]/.test(status)) continue;
      for (const line of response.description.split("\n\n")) {
        const match = /^`([a-z_]+)`: (.*)$/s.exec(line);
        if (match) rows.set(`${match[1]} ${status}`, [status, match[1], match[2]]);
      }
    }
  }
}
// A code whose status varies is listed under every status of its operations: keep one row.
const byCode = new Map();
for (const [status, code, meaning] of rows.values()) {
  const seen = byCode.get(code);
  if (seen) seen.statuses.add(status);
  else byCode.set(code, { statuses: new Set([status]), meaning });
}
const table = ["| Status | `error.code` | When |", "| --- | --- | --- |"];
const sorted = [...byCode.entries()]
  .map(([code, { statuses, meaning }]) => [statuses.size > 3 ? "varies" : [...statuses].sort().join(", "), code, meaning])
  .sort((a, b) => a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]));
for (const [status, code, meaning] of sorted) table.push(`| ${status} | \`${code}\` | ${meaning} |`);
const generated = resolve(docs, "reference/generated");
mkdirSync(generated, { recursive: true });
writeFileSync(resolve(generated, "gateway-errors.md"), table.join("\n") + "\n");
console.log("gen: reference/generated/gateway-errors.md");
