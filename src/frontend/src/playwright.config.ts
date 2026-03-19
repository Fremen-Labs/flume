import { defineConfig } from "@playwright/test";

export default defineConfig({
  // Keep this config minimal so the repo doesn't depend on Lovable tooling.
  timeout: 60_000,
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:8080",
    trace: "on-first-retry",
  },
});
