import type { Key, Schemas } from "../api";

/**
 * A mutation result must never carry the one-time secret: React Query keeps a mutation's data for its
 * gcTime, so a `reset()` on the observer would still leave the secret in `getMutationCache()`. The
 * caller gets the secret first; the mutation resolves with the key only. Every secret-bearing flow
 * (create and rotate, on My keys and the team Keys panel) goes through here.
 */
export async function runSecretMutation(
  fn: () => Promise<Schemas["KeyCreated"]>,
  onSecret: (secret: string, key: Key) => void,
): Promise<Key> {
  const created = await fn();
  onSecret(created.secret, created.key);
  return created.key;
}
