import { test, expect } from "@playwright/test";

const headers = { "X-DeepQueue": "1" };
const base = "/api/servers/local/codex";

test("long work sections collapse together while answers, previews and streamed status stay usable", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(60000);
  const fixture = await (
    await request.post("/api/fixture/activity", { headers })
  ).json();
  const config = (await (await request.get(base)).json()).config;
  await request.post(`${base}/settings`, {
    headers,
    data: { ...config, enabled: true },
  });
  await request.post(`${base}/connect`, { headers, data: {} });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const advance = async (action) => {
    const response = await request.post(
      `/api/fixture/activity?action=${action}`,
      { headers },
    );
    expect(response.ok()).toBeTruthy();
  };
  try {
    await page.goto(`/#view=codex&server=local&thread=${fixture.thread_id}`);
    const chat = page.getByRole("log", { name: "Codex 对话内容" });
    const sections = chat.locator(".codex-activity");
    const history = sections.first();
    const heading = history.locator(":scope > summary");
    await expect(sections).toHaveCount(2);
    await expect(history).not.toHaveAttribute("open");
    await expect(history.locator(".codex-tool")).toHaveCount(0);
    await expect(heading).toContainText("6 段思考");
    await expect(heading).toContainText("6 条命令");
    await expect(heading).toContainText("1 项失败");
    expect((await history.boundingBox()).height).toBeLessThan(110);
    await expect(
      chat.getByText("最终结论：验证精度由 0.91 提高到 0.93。"),
    ).toBeVisible();
    await expect(chat.getByText("旧版模型回复保持可见。")).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("codex-activity-desktop.png"),
      fullPage: true,
    });

    await heading.click();
    await expect(
      history.getByText("先检查提交的代码和运行参数。"),
    ).toBeVisible();
    await history
      .locator(".codex-tool summary")
      .filter({ hasText: "思考过程" })
      .first()
      .click();
    await expect(
      history.getByText("协议测试思考摘要 0", { exact: true }),
    ).toBeVisible();
    await history
      .locator(".codex-tool summary")
      .filter({ hasText: "文件变更" })
      .click();
    await history
      .getByRole("button", { name: /notes\/train.py.*预览/ })
      .click();
    const panel = page.getByRole("complementary", { name: "工作区工具面板" });
    await expect(panel.locator(".workspace-code")).toContainText(
      "accuracy = 0.93",
    );
    await panel.getByRole("button", { name: "收起右侧面板" }).click();
    await page
      .getByRole("button", { name: "刷新当前对话", exact: true })
      .click();
    await expect(history).toHaveAttribute("open", "");
    await heading.focus();
    await heading.press("Enter");
    await expect(history).not.toHaveAttribute("open");
    await expect(heading).toContainText("1 项失败");

    await advance("start");
    await expect(sections).toHaveCount(3);
    const live = sections.last();
    const liveHeading = live.locator(":scope > summary");
    await expect(liveHeading).toContainText("正在执行");
    await expect(live).not.toHaveAttribute("open");
    await liveHeading.click();
    await live
      .locator(".codex-tool summary")
      .filter({ hasText: "思考过程" })
      .click();
    await live
      .locator(".codex-tool summary")
      .filter({ hasText: "运行命令" })
      .click();
    await advance("delta");
    await expect(live).toHaveAttribute("open", "");
    await expect(liveHeading).toContainText("2 条命令");
    await expect(
      live.getByText("实时摘要更新（协议测试）", { exact: true }),
    ).toBeVisible();
    await expect(live.locator("pre").first()).toContainText(
      "streamed accuracy = 0.94",
    );
    await page
      .getByRole("button", { name: "刷新当前对话", exact: true })
      .click();
    await expect(live).toHaveAttribute("open", "");
    await liveHeading.click();
    await advance("finish");
    await expect(chat.getByText("本轮最终回复始终可见。")).toBeVisible();
    await expect(live).not.toHaveAttribute("open");
    await expect(liveHeading).toContainText("已结束");
    await liveHeading.click();
    await live
      .getByRole("button", { name: "收起这段过程", exact: true })
      .click();
    await expect(live).not.toHaveAttribute("open");
    await expect(liveHeading).toBeFocused();

    await page.setViewportSize({ width: 390, height: 844 });
    await heading.scrollIntoViewIfNeeded();
    await expect(heading).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("codex-activity-mobile.png"),
      fullPage: true,
    });
    await heading.click();
    await expect(history).toHaveAttribute("open", "");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await history
      .getByRole("button", { name: "收起这段过程", exact: true })
      .click();
    await expect(history).not.toHaveAttribute("open");
    expect(errors).toEqual([]);
  } finally {
    await request.post(`${base}/settings`, {
      headers,
      data: { ...config, enabled: false },
    });
  }
});
