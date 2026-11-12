import DefaultTheme from "vitepress/theme";
import type { Theme } from "vitepress";
import SwaggerUi from "./SwaggerUi.vue";
import "./custom.css";

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component("SwaggerUi", SwaggerUi);
  },
} satisfies Theme;
