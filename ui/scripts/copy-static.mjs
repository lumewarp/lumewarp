// Copy the built SPA (ui/dist) into the control plane package so it ships as package data.
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(root, "dist");
const dest = join(root, "..", "control-plane", "src", "lumewarp_control_plane", "static");

if (!existsSync(join(src, "index.html"))) throw new Error("ui/dist is missing; run `pnpm build` first");
mkdirSync(dest, { recursive: true });
for (const name of readdirSync(dest)) if (name !== ".gitkeep") rmSync(join(dest, name), { recursive: true, force: true });
cpSync(src, dest, { recursive: true });
writeFileSync(join(dest, ".gitkeep"), "");
console.log(`copied ${src} -> ${dest}`);
