import { test, expect } from "@playwright/test";

const headers = { "X-DeepQueue": "1" };
test.beforeEach(async ({ page }) => {
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
              {
                model: "fixture-launch",
                efforts: ["low", "medium", "high"],
                default_effort: "medium",
              },
              {
                model: "fixture-estimate",
                efforts: ["low", "medium", "high"],
                default_effort: "medium",
              },
              {
                model: "fixture-low-only",
                efforts: ["low"],
                default_effort: "low",
              },
            ],
          ]),
        ),
        errors: {},
      },
    }),
  );
});
test.afterAll(async ({ request }) => {
  await request.post("/api/daemon", { headers, data: { action: "stop" } });
});

test("agent handoff keeps the queue unchanged and source summaries remain linked", async ({
  page,
  request,
  context,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  const before = (await (await request.get("/api/state")).json()).jobs.length;
  await page
    .getByRole("button", { name: "提交实验", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "提交实验", exact: true });
  await expect(dialog.getByRole("tab", { name: "Agent 提交" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await dialog
    .getByLabel("实验需求")
    .fill("比较三个学习率，训练结束后回到本对话总结验证指标。");
  await dialog.getByRole("button", { name: "继续改进", exact: true }).click();
  await expect(dialog.getByLabel("最多改进轮数")).toHaveValue("1");
  await dialog.getByLabel("最多改进轮数").fill("2");
  await dialog.locator(".submission-agent-settings summary").click();
  await dialog.getByLabel("资源预估模型").selectOption("gpt-5.6-luna");
  await dialog.getByLabel("启动适配模型").selectOption("fixture-launch");
  await dialog.getByLabel("归档与改进模型").selectOption("source-thread");
  await dialog.getByRole("button", { name: "复制给 Codex" }).click();
  await expect(
    dialog.getByRole("button", { name: "已复制，待 Agent 提交" }),
  ).toBeVisible();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("$deepqueue");
  expect(copied).toContain("--from-agent");
  expect(copied).toContain("CODEX_THREAD_ID");
  expect(copied).toContain("completion_mode=improve");
  expect(copied).toContain("max_improvement_rounds=2");
  expect(copied).toContain("--parent-job-id");
  expect(copied).toContain(
    'agent_models={"estimate":"gpt-5.6-luna","launch":"fixture-launch","archive":"source-thread"}',
  );
  expect((await (await request.get("/api/state")).json()).jobs.length).toBe(
    before,
  );
  await page.setViewportSize({ width: 320, height: 740 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("agent-submit-mobile.png"),
  });
  await dialog.getByRole("button", { name: "关闭" }).click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page
    .getByRole("button", {
      name: "Source conversation UI fixture",
      exact: true,
    })
    .click();
  const panel = page.getByRole("dialog", { name: "实验详情", exact: true });
  await expect(panel.getByRole("link", { name: "来源对话" })).toHaveAttribute(
    "href",
    "codex://threads/00000000-0000-0000-0000-000000000002",
  );
  await expect(panel.getByText("原对话已总结", { exact: true })).toBeVisible();
  await panel.getByRole("tab", { name: "Agent 归档", exact: true }).click();
  await expect(panel.getByRole("heading", { name: "回传总结" })).toBeVisible();
  await expect(panel.getByRole("cell", { name: "实际执行次数" })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("source-return-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("source-return-mobile.png"),
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  expect(errors).toEqual([]);
});

test("stage models persist through single and batch submission", async ({
  page,
  request,
}, testInfo) => {
  await page.goto("/");
  await page
    .getByRole("button", { name: "提交实验", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "提交实验", exact: true });
  await dialog.getByRole("tab", { name: "单个实验" }).click();
  await dialog.locator(".submission-agent-settings summary").click();
  await dialog.getByLabel("来源对话 ID").fill("model-choice-fixture");
  await dialog.getByLabel("资源预估模型").selectOption("fixture-estimate");
  await dialog.getByLabel("启动适配模型").selectOption("custom");
  await dialog
    .getByLabel("启动适配自定义模型 ID")
    .fill("provider/custom-launch");
  await dialog.getByLabel("归档与改进模型").selectOption("source-thread");
  await dialog.getByLabel("实验名称").fill("Model selection fixture");
  await dialog.getByLabel("运行命令").fill("true");
  await page.screenshot({
    path: testInfo.outputPath("stage-models-desktop.png"),
  });
  await page.setViewportSize({ width: 320, height: 740 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("stage-models-mobile.png"),
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await dialog.getByRole("button", { name: "提交实验", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const state = await (await request.get("/api/state")).json();
  const job = state.jobs.find((job) => job.title === "Model selection fixture");
  await request.post(`/api/jobs/${job.id}/control`, {
    headers,
    data: { action: "pause" },
  });
  const detail = await (await request.get(`/api/jobs/${job.id}`)).json();
  expect(detail.spec.agent_models).toEqual({
    estimate: "fixture-estimate",
    launch: "provider/custom-launch",
    archive: "source-thread",
  });
  const panel = page.getByRole("dialog", { name: "实验详情", exact: true });
  await expect(
    panel.getByText("使用会话当前模型", { exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByText("provider/custom-launch", { exact: true }),
  ).toBeVisible();
  await panel.getByRole("button", { name: "关闭实验详情" }).click();

  await page
    .getByRole("button", { name: "提交实验", exact: true })
    .first()
    .click();
  await dialog.getByRole("tab", { name: "实验批次" }).click();
  await dialog.locator(".submission-agent-settings summary").click();
  await dialog.getByLabel("来源对话 ID").fill("model-choice-fixture");
  await dialog.getByLabel("归档与改进模型").selectOption("source-thread");
  await dialog.getByLabel("实验清单 JSON").fill(
    JSON.stringify({
      name: "model-choice-batch",
      defaults: {
        server: "local",
        cwd: state.cwd,
        command: "true",
        agent_models: {
          estimate: "fixture-estimate",
          launch: "fixture-launch",
        },
      },
      experiments: [
        { key: "default" },
        { key: "custom", agent_models: { launch: "provider/batch-custom" } },
      ],
    }),
  );
  await dialog.getByRole("button", { name: "预览清单" }).click();
  await expect(dialog.getByText("2 个实验", { exact: true })).toBeVisible();
  await dialog.getByLabel("启动适配模型").selectOption("gpt-5.6-luna");
  await expect(dialog.getByRole("button", { name: "提交批次" })).toBeDisabled();
  await dialog.getByLabel("启动适配模型").selectOption("");
  await dialog.getByRole("button", { name: "预览清单" }).click();
  await expect(dialog.getByText("2 个实验", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "提交批次" }).click();
  await expect(dialog).not.toBeVisible();
  await request.post("/api/batch/control?name=model-choice-batch", {
    headers,
    data: { action: "pause" },
  });
  const batch = await (
    await request.get("/api/batch?name=model-choice-batch")
  ).json();
  expect(batch.experiments.map((job) => job.spec.agent_models)).toEqual([
    {
      estimate: "fixture-estimate",
      launch: "fixture-launch",
      archive: "source-thread",
    },
    {
      estimate: "fixture-estimate",
      launch: "provider/batch-custom",
      archive: "source-thread",
    },
  ]);
});

test("priority, independent pause scopes, and durable batch controls", async ({
  page,
  request,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "运行队列", exact: true }),
  ).toBeVisible();
  const row = page.getByRole("row").filter({
    has: page.getByRole("button", {
      name: "CPU baseline / lr 0.001 / seed 11",
      exact: true,
    }),
  });
  const state = await (await request.get("/api/state")).json();
  const selected = state.jobs.find(
    (job) => job.title === "CPU baseline / lr 0.001 / seed 11",
  );
  await row.getByRole("spinbutton", { name: "优先级" }).fill("60");
  await row.getByRole("spinbutton", { name: "优先级" }).press("Enter");
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/jobs/${selected.id}`)).json()).priority,
    )
    .toBe(60);
  await row.getByRole("button", { name: "暂停实验", exact: true }).click();
  await expect(row.getByText("已暂停", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "实验批次", exact: true }).click();
  const batchRow = page.getByRole("row").filter({
    has: page.getByRole("button", { name: "browser-cpu-sweep", exact: true }),
  });
  await batchRow.getByRole("button", { name: "暂停批次", exact: true }).click();
  await expect(
    batchRow.getByRole("button", { name: "恢复批次", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    batchRow.getByRole("button", { name: "恢复批次", exact: true }),
  ).toBeVisible();
  await batchRow.getByRole("button", { name: "恢复批次", exact: true }).click();
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/jobs/${selected.id}`)).json())
          .pause_sources,
    )
    .toEqual(["job"]);
  await request.post(`/api/jobs/${selected.id}/control`, {
    headers,
    data: { action: "resume" },
  });
  await page.getByRole("link", { name: /运行队列/ }).click();
  await expect(row.getByText("排队中", { exact: true })).toBeVisible();
  await expect(row.locator("td").first()).toHaveText("01");
  await page.screenshot({
    path: testInfo.outputPath("queue-desktop.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("result files and labelled agent archive work on desktop and mobile", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  await page
    .getByRole("button", {
      name: "CPU baseline / lr 0.001 / seed 7",
      exact: true,
    })
    .click();
  const panel = page.getByRole("dialog", { name: "实验详情", exact: true });
  await expect(panel).toBeVisible();
  await panel.getByRole("tab", { name: "结果", exact: true }).click();
  await expect(panel.getByText(/synthetic_scalar_objective/)).toBeVisible();
  await panel.getByRole("tab", { name: "日志", exact: true }).click();
  await expect(panel.locator(".log-output")).toContainText("learning_rate");
  await expect(
    panel.getByRole("button", { name: "复制 tmux 连接命令" }),
  ).toBeEnabled();
  await panel.getByRole("tab", { name: "Agent 归档", exact: true }).click();
  await expect(
    panel.getByText("ui-test-fixture", { exact: false }).first(),
  ).toBeVisible();
  await expect(
    panel.getByRole("link", { name: /在 Codex 中打开/ }),
  ).toHaveAttribute("href", /^codex:\/\/threads\//);
  await page.screenshot({
    path: testInfo.outputPath("archive-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(panel).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath("archive-mobile.png") });
  await panel.getByRole("button", { name: "关闭实验详情" }).click();
  await page.getByRole("button", { name: "展开导航" }).click();
  await page.getByRole("link", { name: "服务器资源", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "服务器资源", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("resources-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
});

test("browser batch submission executes once and streams real CPU logs", async ({
  page,
  request,
}, testInfo) => {
  await page.goto("/");
  await page
    .getByRole("button", { name: "提交实验", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "提交实验", exact: true });
  await dialog.getByRole("tab", { name: "实验批次", exact: true }).click();
  const state = await (await request.get("/api/state")).json();
  const manifest = {
    name: "browser-submitted",
    defaults: {
      server: "local",
      cwd: state.home,
      resources: { ram_mib: 128 },
      skip_estimate: true,
      launch_agent: false,
      tmux: false,
      archive: false,
    },
    experiments: [
      { key: "web-command", command: "printf browser-command-completed" },
    ],
  };
  await dialog
    .getByLabel("实验清单 JSON")
    .fill(JSON.stringify(manifest, null, 2));
  await dialog.getByRole("button", { name: "预览清单" }).click();
  await expect(dialog.getByText("1 个实验", { exact: true })).toBeVisible();
  expect(
    (await (await request.get("/api/state")).json()).batches.some(
      (batch) => batch.name === manifest.name,
    ),
  ).toBeFalsy();
  await page.screenshot({
    path: testInfo.outputPath("submit-preview.png"),
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "提交批次" }).click();
  await expect(dialog).not.toBeVisible();
  const batch = await (
    await request.get("/api/batch?name=browser-submitted")
  ).json();
  const id = batch.experiments[0].id;
  await expect(
    page.getByRole("button", { name: "启动调度", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "通用设置", exact: true }).click();
  await page.getByRole("button", { name: "启动调度", exact: true }).click();
  await expect
    .poll(
      async () => (await (await request.get(`/api/jobs/${id}`)).json()).status,
      { timeout: 30000 },
    )
    .toBe("succeeded");
  await page.getByRole("link", { name: "运行队列", exact: true }).click();
  await page.getByRole("button", { name: "web-command", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "实验详情", exact: true });
  await panel.getByRole("tab", { name: "日志", exact: true }).click();
  await expect(panel.locator(".log-output")).toContainText(
    "browser-command-completed",
  );
  const detail = await (await request.get(`/api/jobs/${id}`)).json();
  expect(detail.runs).toHaveLength(1);
  expect(detail.agents).toHaveLength(0);
  await panel.getByRole("button", { name: "关闭实验详情" }).click();
  await page.getByRole("link", { name: "通用设置", exact: true }).click();
  await page.getByRole("button", { name: "停止调度", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "启动调度", exact: true }),
  ).toBeVisible();
});

test("general settings persist, reload into the daemon, and submissions inherit them", async ({
  page,
  request,
  context,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "启动调度", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("实时连接", { exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: "通用设置", exact: true }).click();
  const runtime = page.getByRole("region", { name: "系统运行", exact: true });
  await expect(runtime.getByText("实时连接", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "启动调度", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "停止调度", exact: true }),
  ).toBeVisible();
  const daemon = (await (await request.get("/api/state")).json()).daemon.owner
    .pid;
  await expect(runtime.getByText("运行中", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "通用设置", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存设置", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("资源预估模型").selectOption("gpt-5.6-luna");
  await page.getByLabel("启动适配模型").selectOption("gpt-5.6-luna");
  await page.getByLabel("归档与改进模型").selectOption("source-thread");
  await page.getByLabel("资源预估推理强度").selectOption("low");
  await page.getByLabel("启动适配推理强度").selectOption("high");
  await page.getByLabel("归档与改进推理强度").selectOption("source-thread");
  await expect(page.getByLabel("自动修复次数上限")).toHaveValue("3");
  await page.getByLabel("自动修复次数上限").fill("5");
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.getByText("已保存", { exact: true })).toBeVisible();
  await expect
    .poll(
      async () =>
        (await (await request.get("/api/state")).json()).daemon.heartbeat
          .agent_efforts?.launch,
    )
    .toBe("high");
  await expect
    .poll(
      async () =>
        (await (await request.get("/api/state")).json()).daemon.heartbeat
          .launch_max_retries,
    )
    .toBe(5);
  expect(
    (await (await request.get("/api/state")).json()).daemon.owner.pid,
  ).toBe(daemon);
  await page.reload();
  await expect(page.getByLabel("启动适配推理强度")).toHaveValue("high");
  await expect(page.getByLabel("自动修复次数上限")).toHaveValue("5");
  await page.getByLabel("启动适配模型").selectOption("fixture-low-only");
  await expect(page.getByLabel("启动适配推理强度")).toHaveValue("low");
  expect(
    await page
      .getByLabel("启动适配推理强度")
      .locator('option[value="high"]')
      .count(),
  ).toBe(0);
  await page.getByRole("button", { name: "撤销修改", exact: true }).click();
  await expect(page.getByLabel("启动适配模型")).toHaveValue("gpt-5.6-luna");
  await expect(page.getByLabel("启动适配推理强度")).toHaveValue("high");
  await page.screenshot({
    path: testInfo.outputPath("general-settings-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 320, height: 740 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("general-settings-mobile.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "停止调度", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "启动调度", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: /运行队列/ }).click();
  await page
    .getByRole("button", { name: "提交实验", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "提交实验", exact: true });
  await expect(dialog.getByLabel("资源预估模型")).toHaveCount(0);
  await dialog
    .getByLabel("实验需求")
    .fill("使用通用设置提交实验并归档验证指标。");
  await dialog.getByRole("button", { name: "复制给 Codex" }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("使用通用设置");
  expect(copied).not.toContain("agent_models=");
  expect(copied).not.toContain("agent_efforts=");
  await dialog.getByRole("tab", { name: "单个实验" }).click();
  await dialog.getByLabel("来源对话 ID").fill("global-settings-fixture");
  await dialog.getByLabel("实验名称").fill("Global defaults fixture");
  await dialog.getByLabel("运行命令").fill("true");
  await page.screenshot({
    path: testInfo.outputPath("submit-with-global-defaults.png"),
  });
  await dialog.getByRole("button", { name: "提交实验", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const job = (await (await request.get("/api/state")).json()).jobs.find(
    (job) => job.title === "Global defaults fixture",
  );
  await request.post(`/api/jobs/${job.id}/control`, {
    headers,
    data: { action: "pause" },
  });
  const detail = await (await request.get(`/api/jobs/${job.id}`)).json();
  expect(
    Object.values(detail.spec.agent_models).every((value) => value === null),
  ).toBeTruthy();
  expect(
    Object.values(detail.spec.agent_efforts).every((value) => value === null),
  ).toBeTruthy();
  expect(detail.effective_agent_efforts).toEqual({
    estimate: "low",
    launch: "high",
    archive: "source-thread",
  });
  expect(errors).toEqual([]);
});

test("automatic repair history switches between each attempt's logs", async ({
  page,
  request,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page
    .getByRole("button", { name: "Automatic recovery UI fixture", exact: true })
    .click();
  const panel = page.getByRole("dialog", { name: "实验详情", exact: true });
  await expect(
    panel.getByRole("heading", { name: "运行记录 2 次" }),
  ).toBeVisible();
  const attempts = panel.locator(".run-attempt");
  await expect(attempts).toHaveCount(2);
  await attempts.nth(1).locator("summary").click();
  await expect(attempts.nth(1)).toContainText("OOM 自动修复");
  await expect(attempts.nth(1)).toContainText("--batch-size 2");
  await page.screenshot({
    path: testInfo.outputPath("recovery-history-desktop.png"),
  });
  await attempts.nth(1).getByRole("button", { name: "查看此次日志" }).click();
  await expect(panel.locator(".log-output")).toContainText(
    "recovered batch_size=2",
  );
  const state = await (await request.get("/api/state")).json();
  const job = state.jobs.find(
    (item) => item.title === "Automatic recovery UI fixture",
  );
  const detail = await (await request.get(`/api/jobs/${job.id}`)).json();
  await panel
    .getByLabel("运行记录", { exact: true })
    .selectOption(detail.runs[0].id);
  await expect(panel.locator(".log-output")).toContainText(
    "CUDA out of memory",
  );
  await expect(panel.locator(".log-output")).not.toContainText(
    "recovered batch_size=2",
  );
  await page.setViewportSize({ width: 320, height: 740 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("recovery-logs-mobile.png"),
  });
  await panel
    .getByRole("button", { name: "实时日志 · tmux", exact: true })
    .click();
  await expect(panel.getByLabel("运行记录", { exact: true })).toHaveValue("");
  await expect(panel.locator(".log-output")).toContainText(
    "recovered batch_size=2",
  );
  expect(errors).toEqual([]);
});

test("completion policy and round limit can change from experiment details", async ({
  page,
  request,
}, testInfo) => {
  const submitted = await request.post("/api/jobs", {
    headers: { "X-DeepQueue": "1" },
    data: {
      server: "local",
      cwd: "/tmp",
      title: "Runtime policy UI fixture",
      command: "true",
      source_thread_id: "workflow-ui-source",
      resources: { gpu_count: 256 },
      skip_estimate: true,
    },
  });
  expect(submitted.ok()).toBeTruthy();
  const job = await submitted.json();
  await page.goto(`/#view=queue&job=${job.id}`);
  const panel = page.getByRole("dialog", { name: "实验详情", exact: true });
  await expect(panel.getByLabel("最多改进轮数")).toHaveValue("1");
  await panel.getByLabel("最多改进轮数").fill("2");
  await panel.getByRole("button", { name: "保存轮数" }).click();
  await expect(panel.getByRole("button", { name: "保存轮数" })).toBeDisabled();
  await panel.getByRole("button", { name: "继续改进", exact: true }).click();
  await expect(
    panel.getByRole("button", { name: "继续改进", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  const updated = await (await request.get(`/api/jobs/${job.id}`)).json();
  expect(updated.spec.max_improvement_rounds).toBe(2);
  expect(updated.completion_mode).toBe("improve");
  expect(updated.spec.command).toBe("true");
  await page.reload();
  await expect(panel.getByLabel("最多改进轮数")).toHaveValue("2");
  await panel.getByRole("button", { name: "结束", exact: true }).click();
  await expect(
    panel.getByRole("button", { name: "结束", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  const finished = await (await request.get(`/api/jobs/${job.id}`)).json();
  expect(finished.spec.completion_mode).toBe("finish");
  expect(finished.status).toBe("queued");
  await page.setViewportSize({ width: 320, height: 740 });
  await expect(panel.getByLabel("最多改进轮数")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("runtime-policy-mobile.png"),
  });
});
