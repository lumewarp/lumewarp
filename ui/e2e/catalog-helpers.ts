import { expect, type APIRequestContext } from "@playwright/test";

/**
 * A model is a global definition and is served only where it is deployed. Most specs want a model that works
 * in one zone, so this creates the definition and PUTs its deployment: in `zone` when given, else in the
 * zone of the upstream (the default zone for seeded upstreams). `chat_check` rides on the deployment.
 */
export interface ModelWithDeployment {
  name: string;
  upstream_id: string;
  upstream_model: string;
  model_type?: string;
  zone?: string;
  chat_check?: object | null;
  [definition: string]: unknown;
}

export async function createModel(request: APIRequestContext, headers: Record<string, string>, body: ModelWithDeployment): Promise<{ id: string; name: string }> {
  const { upstream_id, upstream_model, zone, chat_check, ...definition } = body;
  const created = await request.post("/api/v1/models", { headers, data: { model_type: "llm", ...definition } });
  expect(created.status(), await created.text()).toBe(201);
  const model = (await created.json()) as { id: string; name: string };
  await deployModel(request, headers, model.id, { upstream_id, upstream_model, zone, chat_check });
  return model;
}

/** Deploy an existing model; the zone defaults to the upstream's. */
export async function deployModel(
  request: APIRequestContext,
  headers: Record<string, string>,
  modelId: string,
  d: { upstream_id: string; upstream_model: string; zone?: string; chat_check?: object | null },
) {
  let zone = d.zone;
  if (!zone) {
    const listed = await request.get("/api/v1/upstreams?limit=200", { headers });
    zone = ((await listed.json()).items as { id: string; zone: string }[]).find((u) => u.id === d.upstream_id)?.zone;
  }
  expect(zone, "the upstream's zone").toBeTruthy();
  const put = await request.put(`/api/v1/models/${modelId}/deployments/${zone}`, {
    headers,
    data: { upstream_id: d.upstream_id, upstream_model: d.upstream_model, chat_check: d.chat_check ?? null },
  });
  expect(put.status(), await put.text()).toBe(200);
}
