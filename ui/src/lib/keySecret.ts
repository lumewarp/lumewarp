import type { Key, Schemas } from "../api";

/**
 * A mutation result must never carry the one-time secret: React Query keeps a mutation's data for its
 * gcTime, so a `reset()` on the observer would still leave the secret in `getMutationCache()`. The
 * caller gets the secret first; the mutation resolves with the key only. Every secret-bearing flow
 * (key create and rotate, personal and gateway tokens) goes through this file, the only reader of
 * a response's secret field.
 */
export async function runSecretMutation(
  fn: () => Promise<Schemas["KeyCreated"]>,
  onSecret: (secret: string, key: Key) => void,
): Promise<Key> {
  const created = await fn();
  onSecret(created.secret, created.key);
  return created.key;
}

/** The same rule for a token with no key to return: the mutation resolves with nothing. */
export async function runTokenSecretMutation(
  fn: () => Promise<{ secret: string }>,
  onSecret: (secret: string) => void,
): Promise<void> {
  onSecret((await fn()).secret);
}
