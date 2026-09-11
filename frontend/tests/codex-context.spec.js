import { test, expect } from "@playwright/test";

const headers = { "X-DeepQueue": "1" };
const base = "/api/servers/local/codex";

test("archive inputs fold responsively and context telemetry follows compaction across refresh", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(60000);
  const fixture = await (
    await request.post("/api/fixture/context", { headers })
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
    expect(
      (
        await request.post(`/api/fixture/context?action=${action}`, { headers })
      ).ok(),
    ).toBeTruthy();
  };
  try {
    await page.goto(`/#view=codex&server=local&thread=${fixture.thread_id}`);
    const chat = page.getByRole("log", { name: "Codex 对话内容" });
    const inputs = chat.locator(".codex-message.user");
    const short = inputs.nth(0);
    const archive = inputs.nth(1);
    const responsive = inputs.nth(2);
    const usage = page.locator(".codex-context > summary");
    await expect(inputs).toHaveCount(4);
    await expect(usage).toContainText("等待统计");
    await expect(short.getByRole("button", { name: "展开全文" })).toHaveCount(
      0,
    );
    await expect(
      responsive.getByRole("button", { name: "展开全文" }),
    ).toHaveCount(0);
    await expect(
      archive.getByRole("button", { name: "展开全文" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect((await archive.boundingBox()).height).toBeLessThan(230);
    await archive.getByRole("button", { name: "展开全文" }).click();
    await expect(
      archive.getByRole("button", { name: "收起", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
    expect((await archive.boundingBox()).height).toBeGreaterThan(1000);
    await expect(archive).toContainText("$deepqueue");
    await expect(archive).toContainText("[图片]");
    await expect(archive).toContainText("归档结束标记：原始输入应完整保留。");
    const refreshed = page.waitForResponse(
      (r) =>
        r.url().includes(`/threads/${fixture.thread_id}/open`) &&
        r.status() === 200,
    );
    await page
      .getByRole("button", { name: "刷新当前对话", exact: true })
      .click();
    await refreshed;
    await expect(
      archive.getByRole("button", { name: "收起", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
    await archive
      .getByRole("button", { name: "收起长消息", exact: true })
      .click();
    const expand = archive.getByRole("button", { name: "展开全文" });
    await expect(expand).toBeFocused();
    await expect(expand).toBeInViewport();
    await expand.press("Enter");
    await expect(
      archive.getByRole("button", { name: "收起", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
    await advance("usage");
    await expect(usage).toContainText("96k / 128k · 75%");
    await expect(
      archive.getByRole("button", { name: "收起", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
    const source = (
      await (await request.get(`${base}/threads/${fixture.thread_id}`)).json()
    ).thread;
    expect(source.turns[1].items[0].content[0].text).toBe(fixture.archive);
    expect(source.turns).toHaveLength(4);
    await archive.getByRole("button", { name: "收起", exact: true }).click();
    await usage.click();
    const details = page.getByRole("group", { name: "上下文统计详情" });
    await expect(details).toContainText("96,000 tokens");
    await expect(details).toContainText("540,000");
    await expect(details.getByRole("meter")).toHaveAttribute(
      "aria-valuenow",
      "96000",
    );
    await usage.press("Escape");
    await expect(usage).toBeFocused();
    await expect(details).not.toBeVisible();

    await advance("start");
    await expect(page.locator(".codex-context-state")).toHaveText(
      "正在压缩上下文",
    );
    const record = chat.locator(".codex-compaction").last();
    await expect(record).toHaveText("正在压缩上下文");
    await page.reload();
    await expect(record).toHaveText("正在压缩上下文");
    await expect(usage).toContainText("75%");
    await advance("finish");
    await expect(record).toHaveText("上下文已压缩");
    await expect(usage).toContainText("12k / 128k · 9%");
    await page.reload();
    await expect(record).toHaveText("上下文已压缩");
    await expect(usage).toContainText("12k / 128k · 9%");
    await archive.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath("codex-context-desktop.png"),
      fullPage: true,
    });

    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await expect(
        responsive.getByRole("button", { name: "展开全文" }),
      ).toBeVisible();
      await responsive.getByRole("button", { name: "展开全文" }).click();
      await responsive
        .getByRole("button", { name: "收起长消息", exact: true })
        .click();
      await expect(
        responsive.getByRole("button", { name: "展开全文" }),
      ).toBeFocused();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBeTruthy();
      expect(
        await chat.evaluate((el) => el.scrollWidth <= el.clientWidth),
      ).toBeTruthy();
      await usage.click();
      await expect(details).toBeInViewport();
      await usage.press("Escape");
    }
    await archive.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath("codex-context-mobile.png"),
      fullPage: true,
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await expect(
      responsive.getByRole("button", { name: "展开全文" }),
    ).toHaveCount(0);
    await advance("unknown-window");
    await expect(usage).toContainText("96k tokens");
    await expect(usage).not.toContainText("%");
    await advance("start");
    await advance("fail");
    await expect(record).toHaveText("上下文压缩失败");
    await expect(page.locator(".codex-context-state")).toHaveText(
      "上下文压缩失败",
    );
    await advance("start");
    await advance("interrupt");
    await expect(record).toHaveText("上下文压缩已中断");
    expect(errors).toEqual([]);
  } finally {
    await request.post(`${base}/settings`, {
      headers,
      data: { ...config, enabled: false },
    });
  }
});
