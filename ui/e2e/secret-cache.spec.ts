import { QueryClient } from "@tanstack/react-query";
import { expect, test } from "@playwright/test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { Key, Schemas } from "../src/api";
import { runSecretMutation, runTokenSecretMutation } from "../src/lib/keySecret";

/*
 * A real QueryClient, not the DOM: React Query keeps a mutation's result for gcTime, so a `reset()` on
 * the observer would still leave a secret in `getMutationCache()`. The shared path hands the secret to
 * the dialog and resolves with the key, which is what this checks. The dialog's own copy is dropped on
 * close; after that nothing in the mutation cache may contain it.
 */
test("a secret never stays in the mutation cache after its dialog closes", async () => {
  const client = new QueryClient();
  const secret = "FAKE_SECRET_NEVER_REAL";
  const key = { id: "00000000-0000-4000-8000-000000000001" } as Key;
  let dialogSecret: string | null = null;
  const mutation = client.getMutationCache().build(client, {
    mutationFn: () =>
      runSecretMutation(
        async () => ({ secret, key }) as Schemas["KeyCreated"],
        (value) => {
          dialogSecret = value;
        },
      ),
  });
  await mutation.execute(undefined);
  expect(dialogSecret).toBe(secret);
  expect(mutation.state.data).toEqual(key);

  // The dialog closed: its state is gone. The cache must not hold a second copy.
  dialogSecret = null;
  expect(JSON.stringify(client.getMutationCache().getAll())).not.toContain(secret);
});

test("a token secret stays out of the mutation cache too", async () => {
  const client = new QueryClient();
  const secret = "FAKE_TOKEN_NEVER_REAL";
  let dialogSecret: string | null = null;
  const mutation = client.getMutationCache().build(client, {
    mutationFn: () =>
      runTokenSecretMutation(
        async () => ({ secret }),
        (value) => {
          dialogSecret = value;
        },
      ),
  });
  await mutation.execute(undefined);
  expect(dialogSecret).toBe(secret);
  expect(JSON.stringify(client.getMutationCache().getAll())).not.toContain(secret);
});

test("only the shared path reads a response's secret field", () => {
  const root = fileURLToPath(new URL("../src", import.meta.url));
  const readers = readdirSync(root, { recursive: true, encoding: "utf8" })
    .filter((file) => /\.tsx?$/.test(file) && !file.endsWith("schema.d.ts"))
    .filter((file) => /[\w)?]\.secret\b/.test(readFileSync(join(root, file), "utf8")));
  expect(readers).toEqual([join("lib", "keySecret.ts")]);
});
