import { expect, test, type APIRequestContext } from "@playwright/test";

import { createModel } from "./catalog-helpers";
import { confirmDialog, deleteTeams } from "./helpers";
import { Admin, memberSession, rowOf, signIn, VIEWER, VIEWER_EMAIL } from "./selfService";

/*
 * Model groups and the scoped pickers: an installation admin creates a group, an org is
 * restricted to a list, the team picker in it offers only that list, a group granted to the
 * team shows its members to the member picker, and a member narrows an own key to one model.
 * Everything the spec makes is s14-prefixed and removed afterwards (the e2e database is shared).
 */

let admin: Admin;

type Row = { id: string; name: string };

async function cleanup(request: APIRequestContext) {
  await deleteTeams(request, admin.headers, "s14-");
  for (const path of ["/orgs", "/model-groups", "/models", "/upstreams"]) {
    const rows = await admin.get<{ items: Row[] }>(`${path}?limit=200`);
    for (const row of rows.items.filter((item) => item.name.startsWith("s14"))) {
      const response = await request.delete(`/api/v1${path}/${row.id}`, { headers: admin.headers });
      expect(response.status(), `delete ${path} ${row.name}: ${await response.text()}`).toBe(204);
    }
  }
}

test.beforeEach(async ({ request }) => {
  admin = await Admin.login(request);
  await cleanup(request);
});

test.afterEach(async ({ request }) => {
  await cleanup(request);
});

test("a model group is granted down the hierarchy and a key narrows to one model", async ({ page, request, browser }) => {
  const upstream = await admin.post<Row>("/upstreams", { name: "s14-up", kind: "vllm", base_url: "http://127.0.0.1:9/v1" });
  for (const name of ["s14/a", "s14/b", "s14/c"]) {
    await createModel(request, admin.headers, { name, upstream_id: upstream.id, upstream_model: "m", model_type: "llm" });
  }
  await page.context().addCookies((await request.storageState()).cookies);

  // 1. Create a group of two models on the Model groups page.
  await page.goto("/model-groups");
  await expect(page.getByRole("heading", { name: "Model groups", level: 1 })).toBeVisible();
  await page.getByRole("button", { name: "New group" }).click();
  await page.getByLabel("Name", { exact: true }).fill("s14-pair");
  await page.getByRole("checkbox", { name: "s14/a" }).check();
  await page.getByRole("checkbox", { name: "s14/b" }).check();
  await page.getByRole("button", { name: "Create" }).click();
  await expect(rowOf(page, "s14-pair")).toContainText("2");
  const groups = await admin.get<{ items: { name: string; members: { name: string }[] }[] }>("/model-groups?limit=200");
  expect(groups.items.find((group) => group.name === "s14-pair")?.members.map((member) => member.name).sort()).toEqual(["s14/a", "s14/b"]);
  // A second group reaches outside the org's list below, so the team picker does not offer it.
  await admin.post("/model-groups", { name: "s14-wide", description: "", members: ["s14/a", "s14/c"] });

  // 2. Restrict an org to a list: the picker of a new team offers only that list.
  const org = await admin.post<Row>("/orgs", { name: "s14-org", model_access: "list", allowed_models: ["s14/a", "s14/b"] });
  await page.goto(`/orgs/${org.id}`);
  await page.getByRole("button", { name: "New team" }).click();
  const create = page.getByRole("dialog");
  await create.getByLabel("Name", { exact: true }).fill("s14-team");
  await create.getByRole("radio", { name: "Selected" }).click();
  await expect(create.getByRole("checkbox", { name: "s14/a" })).toBeVisible();
  await expect(create.getByRole("checkbox", { name: "s14/b" })).toBeVisible();
  await expect(create.getByRole("checkbox", { name: "s14/c" })).toHaveCount(0);
  await expect(create.getByRole("checkbox", { name: "s14-pair" })).toBeEnabled();
  await expect(create.getByRole("checkbox", { name: "s14-wide" })).toHaveCount(0);
  // Grant the group, not its members.
  await create.getByRole("checkbox", { name: "s14-pair" }).check();
  await create.getByRole("button", { name: "Create" }).click();
  await expect(create).toHaveCount(0);
  const teams = await admin.get<{ items: (Row & { model_access: string; allowed_models: string[]; allowed_groups: string[]; effective_models: string[] | null })[] }>("/teams?limit=200");
  const team = teams.items.find((item) => item.name === "s14-team")!;
  expect(team.model_access).toBe("list");
  expect(team.allowed_models).toEqual([]);
  expect(team.allowed_groups).toEqual(["s14-pair"]);
  expect([...(team.effective_models ?? [])].sort()).toEqual(["s14/a", "s14/b"]);

  // 3. The member picker shows the group's models, not the org's excluded one.
  await admin.putMember(team.id, VIEWER_EMAIL);
  await page.goto(`/teams/${team.id}`);
  await page.getByRole("tab", { name: "Members", exact: true }).click();
  await rowOf(page, VIEWER_EMAIL).getByRole("button", { name: "Edit", exact: true }).click();
  const member = page.getByRole("dialog");
  await member.getByRole("radio", { name: "Selected" }).click();
  await expect(member.getByRole("checkbox", { name: "s14/a" })).toBeVisible();
  await expect(member.getByRole("checkbox", { name: "s14/b" })).toBeVisible();
  await expect(member.getByRole("checkbox", { name: "s14/c" })).toHaveCount(0);
  await member.getByRole("button", { name: "Cancel" }).click();

  // 4. The member creates a key narrowed to one model.
  await signIn(page, VIEWER, "/my-keys");
  await page.getByTestId("team-s14-team").getByRole("button", { name: "New key" }).click();
  const form = page.getByRole("dialog", { name: "New key in s14-team" });
  await form.getByLabel("Name", { exact: true }).fill("s14-narrow");
  await form.getByRole("radio", { name: "Selected" }).click();
  await expect(form.getByRole("checkbox", { name: "s14/c" })).toHaveCount(0);
  await form.getByRole("checkbox", { name: "s14/a" }).check();
  await form.getByRole("button", { name: "Create key" }).click();
  await expect(page.getByRole("dialog", { name: "API key created" }).getByTestId("secret-value")).toHaveText(/^lw-/);
  await page.getByRole("button", { name: "I’ve saved it" }).click();
  const { context } = await memberSession(browser, VIEWER);
  try {
    const mine = (await (await context.request.get("/api/v1/users/me/keys?limit=200")).json()) as {
      items: { name: string; model_access: string; allowed_models: string[]; granted_models: string[]; owner_models: string[] | null }[];
    };
    const key = mine.items.find((item) => item.name === "s14-narrow")!;
    expect(key.model_access).toBe("list");
    expect(key.allowed_models).toEqual(["s14/a"]);
    expect(key.granted_models).toEqual(["s14/a"]);
    expect([...(key.owner_models ?? [])].sort()).toEqual(["s14/a", "s14/b"]);
  } finally {
    await context.close();
  }

  // 5. Delete the group: the confirmation says what it takes with it.
  await page.context().clearCookies();
  await page.context().addCookies((await request.storageState()).cookies);
  await page.goto("/model-groups");
  await rowOf(page, "s14-pair").getByRole("button", { name: "Delete" }).click();
  await expect(confirmDialog(page)).toContainText("1 grant");
  await confirmDialog(page).getByRole("button", { name: "Delete" }).click();
  await expect
    .poll(async () => (await admin.get<{ items: Row[] }>("/model-groups?limit=200")).items.map((group) => group.name))
    .not.toContain("s14-pair");
  await expect(rowOf(page, "s14-pair")).toHaveCount(0);
});
