import { defineConfig } from "@playwright/test";

// The e2e suite starts its own backend (tests/frontend/start_server.py) with fake agent
// executables and a temporary workspace, then drives the production build in Chromium.
export default defineConfig({
  testDir: "../tests/frontend",
  testMatch: /.*\.spec\.ts/,
  timeout: 90_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.WAW_E2E_URL || "http://127.0.0.1:8799",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "../.venv/bin/python ../tests/frontend/start_server.py",
    url: (process.env.WAW_E2E_URL || "http://127.0.0.1:8799") + "/api/v1/health",
    reuseExistingServer: !!process.env.WAW_E2E_URL,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
