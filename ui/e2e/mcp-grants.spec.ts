import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { createServer } from "node:http";

import { deleteTeams, dialog, ensureOrg, pick, shot } from "./helpers";
import { signIn as signInAsMember, VIEWER, VIEWER_EMAIL } from "./selfService";

/*
 * MCP and A2A grants, the Usage tabs and the self-service endpoint snippet.
 *
 * A grant may only name a tool the control plane has seen, so the tools here are not faked: a small MCP
 * server answers `initialize` and `tools/list` on 127.0.0.1, and `POST /mcp-servers/discover` records its
 * definitions for real. Teams, members and policies go through the API the UI uses; the Usage tab answers
 * from routes, because usage events arrive over the gateway's gRPC stream, which a spec cannot drive.
 */

const BREAK_GLASS = "e2e-break-glass-password";
const admin: Record<string, string> = {};

type Team = {
  id: string;
  name: string;
  mcp_access?: string;
  mcp_servers?: { server: string; all_tools: boolean; tools: string[] }[];
  a2a_access?: string;
  a2a_agents?: string[];
  members?: unknown;
};
type Member = {
  email: string;
  mcp_access: string;
  mcp_servers: { server: string; all_tools: boolean; tools: string[] }[];
  a2a_access: string;
  a2a_agents: string[];
};

test.beforeEach(async ({ request }) => {
  const response = await request.post("/api/v1/session/break-glass", { data: { password: BREAK_GLASS } });
  expect(response.status()).toBe(204);
  admin["X-Lumewarp-CSRF"] = (await (await request.get("/api/v1/session")).json()).csrf_token;
});

// The worktree's database is shared by the whole Playwright run: this spec takes its own rows away again.
test.afterEach(async ({ request }) => {
  for (const [path, prefix] of [
    ["/mcp-servers", "mg-"],
    ["/a2a-agents", "mg-"],
  ] as const) {
    const listed = await request.get(`/api/v1${path}?limit=200`, { headers: admin });
    for (const row of ((await listed.json()).items ?? []) as { id: string; name: string }[]) {
      if (row.name.startsWith(prefix)) await request.delete(`/api/v1${path}/${row.id}`, { headers: admin });
    }
  }
  await deleteTeams(request, admin, "mg-");
});

async function send<T>(request: APIRequestContext, method: "GET" | "POST" | "PATCH" | "PUT", path: string, data?: object): Promise<T> {
  const response = await request.fetch(`/api/v1${path}`, { method, headers: admin, data });
  // A create answers 201 and a member upsert 200 or 201: the caller asserts the body it needs.
  expect(response.ok(), `${method} ${path}: ${await response.text()}`).toBe(true);
  return (response.status() === 204 ? undefined : await response.json()) as T;
}

async function makeTeam(request: APIRequestContext, name: string): Promise<Team> {
  return send<Team>(request, "POST", "/teams", { name, org_id: await ensureOrg(request, admin) });
}

async function memberId(request: APIRequestContext, email: string): Promise<string> {
  const users = await send<{ items: { id: string; email: string }[] }>(request, "GET", "/users?limit=200");
  const user = users.items.find((item) => item.email === email);
  expect(user, `${email} exists`).toBeTruthy();
  return user!.id;
}

/**
 * A minimal MCP endpoint on a free port: `initialize`, `notifications/initialized` and `tools/list` are all
 * discovery calls, answered as JSON. The control plane runs on this host, so it can reach it.
 */
async function mcpUpstream(tools: string[]): Promise<{ url: string; close: () => Promise<void> }> {
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => (body += chunk));
    request.on("end", () => {
      const message = (body ? JSON.parse(body) : {}) as { id?: number; method?: string };
      const answer = (result: unknown) => {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }));
      };
      if (message.method === "initialize") {
        answer({ protocolVersion: "2025-11-25", capabilities: { tools: {}, prompts: {} }, serverInfo: { name: "mg-mock", version: "1" } });
      } else if (message.method === "tools/list") {
        answer({ tools: tools.map((name) => ({ name, description: `${name} tool`, inputSchema: { type: "object", properties: {} } })) });
      } else if (message.method === "prompts/list") {
        answer({ prompts: [] });
      } else {
        // A notification: any 2xx ends it.
        response.writeHead(202);
        response.end();
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  return { url: `http://127.0.0.1:${port}/mcp`, close: () => new Promise((resolve) => server.close(() => resolve())) };
}

