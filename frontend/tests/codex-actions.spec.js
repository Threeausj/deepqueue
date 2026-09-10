import { test, expect } from "@playwright/test";

const base = "/api/servers/local/codex";
const headers = { "X-DeepQueue": "1" };

test.afterAll(async ({ request }) => {
  const { config } = await (await request.get(base)).json();
  await request.post(`${base}/settings`, {
    headers,
    data: { ...config, enabled: false },
  });
});

test("task access, native message forks, rename and mobile actions", async ({
  page,
  request,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const { config } = await (await request.get(base)).json();
  await request.post(`${base}/settings`, {
    headers,
    data: { ...config, enabled: true },
  });
  await request.post(`${base}/connect`, { headers, data: {} });
  await page.goto("/#view=codex&server=local");
  await page
    .getByRole("button", { name: "在 视觉实验 中新建任务", exact: true })
    .click();
  const create = page.getByRole("dialog", { name: "新建 Codex 任务" });
  await create
    .getByLabel("任务模型", { exact: true })
    .selectOption("gpt-5.6-luna");
  await create.getByText("访问权限", { exact: true }).click();
  await create.getByLabel("文件与网络访问").selectOption(":read-only");
  await create.getByLabel("审批策略", { exact: true }).selectOption("never");
  await create.getByRole("button", { name: "创建任务" }).click();
  await expect(create).not.toBeVisible();
  const original = new URLSearchParams(new URL(page.url()).hash.slice(1)).get(
    "thread",
  );
  const created = await (
    await request.get(`${base}/threads/${original}`)
  ).json();
  expect(created.thread.activePermissionProfile.id).toBe(":read-only");
  expect(created.thread.approvalPolicy).toBe("never");
  const log = page.getByRole("log", { name: "Codex 对话内容" });
  const input = page.getByRole("textbox", { name: "发给 Codex 的消息" });
  await page.getByRole("button", { name: "访问权限", exact: true }).click();
  const access = page.getByRole("dialog", { name: "访问权限", exact: true });
  await access.getByLabel("文件与网络访问").selectOption(":danger-full-access");
  await expect(
    access.locator('option[value="restricted-by-admin"]'),
  ).toHaveJSProperty("disabled", true);
  await access.getByLabel("审批策略", { exact: true }).selectOption("never");
  await access.getByRole("button", { name: "保存权限" }).click();
  await expect(access).not.toBeVisible();
  await expect(page.locator(".codex-access-trigger")).toContainText("完全访问");
  await page.reload();
  await expect(page.locator(".codex-access-trigger")).toContainText("完全访问");
  await page.getByRole("button", { name: "访问权限", exact: true }).click();
  await access.getByText("查看当前生效权限").click();
  await expect(access.locator("pre")).toContainText(
    '"approvalPolicy": "never"',
  );
  await access.getByLabel("文件与网络访问").selectOption(":workspace");
  // A rejected update stays visible in the dialog and never changes the current badge.
  await page.route(`**${base}/threads/${original}/access`, (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "管理员限制了此权限配置" }),
    }),
  );
  await access.getByRole("button", { name: "保存权限" }).click();
  await expect(access.getByRole("alert")).toContainText("管理员限制");
  await expect(page.locator(".codex-access-trigger")).toContainText("完全访问");
  await page.unroute(`**${base}/threads/${original}/access`);
  await access.getByRole("button", { name: "保存权限" }).click();
  await expect(access).not.toBeVisible();
  await expect(page.locator(".codex-access-trigger")).toContainText(
    "工作区读写",
  );
  for (const text of ["基线指标 0.81", "后续指标 0.83"]) {
    await input.fill(text);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(
      log.getByText(`已收到：${text}`, { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "中断", exact: true }),
    ).not.toBeVisible();
  }
  await log
    .getByRole("button", { name: "从此处分支", exact: true })
    .first()
    .click();
  await expect(page.getByRole("button", { name: "返回原聊天" })).toBeVisible();
  const branch = new URLSearchParams(new URL(page.url()).hash.slice(1)).get(
    "thread",
  );
  expect(branch).not.toBe(original);
  await expect(
    log.getByText("已收到：基线指标 0.81", { exact: true }),
  ).toBeVisible();
  await expect(
    log.getByText("已收到：后续指标 0.83", { exact: true }),
  ).toHaveCount(0);
  const branchData = await (
    await request.get(`${base}/threads/${branch}`)
  ).json();
  expect(branchData.thread.projectId).toBe("vision");
  expect(branchData.thread.forkedFromId).toBe(original);
  expect(branchData.thread.activePermissionProfile.id).toBe(":workspace");
  await input.fill("仅在分支中探索优化");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    log.getByText("已收到：仅在分支中探索优化", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "中断", exact: true }),
  ).not.toBeVisible();
  await page.getByRole("button", { name: "更多聊天操作" }).click();
  await page.getByRole("button", { name: "重命名聊天", exact: true }).click();
  const rename = page.getByRole("dialog", { name: "重命名聊天" });
  await rename.getByLabel("聊天名称").fill("独立的优化方向");
  await rename.getByRole("button", { name: "保存名称" }).click();
  await expect(rename).not.toBeVisible();
  await expect(page.locator(".codex-chat-heading h2")).toHaveText(
    "独立的优化方向",
  );
  await page.getByRole("button", { name: "更多聊天操作" }).click();
  await page.screenshot({
    path: testInfo.outputPath("codex-actions-desktop.png"),
  });
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "分支到新聊天", exact: true }),
  ).not.toBeVisible();
  await page.getByRole("button", { name: "返回原聊天" }).click();
  await expect(
    log.getByText("已收到：后续指标 0.83", { exact: true }),
  ).toBeVisible();
  await expect(
    log.getByText("已收到：仅在分支中探索优化", { exact: true }),
  ).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "更多聊天操作" }).click();
  await page.getByRole("button", { name: "分支到新聊天", exact: true }).click();
  await expect(page.getByRole("button", { name: "返回原聊天" })).toBeVisible();
  await expect(
    log.getByText("已收到：后续指标 0.83", { exact: true }),
  ).toBeVisible();
  expect((await log.boundingBox()).height).toBeGreaterThan(300);
  await page.getByRole("button", { name: "访问权限", exact: true }).click();
  await expect(access.getByLabel("文件与网络访问")).toBeEnabled();
  await page.screenshot({
    path: testInfo.outputPath("codex-access-mobile.png"),
  });
  await access.getByRole("button", { name: "取消", exact: true }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});
