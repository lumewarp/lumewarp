import { expect, test, type Page } from "@playwright/test";

import { signIn, VIEWER } from "./selfService";

/*
 * The Playground page against the real control plane for the session, with the playground API mocked: the
 * list of usable keys and the chat completion stream. No gateway is needed.
 */

const KEY = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "pg-key",
  display_prefix: "lw_abcd",
  team_id: "22222222-2222-4222-8222-222222222222",
  team_name: "pg-team",
  models: ["gpt-a", "gpt-b"],
};

const sse = (...chunks: object[]) => chunks.map((c) => `data: ${JSON.stringify(c)}\n\n`).join("") + "data: [DONE]\n\n";
const delta = (content: object, extra: object = {}) => ({
  id: "c1",
  object: "chat.completion.chunk",
  created: 1,
  model: "gpt-a",
  choices: [{ index: 0, delta: content, finish_reason: null }],
  ...extra,
});

async function mockInfo(page: Page, body: object) {
  await page.route(
    (url) => url.pathname === "/api/v1/playground",
    (route) => route.fulfill({ json: body }),
  );
}

test("sends the act-as and CSRF headers, streams text and shows the request id and usage", async ({ page }) => {
  await mockInfo(page, { enabled: true, keys: [KEY] });
  let seen: { headers: Record<string, string>; body: { model: string; messages: { role: string; content: string }[]; temperature?: number; stream?: boolean; chat_template_kwargs?: unknown } } | undefined;
  await page.route("**/api/v1/playground/v1/chat/completions", async (route) => {
    seen = { headers: route.request().headers(), body: route.request().postDataJSON() };
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "x-lumewarp-request-id": "req-123" },
      body: sse(
        delta({ reasoning_content: "thinking hard" }),
        delta({ content: "\n\n " }),
        delta({ content: "Hello " }),
        delta({ content: "**world**" }),
        {
          id: "c1",
          object: "chat.completion.chunk",
          created: 1,
          model: "gpt-a",
          choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
          usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 },
        },
      ),
    });
  });

  await signIn(page, VIEWER, "/playground");
  await expect(page.getByRole("heading", { name: "Playground", level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: "Playground" })).toBeVisible();

  await page.getByLabel("Request body (JSON)").fill('{"temperature": 0.2, "model": "ignored", "chat_template_kwargs": {"enable_thinking": false}}');
  await page.getByLabel("Message").fill("Hi there");
  await page.getByLabel("Message").press("Enter");

  await expect(page.getByTestId("assistant-text")).toHaveText("Hello world");
  await expect(page.getByTestId("assistant-text").locator("strong")).toHaveText("world");
  await expect(page.getByTestId("turn-meta")).toContainText(/TTFT \d+ ms/);
  await expect(page.getByTestId("turn-meta")).toContainText("req-123");
  await expect(page.getByTestId("turn-meta")).toContainText("Output tokens3");
  // Reasoning is collapsed until opened.
  await expect(page.getByTestId("reasoning-text")).toHaveCount(0);
  await page.getByRole("button", { name: /Thought for/ }).click();
  await expect(page.getByTestId("reasoning-text")).toHaveText("thinking hard");

  expect(seen?.headers["x-lumewarp-act-as"]).toBe(KEY.id);
  expect(seen?.headers["x-lumewarp-csrf"]).toBeTruthy();
  expect(seen?.headers["authorization"]).toBeUndefined();
  expect(seen?.body.model).toBe("gpt-a");
  expect(seen?.body.messages).toEqual([{ role: "user", content: "Hi there" }]);
  expect(seen?.body.temperature).toBe(0.2);
  expect(seen?.body.chat_template_kwargs).toEqual({ enable_thinking: false });
  expect(seen?.body.stream).toBe(true);

  // A long transcript scrolls inside the chat card; the page itself never scrolls.
  await page.getByTestId("assistant-text").evaluate((el) => {
    el.textContent = "line\n".repeat(400);
  });
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight)).toBe(true);
  const log = page.getByRole("log", { name: "Transcript" });
  expect(await log.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);

  await page.getByRole("button", { name: "Clear" }).click();
  await expect(page.getByTestId("turn-assistant")).toHaveCount(0);
});

test("shows a gateway error inline in the transcript", async ({ page }) => {
  await mockInfo(page, { enabled: true, keys: [KEY] });
  await page.route("**/api/v1/playground/v1/chat/completions", (route) =>
    route.fulfill({
      status: 429,
      headers: { "x-lumewarp-request-id": "req-429" },
      json: { error: { message: "Rate limit exceeded", type: "rate_limit_error", code: "rate_limited" } },
    }),
  );
  await signIn(page, VIEWER, "/playground");
  await page.getByLabel("Message").fill("Hi");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("turn-error")).toContainText("Rate limit exceeded");
  await expect(page.getByTestId("turn-meta")).toContainText("req-429");
});

test("says so when the playground is not configured", async ({ page }) => {
  await mockInfo(page, { enabled: false, keys: [] });
  await signIn(page, VIEWER, "/playground");
  await expect(page.getByTestId("playground-disabled")).toContainText("isn't configured");
  await expect(page.getByLabel("Message")).toHaveCount(0);
});

test("links to My keys when there is no usable key", async ({ page }) => {
  await mockInfo(page, { enabled: true, keys: [] });
  await signIn(page, VIEWER, "/playground");
  const empty = page.getByTestId("playground-no-keys");
  await expect(empty).toBeVisible();
  await empty.getByRole("link", { name: "My keys" }).click();
  await expect(page).toHaveURL(/\/my-keys$/);
});

test("does not send a failed turn again", async ({ page }) => {
  await mockInfo(page, { enabled: true, keys: [KEY] });
  const bodies: { messages: { role: string; content: string }[] }[] = [];
  await page.route("**/api/v1/playground/v1/chat/completions", async (route) => {
    bodies.push(route.request().postDataJSON());
    if (bodies.length === 1) {
      await route.fulfill({ status: 429, json: { error: { code: "rate_limited", message: "Rate limit exceeded" } } });
    } else {
      await route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: sse(delta({ content: "ok" })) });
    }
  });
  await signIn(page, VIEWER, "/playground");
  await page.getByLabel("Message").fill("first");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("turn-error")).toBeVisible();
  await page.getByLabel("Message").fill("second");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("assistant-text")).toHaveText("ok");
  expect(bodies[1]?.messages).toEqual([{ role: "user", content: "second" }]);
  await expect(page.getByTestId("turn-user")).toHaveCount(2);
});
