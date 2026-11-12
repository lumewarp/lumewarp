---
title: Gateway API
outline: false
aside: false
---

# Gateway API

The API clients call, generated from the gateway's own endpoint and error tables
(`lumewarp openapi` prints it). Request and response bodies of the OpenAI-compatible
endpoints pass through, so this document lists only the fields the gateway itself reads;
every other field is the upstream's.

Download: <a href="/openapi/gateway.json" target="_blank" download>gateway.json</a>

<ClientOnly>
  <SwaggerUi spec="/openapi/gateway.json" />
</ClientOnly>
