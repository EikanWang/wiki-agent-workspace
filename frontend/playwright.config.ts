import { defineConfig } from "@playwright/test";

const url = process.env.WAW_E2E_URL || "http://127.0.0.1:8799";

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
    baseURL: url,
    trace: "retain-on-failure",
  },
  webServer: {
    command: "../.venv/bin/python ../tests/frontend/start_server.py",
    url: url + "/api/v1/health",
    // the server listens where the tests look (and names the files it leaves for them after this port)
    env: { ...process.env, WAW_E2E_PORT: new URL(url).port || "80" } as Record<string, string>,
    reuseExistingServer: !!process.env.WAW_E2E_URL,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
    // SIGTERM (not the default SIGKILL) so start_server.py can stop its tmux server and delete its temp folder
    gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
  },
});
