import { test, expect } from "@playwright/test";

const headers = { "X-DeepQueue": "1" };

test("server names, persistent order and collapsible resources", async ({
  page,
  request,
}, testInfo) => {
  const original = await (await request.get("/api/servers")).json();
  const local = original.find((server) => server.name === "local");
  const cards = page.locator(".server-band[data-server-id]");
  const card = page.locator('.server-band[data-server-id="local"]');
  try {
    await page.goto("/#view=resources");
    await card
      .getByRole("button", { name: "收起 local 资源详情", exact: true })
      .click();
    await expect(card.locator(".server-resource-details")).toBeHidden();
    await page.reload();
    await expect(
      card.getByRole("button", { name: "展开 local 资源详情" }),
    ).toHaveAttribute("aria-expanded", "false");
    await card.getByRole("button", { name: "展开 local 资源详情" }).click();
    await expect(card.locator(".server-resource-details")).toBeVisible();
    await card.getByRole("button", { name: "编辑 local", exact: true }).click();
    const dialog = page.getByRole("dialog", {
      name: "编辑服务器",
      exact: true,
    });
    await expect(dialog.getByLabel("服务器标识")).toHaveValue("local");
    await expect(dialog.getByLabel("服务器标识")).toHaveAttribute(
      "readonly",
      "",
    );
    await dialog.getByLabel("服务器名称").fill("主训练服务器");
    await dialog.getByLabel("最大并发").fill("3");
    await dialog.getByRole("button", { name: "保存修改" }).click();
    await expect(dialog).not.toBeVisible();
    await expect(card.locator(".server-card-title")).toContainText(
      "主训练服务器",
    );
    await expect(
      page.getByRole("button", { name: "服务器 主训练服务器", exact: true }),
    ).toBeVisible();
    await card
      .getByRole("button", { name: "下移 主训练服务器", exact: true })
      .click();
    await expect(cards.first()).toHaveAttribute("data-server-id", "remote-ui");
    await page.reload();
    await expect(cards.last()).toHaveAttribute("data-server-id", "local");
    const serverNav = page
      .locator(".sidebar")
      .getByRole("button", { name: /^服务器 / });
    await expect(serverNav.first()).toHaveAccessibleName("服务器 remote-ui");
    // Pointer dragging is the alternative to the keyboard-accessible arrows.
    await card
      .getByRole("button", { name: "拖动 主训练服务器 排序" })
      .dragTo(cards.first());
    await expect(cards.first()).toHaveAttribute("data-server-id", "local");
    const state = await (await request.get("/api/state")).json();
    expect(state.jobs.some((job) => job.server === "local")).toBeTruthy();
    expect(state.jobs.some((job) => job.server === "主训练服务器")).toBeFalsy();
    await page.screenshot({
      path: testInfo.outputPath("resources-desktop.png"),
    });
    await page.setViewportSize({ width: 320, height: 740 });
    await expect(
      card.getByRole("button", { name: "编辑 主训练服务器", exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await card
      .getByRole("button", { name: "收起 主训练服务器 资源详情" })
      .click();
    await page.screenshot({
      path: testInfo.outputPath("resources-mobile.png"),
    });
  } finally {
    await request.post("/api/servers/local/settings", {
      headers,
      data: {
        display_name: local.config.display_name ?? null,
        max_running: local.config.max_running,
      },
    });
    await request.post("/api/servers/order", {
      headers,
      data: { names: original.map((server) => server.name) },
    });
  }
});

test("submission round controls align and search has one contained focus ring", async ({
  page,
}, testInfo) => {
  await page.route("**/api/models", (route) =>
    route.fulfill({ json: { models: {}, errors: {} } }),
  );
  await page.goto("/#view=queue&server=local");
  const search = page.getByRole("textbox", { name: "搜索实验", exact: true });
  await search.click();
  const focus = await search.evaluate((input) => ({
    inner: getComputedStyle(input).outlineStyle,
    outer: getComputedStyle(input.parentElement).outlineStyle,
    input: input.getBoundingClientRect().toJSON(),
    parent: input.parentElement.getBoundingClientRect().toJSON(),
  }));
  expect(focus.inner).toBe("none");
  expect(focus.outer).toBe("solid");
  expect(focus.input.right).toBeLessThanOrEqual(focus.parent.right);
  await page.screenshot({ path: testInfo.outputPath("search-focus.png") });
  await page
    .getByRole("button", { name: "提交实验", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "提交实验", exact: true });
  await dialog.getByRole("button", { name: "继续改进", exact: true }).click();
  const choice = await dialog
    .getByRole("group", { name: "运行结束后" })
    .boundingBox();
  const rounds = await dialog.getByLabel("最多改进轮数").boundingBox();
  expect(Math.abs(choice.y - rounds.y)).toBeLessThanOrEqual(1);
  expect(Math.abs(choice.height - rounds.height)).toBeLessThanOrEqual(1);
  await expect(dialog.getByLabel("最多改进轮数")).toHaveValue("1");
  await page.screenshot({
    path: testInfo.outputPath("submission-desktop.png"),
  });
  await page.setViewportSize({ width: 320, height: 740 });
  await expect(dialog.getByLabel("最多改进轮数")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath("submission-mobile.png") });
});

test("live logs open at the latest output, follow updates and respect history reading", async ({
  page,
  request,
}, testInfo) => {
  const state = await (await request.get("/api/state")).json();
  const job = state.jobs.find(
    (item) => item.title === "CPU baseline / lr 0.001 / seed 7",
  );
  const detail = await (await request.get(`/api/jobs/${job.id}`)).json();
  let count = 120,
    requests = 0;
  await page.route(`**/api/jobs/${job.id}`, (route) =>
    route.fulfill({ json: { ...detail, status: "running" } }),
  );
  await page.route(`**/api/jobs/${job.id}/logs*`, (route) => {
    requests++;
    return route.fulfill({
      json: {
        truncated: false,
        text: Array.from(
          { length: count },
          (_, index) =>
            `Epoch ${index + 1} | val_accuracy=${(70 + (index + 1) / 10).toFixed(2)}% | loss=0.123`,
        ).join("\n"),
      },
    });
  });
  await page.goto("/#view=queue&server=local");
  await page
    .getByRole("button", { name: `查看 ${job.title} 实时日志`, exact: true })
    .click();
  const panel = page.getByRole("dialog", { name: "实验详情", exact: true });
  const log = panel.getByRole("log", { name: "实时训练日志" });
  const gap = () =>
    log.evaluate(
      (node) => node.scrollHeight - node.scrollTop - node.clientHeight,
    );
  await expect(log).toContainText("Epoch 120");
  await expect.poll(gap).toBeLessThan(2);
  await expect(
    panel.getByRole("button", { name: "复制 tmux 连接命令" }),
  ).toBeEnabled();
  await panel.getByText("查看终端连接命令", { exact: true }).click();
  await expect.poll(gap).toBeLessThan(2);
  await panel.getByText("查看终端连接命令", { exact: true }).click();
  await expect.poll(gap).toBeLessThan(2);
  count = 140;
  await expect(log).toContainText("Epoch 140");
  await expect.poll(gap).toBeLessThan(2);
  await log.evaluate((node) => {
    node.scrollTop = 180;
  });
  await expect(
    panel.getByRole("button", { name: "回到最新日志" }),
  ).toHaveAttribute("aria-pressed", "false");
  const previousRequests = requests;
  count = 160;
  await expect.poll(() => requests).toBeGreaterThan(previousRequests);
  await expect(log).toContainText("Epoch 160");
  expect(await log.evaluate((node) => node.scrollTop)).toBe(180);
  await panel.getByRole("button", { name: "回到最新日志" }).click();
  await expect.poll(gap).toBeLessThan(2);
  await log.evaluate((node) => {
    node.scrollTop = 0;
  });
  await panel
    .getByRole("button", { name: "实时日志 · tmux", exact: true })
    .click();
  await expect.poll(gap).toBeLessThan(2);
  await page.screenshot({ path: testInfo.outputPath("live-logs-desktop.png") });
  await page.reload();
  await expect(log).toContainText("Epoch 160");
  await expect.poll(gap).toBeLessThan(2);
  await page.setViewportSize({ width: 320, height: 740 });
  await panel
    .getByRole("button", { name: "实时日志 · tmux", exact: true })
    .click();
  await expect.poll(gap).toBeLessThan(2);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath("live-logs-mobile.png") });
});
