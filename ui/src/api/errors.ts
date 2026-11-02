export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code = "error",
    /** Request field a 422 refers to (last segment of the server's `field.path: message`). */
    public field?: string,
    /** Seconds from a 429 `Retry-After` header, when the server sent one. */
    public retryAfter?: number,
  ) {
    super(message);
  }
}

/** The server reports validation failures as `<field path>: <message>`; extract the leaf field. */
export function fieldOf(message: string): string | undefined {
  const m = /^([A-Za-z_][\w.-]*): /.exec(message);
  return m?.[1].split(".").pop();
}
