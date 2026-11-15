/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";

/** Where the Day 9 FastAPI server listens during local development. */
const DEFAULT_API_TARGET = "http://127.0.0.1:8001";

export default defineConfig(({ mode }) => {
  // AEGIS_API_TARGET is read from the shell or a local .env.development file.
  // The proxy keeps the browser on one origin, so the API needs no CORS rules.
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.AEGIS_API_TARGET || DEFAULT_API_TARGET;

  return {
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/api/v1": {
          target: apiTarget,
        },
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
    },
  };
});
