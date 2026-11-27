import { expect, test } from "@playwright/test";

import { pick } from "./helpers";
import { signIn, VIEWER } from "./selfService";

/*
 * The MCP client entry point on My keys and the audit `via` column. The MCP server itself is on the
 * control plane (T5 covers it end to end); these tests cover the UI: the snippet dialog, the token
 * expiry the create dialog now sends, and how the audit log badges the authentication route.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const baseURL = () => test.info().project.use.baseURL!;

test("the MCP connect dialog shows the endpoint and copyable snippets", async ({ page }) => {
  await signIn(page, VIEWER);
  await page.getByRole("button", { name: "Connect an MCP client" }).click();
  const dialog = page.getByRole("dialog", { name: "Connect an MCP client" });
  await expect(dialog).toBeVisible();

  const endpoint = `${baseURL()}/mcp`;
  await expect(dialog.getByLabel("endpoint URL", { exact: true })).toHaveText(endpoint);
  await expect(dialog.getByLabel("Claude Code command", { exact: true })).toHaveText(
    `claude mcp add --transport http lumewarp ${endpoint} --header "Authorization: Bearer <TOKEN>"`,
  );
  const json = JSON.parse((await dialog.getByLabel("Cursor and VS Code mcp.json", { exact: true }).textContent())!);
  expect(json).toEqual({
    mcpServers: { lumewarp: { type: "http", url: endpoint, headers: { Authorization: "Bearer <TOKEN>" } } },
  });
  for (const name of ["Copy endpoint URL", "Copy Claude Code command", "Copy Cursor and VS Code mcp.json"]) {
    await expect(dialog.getByRole("button", { name })).toBeVisible();
  }
  // The claude.ai connector takes the same URL and header, and no real secret is ever put in the dialog.
  await expect(dialog).toContainText("Request headers");
  await expect(dialog).toContainText("Authorization: Bearer <TOKEN>");
  expect(await dialog.textContent()).not.toMatch(/lwp-/);

  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseURL() });
  await dialog.getByRole("button", { name: "Copy endpoint URL" }).click();
  await expect(dialog.getByRole("button", { name: "endpoint URL copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(endpoint);

  // Phone width: the dialog fits; a long snippet scrolls inside its own block.
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(dialog).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  // Dark mode renders the same content.
  await dialog.getByRole("button", { name: "Close" }).click();
  await page.evaluate(() => localStorage.setItem("lumewarp.theme", "dark"));
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await page.getByRole("button", { name: "Connect an MCP client" }).click();
  const dark = page.getByRole("dialog", { name: "Connect an MCP client" });
  await expect(dark.getByLabel("endpoint URL", { exact: true })).toHaveText(endpoint);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("a new token defaults to a 90-day expiry, takes another, and the list shows it", async ({ page }) => {
  await signIn(page, VIEWER);
  await page.getByRole("button", { name: "New token" }).click();
  const dialog = page.getByRole("dialog", { name: "New personal access token" });
  await expect(dialog.getByRole("combobox", { name: "Expiry" })).toContainText("90 days");
  await dialog.getByLabel("Name").fill("e2e-mcp-laptop");
  await pick(dialog, "Expiry", "30 days");

  const posted = page.waitForRequest(
    (r) => r.method() === "POST" && new URL(r.url()).pathname === "/api/v1/users/me/tokens",
  );
  await dialog.getByRole("button", { name: "Create token" }).click();
  const body = (await posted).postDataJSON() as { name: string; expires_at: string | null };
  expect(body.name).toBe("e2e-mcp-laptop");
  expect(body.expires_at).toBeTruthy();
  const days = (new Date(body.expires_at!).getTime() - Date.now()) / 86_400_000;
  expect(days).toBeGreaterThan(29.9);
  expect(days).toBeLessThan(30.1);

  const secret = page.getByRole("dialog", { name: "Token created" });
  await expect(secret.getByTestId("secret-value")).toHaveText(/^lwp-/);
  await secret.getByRole("button", { name: /saved it/ }).click();

  const shown = await page.evaluate((iso) => new Date(iso).toLocaleDateString(), body.expires_at!);
  await expect(page.getByRole("row").filter({ hasText: "e2e-mcp-laptop" })).toContainText(shown);
});

test("the audit log badges the authentication route and filters by it", async ({ page, request }) => {
  const login = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(login.status()).toBe(204);
  await page.context().addCookies((await request.storageState()).cookies);

  type Via = "session" | "pat" | "mcp" | null;
  const entry = (id: number, via: Via) => ({
    id,
    ts: new Date().toISOString(),
    actor_type: "user",
    actor_id: null,
    via,
    action: `via.${via ?? "none"}`,
    resource_type: "user",
    resource_id: null,
    team_id: null,
    org_id: null,
    details: {},
    key: null,
  });
  const rows = [entry(4, "mcp"), entry(3, "pat"), entry(2, "session"), entry(1, null)];
  await page.route("**/api/v1/audit?**", (route) => {
    const via = new URL(route.request().url()).searchParams.get("via");
    return route.fulfill({ json: { items: rows.filter((row) => !via || row.via === via), next_cursor: null } });
  });

  await page.goto("/audit");
  const row = (action: string) => page.getByRole("row").filter({ hasText: action });
  await expect(row("via.session").getByTestId("audit-via")).toHaveText("Session");
  await expect(row("via.pat").getByTestId("audit-via")).toHaveText("PAT");
  await expect(row("via.mcp").getByTestId("audit-via")).toHaveText("MCP");
  // A null `via` (gateway and system events, and rows older than the column) shows nothing.
  await expect(row("via.none").getByTestId("audit-via")).toHaveCount(0);
  await expect(page.getByTestId("audit-via")).toHaveCount(3);

  const filtered = page.waitForRequest(
    (r) => new URL(r.url()).pathname === "/api/v1/audit" && new URL(r.url()).searchParams.get("via") === "mcp",
  );
  await pick(page, "Via", "MCP");
  await filtered;
  await expect(page.getByRole("combobox", { name: "Via" })).toContainText("MCP");
  await expect(row("via.mcp")).toBeVisible();
  await expect(row("via.session")).toHaveCount(0);
  await expect(row("via.pat")).toHaveCount(0);
  await expect(row("via.none")).toHaveCount(0);
});