/** One MCP server with real definitions (via Discover now) and one A2A agent. */
async function seedTargets(request: APIRequestContext, prefix: string, tools: string[]): Promise<{ server: string; agent: string }> {
  const upstream = await mcpUpstream(tools);
  try {
    const created = await send<{ id: string; name: string }>(request, "POST", "/mcp-servers", { name: `${prefix}-srv`, url: upstream.url, auth: { plugin: "none" } });
    const discovered = await send<{ tools: { name: string; state: string }[] }>(request, "POST", "/mcp-servers/discover", { url: upstream.url, server_id: created.id });
    expect(discovered.tools.map((tool) => tool.name).sort()).toEqual([...tools].sort());
    const agent = await send<{ name: string }>(request, "POST", "/a2a-agents", { name: `${prefix}-agent`, url: "https://agent.example/a2a" });
    return { server: created.name, agent: agent.name };
  } finally {
    await upstream.close();
  }
}

async function signInAsAdmin(page: Page, request: APIRequestContext, path: string) {
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto(path);
}

test("a team is granted MCP servers with chosen tools and A2A agents, and reads them back", async ({ page, request }) => {
  const target = await seedTargets(request, "mg1", ["echo", "add"]);
  const team = await makeTeam(request, "mg1-team");
  await signInAsAdmin(page, request, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Edit team" }).click();
  const form = dialog(page);

  // The model picker keeps its All | Selected; the MCP and A2A groups name their own options so that three
  // groups in one dialog are still distinct to a reader.
  const mcp = form.getByTestId("mcp-grants");
  await expect(mcp.getByRole("radio", { name: "Every server" })).toBeChecked();
  await mcp.getByRole("radio", { name: "Chosen servers" }).click();
  const server = mcp.getByRole("group", { name: target.server });
  await server.getByRole("checkbox", { name: target.server }).check();
  // A new server starts with every tool; the pick list is the definitions Discover now recorded.
  await expect(server.getByRole("radio", { name: "All tools" })).toBeChecked();
  await server.getByRole("radio", { name: "Selected tools" }).click();
  await server.getByLabel("echo", { exact: true }).check();
  await server.getByLabel("add", { exact: true }).check();
  await server.getByLabel("add", { exact: true }).uncheck();

  const agents = form.getByTestId("agent-grants");
  await agents.getByRole("radio", { name: "Chosen agents" }).click();
  await agents.getByLabel(target.agent, { exact: true }).check();
  await shot(page, "mcp-grants-team");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form).toBeHidden();

  const saved = await send<Team>(request, "GET", `/teams/${team.id}`);
  expect(saved.mcp_access).toBe("list");
  expect(saved.mcp_servers).toEqual([{ server: target.server, all_tools: false, tools: ["echo"] }]);
  expect(saved.a2a_access).toBe("list");
  expect(saved.a2a_agents).toEqual([target.agent]);

  // A reload reads the stored grants back into the same controls.
  await page.reload();
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Edit team" }).click();
  const again = dialog(page).getByTestId("mcp-grants").getByRole("group", { name: target.server });
  await expect(again.getByLabel("echo", { exact: true })).toBeChecked();
  await expect(again.getByLabel("add", { exact: true })).not.toBeChecked();
  await expect(dialog(page).getByTestId("agent-grants").getByLabel(target.agent, { exact: true })).toBeChecked();
});

