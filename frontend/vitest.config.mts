import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";

// unit tests for lib/ and components/ (next 16 guide: node_modules/next/dist/docs/01-app/02-guides/testing/vitest.md)
export default defineConfig({
  plugins: [tsconfigPaths(), react()],
  test: {
    environment: "jsdom",
  },
});
