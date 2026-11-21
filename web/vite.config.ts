import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "/static/",
  plugins: [react()],
  test: { environment: "jsdom" },
});
