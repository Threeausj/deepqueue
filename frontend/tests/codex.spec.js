import { test, expect } from "@playwright/test";
const headers = { "X-DeepQueue": "1" };

test.afterAll(async ({ request }) => {
  const config = (await (await request.get("/api/servers/local/codex")).json())
    .config;
  await request.post("/api/servers/local/codex/settings", {
    headers,
    data: { ...config, enabled: false },
  });
});

test("server Codex workspace connects, streams, handles requests and preserves tasks", async ({
  page,
  request,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/#view=codex&server=local");
  await expect(
    page.getByRole("region", { name: "Codex 工作区", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Codex 工作区", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "连接设置", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "连接 local 的 Codex" });
  await dialog.getByLabel("启用网页连接与原任务反馈").check();
  await dialog.getByRole("button", { name: "保存连接设置" }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: "连接 Codex", exact: true }).click();
  await expect(page.getByText("已连接", { exact: true })).toBeVisible();
  const project = page.getByRole("list", {
    name: "视觉实验的任务",
    exact: true,
  });
  const independent = page.getByRole("list", {
    name: "独立任务的任务",
    exact: true,
  });
  await expect(
    project.getByRole("button", { name: /Codex 协作示例/ }),
  ).toBeVisible();
  await expect(
    independent.getByRole("button", { name: /独立分析任务/ }),
  ).toBeVisible();
  await expect(
    project.getByRole("button", { name: /独立分析任务/ }),
  ).toHaveCount(0);
  const folder = page
    .locator(".codex-project-toggle")
    .filter({ hasText: "视觉实验" });
  await folder.click();
  await expect(project).toHaveCount(0);
  await page.reload();
  await expect(folder).toHaveAttribute("aria-expanded", "false");
  await folder.click();
  const search = page.getByRole("textbox", { name: "搜索 Codex 任务" });
  await search.fill("独立分析");
  await expect(
    page.getByRole("button", { name: /Codex 协作示例/ }),
  ).toHaveCount(0);
  await expect(
    independent.getByRole("button", { name: /独立分析任务/ }),
  ).toBeVisible();
  await search.fill("");
  await page.getByRole("button", { name: /Codex 协作示例/ }).click();
  const input = page.getByRole("textbox", { name: "发给 Codex 的消息" });
  const log = page.getByRole("log", { name: "Codex 对话内容" });
  await expect(input).toBeEnabled();
  expect((await log.boundingBox()).height).toBeGreaterThan(550);
  await page
    .getByRole("combobox", { name: "Codex 对话模型" })
    .selectOption("gpt-5.6-luna");
  await page
    .getByRole("combobox", { name: "Codex 推理强度" })
    .selectOption("high");
  await input.fill("比较两次实验的验证精度");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(log.getByText("这是", { exact: false }).first()).toBeVisible();
  await expect(log.getByText("已收到：比较两次实验的验证精度")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "中断", exact: true }),
  ).not.toBeVisible();

  await input.fill("测试审批");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Codex 请求确认" }),
  ).toBeVisible();
  // Reopening the browser recovers pending server requests from the shared connection.
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Codex 请求确认" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "拒绝", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Codex 请求确认" }),
  ).not.toBeVisible();
  await expect(log.getByText(/已收到：测试审批.*decline/)).toBeVisible();

  await input.fill("测试提问");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByLabel("下一步优化什么？").fill("验证精度");
  await page.getByRole("button", { name: "提交回复" }).click();
  await expect(log.getByText(/已收到：测试提问.*验证精度/)).toBeVisible();

  await input.fill("等待我检查训练参数");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByRole("button", { name: "中断", exact: true }).click();
  await expect(log.getByText("此轮已中断")).toBeVisible();
  await page.getByRole("button", { name: "断开连接", exact: true }).click();
  await expect(input).toBeDisabled();
  await page.getByRole("button", { name: "连接 Codex", exact: true }).click();
  await expect(input).toBeEnabled();
  await expect(log.getByText("此轮已中断")).toBeVisible();

  await page.screenshot({
    path: testInfo.outputPath("codex-workspace-desktop.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "在 视觉实验 中新建任务", exact: true })
    .click();
  const projectCreate = page.getByRole("dialog", { name: "新建 Codex 任务" });
  await expect(projectCreate.getByLabel("所属项目")).toHaveValue("vision");
  await expect(projectCreate.getByLabel("项目目录")).toHaveValue(
    "/tmp/codex-fixture",
  );
  await projectCreate.getByRole("button", { name: "创建任务" }).click();
  await expect(projectCreate).not.toBeVisible();
  const projectTaskId = new URLSearchParams(
    new URL(page.url()).hash.slice(1),
  ).get("thread");
  const projectTask = await (
    await request.get(`/api/servers/local/codex/threads/${projectTaskId}`)
  ).json();
  expect(projectTask.thread.projectId).toBe("vision");
  await page
    .getByRole("button", { name: "新建 Codex 任务", exact: true })
    .click();
  const create = page.getByRole("dialog", { name: "新建 Codex 任务" });
  await expect(create.getByLabel("所属项目")).toHaveValue("");
  await create.getByLabel("项目目录").fill("/tmp/new-project");
  await create
    .getByLabel("任务模型", { exact: true })
    .selectOption("gpt-5.6-luna");
  await create.getByRole("button", { name: "创建任务" }).click();
  await expect(create).not.toBeVisible();
  await expect(page.locator(".codex-chat-heading")).toContainText(
    "/tmp/new-project",
  );
  const id = new URLSearchParams(new URL(page.url()).hash.slice(1)).get(
    "thread",
  );
  const task = await (
    await request.get(`/api/servers/local/codex/threads/${id}`)
  ).json();
  expect(task.thread.cwd).toBe("/tmp/new-project");
  expect(task.thread.model).toBe("gpt-5.6-luna");
  expect(task.thread.projectId).toBeNull();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(input).toBeVisible();
  await expect(
    page.getByRole("complementary", { name: "任务目录" }),
  ).toHaveCount(0);
  expect((await log.boundingBox()).height).toBeGreaterThan(300);
  await page.getByRole("button", { name: "展开任务目录", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "任务目录" }),
  ).toBeVisible();
  await independent
    .getByRole("button", { name: "独立分析任务", exact: false })
    .click();
  await expect(
    page.getByRole("complementary", { name: "任务目录" }),
  ).toHaveCount(0);
  await expect(page.locator(".codex-chat-heading")).toContainText(
    "独立分析任务",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("codex-workspace-mobile.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollHeight <= innerHeight + 1,
    ),
  ).toBeTruthy();
});
