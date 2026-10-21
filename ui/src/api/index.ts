import { httpApi } from "./http";
import { mockApi } from "./mock";
import type { Api } from "./types";

/** `VITE_API_MOCK=1` forces mock data; unset defaults to mock in dev, real API in production builds. */
const flag = import.meta.env.VITE_API_MOCK;
export const useMock = flag === undefined || flag === "" ? import.meta.env.DEV : flag === "1" || flag === "true";

export const api: Api = useMock ? mockApi : httpApi;
export { ApiError } from "./errors";
export type * from "./types";