test("a member narrows the team's grants, and a name outside the parent's scope stays visible to remove", async ({ page, request }) => {
  const target = await seedTargets(request, "mg2", ["echo", "add"]);
  const team = await makeTeam(request, "mg2-team");
  const viewer = await memberId(request, VIEWER_EMAIL);
  // The team allows both tools and the agent; the member takes `add` and the agent; then the team narrows,
  // which leaves the member's grant outside its parent — the case the "Not offered" bucket exists for.
  await send(request, "PATCH", `/teams/${team.id}`, {
    mcp_access: "list",
    mcp_servers: [{ server: target.server, all_tools: false, tools: ["echo", "add"] }],
    a2a_access: "list",
    a2a_agents: [target.agent],
  });
  await send(request, "PUT", `/teams/${team.id}/members/${viewer}`, {
    role: "member",
    mcp_access: "list",
    mcp_servers: [{ server: target.server, all_tools: false, tools: ["add"] }],
    a2a_access: "list",
    a2a_agents: [target.agent],
  });
  await send(request, "PATCH", `/teams/${team.id}`, {
    mcp_servers: [{ server: target.server, all_tools: false, tools: ["echo"] }],
    a2a_agents: [],
  });

  await signInAsAdmin(page, request, `/teams/${team.id}`);
  await page.getByRole("tab", { name: "Members", exact: true }).click();
  await page.getByRole("row").filter({ hasText: VIEWER_EMAIL }).getByRole("button", { name: "Edit" }).click();
  const form = dialog(page);

  const mcp = form.getByTestId("mcp-grants");
  // The member has its own list already; the team's (Every …) choice is what a member without one shows.
  await expect(mcp.getByRole("radio", { name: "Chosen servers" })).toBeChecked();
  await expect(mcp.getByRole("radio", { name: "Same as the team" })).toBeVisible();
  const server = mcp.getByRole("group", { name: target.server });
  // The team names the tools, so "All tools" is not on offer and its tool list is the pick list.
  await expect(server.getByRole("radio", { name: "All tools" })).toHaveCount(0);
  await expect(server.getByLabel("echo", { exact: true })).toBeVisible();
  await expect(server.getByLabel("add", { exact: true })).toHaveCount(0);
  const staleTools = server.getByRole("group", { name: `${target.server} tools not offered` });
  await expect(staleTools).toContainText("outside the parent's scope");
  // A name in the bucket is one-way: ticking it takes it out of the grant and the bucket goes away.
  await staleTools.getByRole("checkbox", { name: /^add/ }).click();
  await expect(staleTools).toHaveCount(0);
  await server.getByLabel("echo", { exact: true }).check();

  const agents = form.getByTestId("agent-grants");
  await agents.getByRole("radio", { name: "Chosen agents" }).click();
  const staleAgents = agents.getByRole("group", { name: "A2A agents not offered" });
  await expect(staleAgents).toContainText("outside the parent's scope");
  await staleAgents.getByRole("checkbox", { name: new RegExp(`^${target.agent}`) }).click();
  await expect(staleAgents).toHaveCount(0);
  await shot(page, "mcp-grants-member");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form).toBeHidden();

  const members = await send<{ items: Member[] }>(request, "GET", `/teams/${team.id}/members`);
  const saved = members.items.find((member) => member.email === VIEWER_EMAIL);
  expect(saved?.mcp_servers).toEqual([{ server: target.server, all_tools: false, tools: ["echo"] }]);
  expect(saved?.a2a_agents).toEqual([]);
});

