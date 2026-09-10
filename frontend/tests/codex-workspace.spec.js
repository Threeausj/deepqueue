import { test, expect } from "@playwright/test";
const headers = { "X-DeepQueue": "1" };
const base = "/api/servers/local/codex";

test("workspace caches conversations and integrates slash, terminal, files and live previews", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(90000);
  const fixture = await (
    await request.post("/api/fixture/workspace", { headers })
  ).json();
  const config = (await (await request.get(base)).json()).config;
  await request.post(`${base}/settings`, {
    headers,
    data: { ...config, enabled: true },
  });
  await request.post(`${base}/connect`, { headers, data: {} });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const route = (id) => `/#view=codex&server=local&thread=${id}`;
  await page.goto(route(fixture.thread_id));
  const chat = page.getByRole("log", { name: "Codex 对话内容" });
  const input = page.getByRole("textbox", { name: "发给 Codex 的消息" });
  await expect(chat).toContainText("验证精度提高到 0.93");
  await input.fill("保留的消息草稿");
  await page.getByRole("button", { name: /缓存切换验证/ }).click();
  await expect(chat).toContainText("第二个任务的独立记录");
  let release;
  const blocked = new Promise((resolve) => (release = resolve));
  await page.route(`**/threads/${fixture.thread_id}/open?*`, async (route) => {
    await blocked;
    await route.continue();
  });
  await page.getByRole("button", { name: /工作区工具验证/ }).click();
  await expect(chat).toContainText("验证精度提高到 0.93", { timeout: 1500 });
  await expect(input).toHaveValue("保留的消息草稿");
  release();
  await page.unrouteAll({ behavior: "wait" });

  await input.fill("/experiment");
  const choices = page.getByRole("listbox", { name: "插件与 Skills" });
  await expect(choices.getByRole("option")).toHaveCount(1);
  await input.press("Enter");
  await expect(input).toHaveValue("$experiment-review ");
  await input.press("End");
  await input.pressSequentially("/lab");
  await expect(choices.getByRole("option")).toHaveCount(1);
  await input.press("Enter");
  await expect(page.getByLabel("已选择的插件与 skills")).toContainText(
    "实验工具",
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(chat).toContainText("已收到：$experiment-review $lab");
  const calls = await (await request.get("/api/fixture/codex-calls")).json();
  const sent = calls.findLast(
    (c) => c.method === "turn/start" && c.params.threadId === fixture.thread_id,
  );
  expect(sent.params.input.slice(1).map((i) => i.name)).toEqual([
    "experiment-review",
    "plot-results",
  ]);

  await page.getByRole("button", { name: "打开项目文件", exact: true }).click();
  const panel = page.getByRole("complementary", { name: "工作区工具面板" });
  await panel.getByRole("button", { name: "README.md", exact: true }).click();
  await expect(
    panel.getByRole("heading", { name: "工作区预览验证" }),
  ).toBeVisible();
  await expect(panel.getByRole("table")).toContainText("0.93");
  await panel.getByRole("tab", { name: "文件", exact: true }).click();
  await panel.getByRole("button", { name: "notes", exact: true }).click();
  await panel.getByRole("button", { name: "train.py", exact: true }).click();
  await expect(panel.locator(".workspace-code")).toContainText(
    "accuracy = 0.93",
  );
  await panel.getByRole("tab", { name: "文件", exact: true }).click();
  await panel.getByRole("button", { name: "上一级目录" }).click();
  await panel.getByRole("button", { name: "page.html", exact: true }).click();
  await expect(
    page
      .frameLocator('iframe[title="预览 page.html"]')
      .getByRole("heading", { name: "项目 HTML 文件" }),
  ).toBeVisible();
  await panel.getByRole("tab", { name: "文件", exact: true }).click();
  await panel.getByRole("button", { name: "chart.svg", exact: true }).click();
  await expect(panel.getByRole("img", { name: "chart.svg" })).toBeVisible();

  await panel.getByRole("tab", { name: "终端", exact: true }).click();
  await expect(panel.getByText("交互终端", { exact: true })).toBeVisible();
  const terminal = `${base}/threads/${fixture.thread_id}/terminal`;
  const initial = await (await request.get(terminal)).json();
  await panel
    .locator(".xterm-helper-textarea")
    .pressSequentially("printf test-terminal");
  await expect
    .poll(async () => {
      const state = await (await request.get(terminal)).json();
      return Buffer.from(state.output_base64, "base64").toString();
    })
    .toContain("printf test-terminal");
  const separator = panel.getByRole("separator", { name: "调整工具面板宽度" });
  const before = await panel.boundingBox();
  const handle = await separator.boundingBox();
  await page.mouse.move(handle.x + 3, handle.y + 80);
  await page.mouse.down();
  await page.mouse.move(handle.x - 70, handle.y + 80);
  await page.mouse.up();
  await expect
    .poll(async () => (await panel.boundingBox()).width)
    .toBeGreaterThan(before.width + 40);
  await panel.getByRole("button", { name: "收起右侧面板" }).click();
  await expect(panel).not.toBeVisible();
  await page.getByRole("button", { name: "打开终端", exact: true }).click();
  expect((await (await request.get(terminal)).json()).id).toBe(initial.id);
  await panel.getByRole("button", { name: "关闭当前终端" }).click();
  await expect(panel.getByText("终端已退出", { exact: true })).toBeVisible();

  await panel.getByRole("tab", { name: "预览", exact: true }).click();
  await panel.getByRole("button", { name: "网页服务", exact: true }).click();
  await panel
    .getByRole("spinbutton", { name: "网页服务端口" })
    .fill(String(fixture.port));
  await panel.getByRole("button", { name: "打开", exact: true }).click();
  const preview = page.frameLocator('iframe[title="网页服务预览"]');
  await expect(
    preview.getByRole("heading", { name: "实时项目服务" }),
  ).toBeVisible();
  await expect(preview.locator("#metrics")).toHaveText("Accuracy 0.93");
  await expect(preview.locator("#socket")).toHaveText(
    "WebSocket: preview-ready",
  );
  await expect(preview.locator("#isolated")).toHaveText("独立预览");
  await expect(preview.getByRole("heading")).toHaveCSS(
    "color",
    "rgb(70, 90, 200)",
  );
  await page.screenshot({
    path: testInfo.outputPath("workspace-desktop.png"),
    fullPage: true,
  });
  const previewUrl = await panel
    .locator('iframe[title="网页服务预览"]')
    .getAttribute("src");
  await panel.getByRole("button", { name: "收起右侧面板" }).click();
  await page.getByRole("button", { name: "打开预览", exact: true }).click();
  await expect(panel.locator('iframe[title="网页服务预览"]')).toHaveAttribute(
    "src",
    previewUrl,
  );
  await panel.getByRole("button", { name: "关闭网页预览" }).click();
  const expired = await request.get(`http://127.0.0.1:18765/`, {
    headers: { Host: new URL(previewUrl).host },
  });
  expect(expired.status()).toBe(404);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(panel).toBeVisible();
  expect((await panel.boundingBox()).width).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: testInfo.outputPath("workspace-mobile.png"),
    fullPage: true,
  });
  await panel.getByRole("button", { name: "收起右侧面板" }).click();
  await expect(input).toBeVisible();
  expect(errors).toEqual([]);

  await page.setViewportSize({ width: 1440, height: 1000 });
  const tree = page.getByRole("complementary", { name: "任务目录" });
  await page.getByRole("button", { name: "收起任务目录", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(() => sessionStorage.getItem("deepqueue.codex.history.v1")),
    )
    .not.toBeNull();
  await page.reload();
  await expect(tree).not.toBeVisible();
  await expect(chat).toContainText("验证精度提高到 0.93");
  await page.evaluate(async () => {
    await fetch("/api/auth/logout", {
      method: "POST",
      headers: { "X-DeepQueue": "1" },
    });
    window.dispatchEvent(new Event("deepqueue:auth-reset"));
  });
  expect(
    await page.evaluate(() =>
      sessionStorage.getItem("deepqueue.codex.history.v1"),
    ),
  ).toBeNull();
  await request.post(`${base}/settings`, {
    headers,
    data: { ...config, enabled: false },
  });
});
