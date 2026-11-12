---
title: Control plane API
outline: false
aside: false
---

<script setup>
import { withBase } from "vitepress";
</script>

# Control plane API

The management API, generated from the control plane's code. A running control plane
serves the same document at `/openapi.json` and an interactive copy at `/docs`.

Download: <a :href="withBase('/openapi/control-plane.json')" target="_blank" download>control-plane.json</a>

<ClientOnly>
  <SwaggerUi spec="/openapi/control-plane.json" />
</ClientOnly>