test("usage: the MCP and A2A tabs group by server, method and tool with failures and duration", async ({ page, request }) => {
  const row = (over: Record<string, unknown>) => ({
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
    cost_nano_usd: 0,
    unknown_cost_count: 0,
    ...over,
  });
  const mcp = [
    row({ kind: "mcp", target: "s1", method: "tools/call", tool: "echo", requests: 10, error_count: 2, total_duration_ms: 5_000 }),
    row({ kind: "mcp", target: "s1", method: "tools/list", tool: "", requests: 5, error_count: 0, total_duration_ms: 500 }),
  ];
  const a2a = [row({ kind: "a2a", target: "agent", method: "SendMessage", tool: "", requests: 4, error_count: 1, total_duration_ms: 12_000 })];
  const protocols = [
    row({ client_fallback: "falls_back", client_protocol: "2025-11-25", upstream_protocol: "2025-11-25", requests: 7, error_count: 0, total_duration_ms: 70 }),
    row({ client_fallback: "no_probe", client_protocol: "2025-06-18", upstream_protocol: "2026-07-28", requests: 2, error_count: 2, total_duration_ms: 20 }),
  ];
  const asked: { kind: string; group: string }[] = [];
  await page.route(/\/api\/v1\/usage\?/, async (route) => {
    const url = new URL(route.request().url());
    const kind = url.searchParams.get("kind") ?? "";
    asked.push({ kind, group: url.searchParams.get("group_by") ?? "" });
    const group = url.searchParams.get("group_by") ?? "";
    const items = kind === "mcp" ? (group.includes("client_fallback") ? protocols : mcp) : kind === "a2a" ? a2a : [];
    await route.fulfill({ json: { items, next_cursor: null } });
  });

  await signInAsAdmin(page, request, "/usage");
  // The LLM tab is the default and asks for LLM events only: MCP and A2A carry no model, tokens or cost.
  await expect(page.getByRole("tab", { name: "LLM" })).toHaveAttribute("aria-selected", "true");
  await expect.poll(() => asked.some((call) => call.kind === "llm")).toBe(true);
  await expect(page.getByText("Spend", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "MCP", exact: true }).click();
  // The MCP tab opens on server × method × tool, the most detailed read.
  await expect.poll(() => asked.some((call) => call.kind === "mcp" && call.group === "target,method,tool")).toBe(true);
  const table = page.getByRole("table");
  await expect(table.getByRole("columnheader", { name: "Server" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Method" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Tool" })).toBeVisible();
  await expect(table.getByRole("row").filter({ hasText: "s1" })).toHaveCount(2);
  const call = table.getByRole("row").filter({ hasText: "tools/call" });
  await expect(call).toContainText("echo");
  await expect(call).toContainText("20%");
  await expect(call).toContainText("500 ms");
  // 15 requests, 2 failures, 5.5 s in total: tokens and spend are not on this tab.
  await expect(page.getByText("Spend", { exact: true })).toHaveCount(0);
  await expect(page.getByText("15", { exact: true })).toBeVisible();
  await expect(page.getByText("13.3%", { exact: true })).toBeVisible();
  await expect(page.getByText("367 ms", { exact: true })).toBeVisible();
  await shot(page, "usage-mcp");

  // The group-by select asks the API for the dimensions it shows, and the table follows.
  await pick(page, "Group by", "Server");
  await expect.poll(() => asked.some((call2) => call2.kind === "mcp" && call2.group === "target")).toBe(true);
  await expect(table.getByRole("columnheader", { name: "Server" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Method" })).toHaveCount(0);

  // The protocol analysis: how clients negotiate, against the revisions on both sides.
  await pick(page, "Group by", "Fallback × client protocol × upstream protocol");
  await expect
    .poll(() => asked.some((call4) => call4.kind === "mcp" && call4.group === "client_fallback,client_protocol,upstream_protocol"))
    .toBe(true);
  for (const header of ["Fallback", "Client protocol", "Upstream protocol"]) {
    await expect(table.getByRole("columnheader", { name: header })).toBeVisible();
  }
  await expect(table.getByRole("row").filter({ hasText: "Falls back" })).toContainText("2025-11-25");
  const refused = table.getByRole("row").filter({ hasText: "Never probes" });
  await expect(refused).toContainText("2026-07-28");
  await expect(refused).toContainText("100%");

  await page.getByRole("tab", { name: "A2A", exact: true }).click();
  await expect.poll(() => asked.some((call3) => call3.kind === "a2a" && call3.group === "target,method")).toBe(true);
  const agent = page.getByRole("table").getByRole("row").filter({ hasText: "SendMessage" });
  await expect(agent).toContainText("agent");
  await expect(agent).toContainText("25%");
  await expect(agent).toContainText("3 s");
});

test("My keys shows a key's MCP and A2A reach and a connection snippet with a placeholder key", async ({ page, request }) => {
  const target = await seedTargets(request, "mg4", ["echo"]);
  const team = await makeTeam(request, "mg4-team");
  const viewer = await memberId(request, VIEWER_EMAIL);
  await send(request, "PATCH", `/teams/${team.id}`, {
    mcp_access: "list",
    mcp_servers: [{ server: target.server, all_tools: false, tools: ["echo"] }],
    a2a_access: "list",
    a2a_agents: [target.agent],
  });
  await send(request, "PUT", `/teams/${team.id}/members/${viewer}`, {
    role: "member",
    mcp_access: "list",
    mcp_servers: [{ server: target.server, all_tools: false, tools: ["echo"] }],
    a2a_access: "list",
    a2a_agents: [target.agent],
  });

  await signInAsMember(page, VIEWER, "/my-keys");
  const section = page.getByTestId(`team-${team.name}`);
  await expect(section.getByTestId("mcp-servers")).toContainText(target.server);
  await expect(section.getByTestId("mcp-servers")).toContainText("echo");
  await expect(section.getByTestId("callable-agents")).toContainText(target.agent);

  const snippet = section.getByTestId("connection-snippet");
  await expect(snippet).toContainText("X-Lumewarp-Key: lw-YOUR-KEY");
  await expect(snippet).toContainText("POST <gateway>/mcp");
  await expect(snippet).toContainText(`POST <gateway>/mcp/servers/${target.server}`);
  await expect(snippet).toContainText("GET  <gateway>/a2a");
  await expect(snippet).toContainText(`POST <gateway>/a2a/${target.agent}`);
  // No secret is ever part of the page: the block names a placeholder, never a key.
  expect(await snippet.textContent()).not.toMatch(/lw-(?!YOUR-KEY)[A-Za-z0-9_-]{16,}/);

  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await section.getByRole("button", { name: "Copy" }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(await snippet.textContent());
  await shot(page, "my-keys-mcp");
});
