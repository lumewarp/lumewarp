<script setup lang="ts">
// Renders one OpenAPI document with Swagger UI. The bundle touches `window`, so it is
// imported in the browser only.
import { onMounted, ref } from "vue";
import { withBase } from "vitepress";
import "swagger-ui-dist/swagger-ui.css";

const props = defineProps<{ spec: string }>();
const root = ref<HTMLElement>();

onMounted(async () => {
  const { default: SwaggerUIBundle } = await import(
    "swagger-ui-dist/swagger-ui-es-bundle.js"
  );
  SwaggerUIBundle({
    url: withBase(props.spec),
    domNode: root.value,
    deepLinking: true,
    docExpansion: "none",
    defaultModelsExpandDepth: 0,
    // The manual is static: nothing here can reach a deployment, so no "Try it out".
    supportedSubmitMethods: [],
  });
});
</script>

<template>
  <div ref="root" class="swagger vp-raw" />
</template>
