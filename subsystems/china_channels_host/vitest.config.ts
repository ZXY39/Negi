import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      // Mirrors tsconfig `paths` and scripts/rewrite-shared-imports.mjs:
      // the dist build rewrites `@openclaw-china/shared` to relative paths
      // after tsc emits; tests run against source, so alias the specifier
      // back to the shared source entry.
      "@openclaw-china/shared": fileURLToPath(
        new URL("./src/vendor/shared/index.ts", import.meta.url)
      ),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      // Asserts upstream OpenClaw packaging artifacts (openclaw.plugin.json,
      // package.json, skills/) that are not vendored into this repo.
      "**/manifest.skills.test.ts",
    ],
  },
});
