import { test, expect } from "@playwright/test";

const headers = { "X-DeepQueue": "1" };

test("server workspaces scope navigation, submissions, batch controls and archives", async ({
  page,
  request,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/models", (route) =>
    route.fulfill({
      json: {
        models: Object.fromEntries(
          ["estimate", "launch", "archive"].map((phase) => [
            phase,
            [
              {
                model: "gpt-5.6-luna",
                efforts: ["low", "medium", "high"],
                default_effort: "medium",
              },
            ],
          ]),
        ),
        errors: {},
      },
    }),
  );
  const state = await (await request.get("/api/state")).json();
  const manifest = {
    name: "workspace-shared",
    defaults: {
      server: "local",
      cwd: state.home,
      command: "true",
      resources: { ram_mib: 128 },
      skip_estimate: true,
      archive: false,
      launch_agent: false,
      tmux: false,
    },
    experiments: [
      { key: "local", title: "Local workspace experiment" },
      {
        key: "remote",
        title: "Remote workspace experiment",
        server: "remote-ui",
      },
    ],
  };
  const submission = await request.post("/api/batches", {
    headers,
    data: manifest,
  });
  expect(submission.ok(), await submission.text()).toBeTruthy();
  const submitted = await submission.json();
  const archives = [];
  for (const server of ["local", "remote-ui"]) {
    const job = await (
      await request.post("/api/jobs", {
        headers,
        data: {
          ...manifest.defaults,
          server,
          archive: true,
          title: `${server} archived experiment`,
        },
      })
    ).json();
    archives.push(job);
    await request.post(`/api/jobs/${job.id}/control`, {
      headers,
      data: { action: "cancel" },
    });
  }
  try {
    await page.goto("/#view=queue&server=local");
    await page.getByRole("button", { name: "展开侧栏", exact: true }).click();
    const nav = page.getByRole("navigation", { name: "主导航" });
    await expect(
      nav.getByRole("group", { name: "local 工作空间", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: "Local workspace experiment",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: "Remote workspace experiment",
        exact: true,
      }),
    ).toHaveCount(0);
    await nav
      .getByRole("button", { name: "服务器 remote-ui", exact: true })
      .click();
    await expect(page).toHaveURL(/server=remote-ui/);
    await expect(
      page.getByRole("button", {
        name: "Remote workspace experiment",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: "Local workspace experiment",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(page.getByLabel("服务器筛选")).toHaveCount(0);
    await page
      .getByRole("button", { name: "提交实验", exact: true })
      .first()
      .click();
    const dialog = page.getByRole("dialog", { name: "提交实验", exact: true });
    await expect(
      dialog.getByRole("combobox", { name: "服务器", exact: true }),
    ).toHaveValue("remote-ui");
    await expect(
      dialog.getByRole("combobox", { name: "服务器", exact: true }),
    ).toBeDisabled();
    await dialog.getByRole("tab", { name: "实验批次", exact: true }).click();
    const payload = JSON.parse(
      await dialog.getByLabel("实验清单 JSON").inputValue(),
    );
    expect(payload.defaults.server).toBe("remote-ui");
    await dialog.getByLabel("实验清单 JSON").fill(
      JSON.stringify({
        ...manifest,
        name: "workspace-rejected",
        defaults: { ...manifest.defaults, server: "remote-ui" },
        experiments: [{ key: "foreign", server: "local" }],
      }),
    );
    await dialog.getByRole("button", { name: "预览清单", exact: true }).click();
    await expect(dialog.getByRole("alert")).toContainText("workspace server");
    await expect(
      dialog.getByRole("button", { name: "提交批次", exact: true }),
    ).toBeDisabled();
    await dialog.getByRole("button", { name: "关闭", exact: true }).click();

    await nav.getByRole("link", { name: "实验批次", exact: true }).click();
    const row = page.getByRole("row").filter({
      has: page.getByRole("button", { name: manifest.name, exact: true }),
    });
    await expect(row.locator(".batch-progress")).toContainText("0 / 1");
    await expect(
      page.getByRole("button", { name: "browser-cpu-sweep", exact: true }),
    ).toHaveCount(0);
    await row.getByRole("button", { name: "暂停批次", exact: true }).click();
    await expect(
      row.getByRole("button", { name: "恢复批次", exact: true }),
    ).toBeVisible();
    expect(
      (await (await request.get(`/api/jobs/${submitted.jobs.local}`)).json())
        .pause_sources,
    ).toEqual([]);
    expect(
      (await (await request.get(`/api/jobs/${submitted.jobs.remote}`)).json())
        .pause_sources,
    ).toContain("batch");
    await page.reload();
    await expect(
      row.getByRole("button", { name: "恢复批次", exact: true }),
    ).toBeVisible();
    await row
      .getByRole("button", { name: `打开批次 ${manifest.name}`, exact: true })
      .click();
    await expect(page).toHaveURL(/server=remote-ui.*batch=workspace-shared/);
    await expect(
      page.getByRole("button", {
        name: "Remote workspace experiment",
        exact: true,
      }),
    ).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("server-workspace-desktop.png"),
    });

    await nav.getByRole("link", { name: "Agent 归档", exact: true }).click();
    await expect(page.locator(".archive-title strong")).toHaveText([
      "remote-ui archived experiment",
    ]);
    await nav.getByRole("link", { name: "Codex 工作区", exact: true }).click();
    await expect(
      page.getByRole("combobox", { name: "Codex 服务器", exact: true }),
    ).toHaveValue("remote-ui");
    await expect(
      nav.getByRole("link", { name: "Codex 工作区", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await nav.getByRole("link", { name: "服务器资源", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "local 接入配置", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "remote-ui 接入配置", exact: true }),
    ).toBeVisible();
    await nav.getByRole("link", { name: "通用设置", exact: true }).click();
    await expect(
      page.getByRole("region", { name: "系统运行", exact: true }),
    ).toBeVisible();

    await page.setViewportSize({ width: 320, height: 740 });
    await page.getByRole("button", { name: "展开导航", exact: true }).click();
    await nav
      .getByRole("button", { name: "服务器 local", exact: true })
      .click();
    await expect(page).toHaveURL(/view=queue&server=local/);
    await expect(
      page.getByRole("button", {
        name: "Local workspace experiment",
        exact: true,
      }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await page.getByRole("button", { name: "展开导航", exact: true }).click();
    await page.screenshot({
      path: testInfo.outputPath("server-workspace-mobile.png"),
      animations: "disabled",
    });
    await page
      .getByRole("button", { name: "关闭导航", exact: true })
      .click({ position: { x: 310, y: 100 } });
    await page.goto("/#view=queue&server=nonexistent-workspace");
    await expect(
      page.getByText("未找到此服务器", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: "Local workspace experiment",
        exact: true,
      }),
    ).toHaveCount(0);
    await page.goto(`/#view=queue&job=${archives[1].id}`);
    await expect(page).toHaveURL(/server=remote-ui/);
    await expect(
      page.getByRole("dialog", { name: "实验详情", exact: true }),
    ).toBeVisible();
    expect(errors).toEqual([]);
  } finally {
    await request.post(`/api/batch/control?name=${manifest.name}`, {
      headers,
      data: { action: "cancel" },
    });
  }
});
