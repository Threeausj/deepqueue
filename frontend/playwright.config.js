import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 45000,
  expect: { timeout: 10000 },
  use: {
    baseURL: "http://127.0.0.1:18765",
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: "../.venv/bin/python ../scripts/web_smoke_server.py",
    url: "http://127.0.0.1:18765/api/state",
    reuseExistingServer: false,
    timeout: 45000,
  },
});
