import type { Key } from "../api";

/**
 * The suspension reasons the server lets a key's owner resume by themselves. This mirrors the
 * owner branch of `canResumeKey`: an admin suspension, an owner deactivation or any unknown reason
 * needs an administrator, not the member's own Resume button.
 */
export function ownerResumableReason(key: Key) {
  return (
    key.suspended_reason === null ||
    key.suspended_reason === "owner" ||
    // An owner who is back on the team may resume keys suspended by their removal.
    key.suspended_reason === "owner_removed"
  );
}
