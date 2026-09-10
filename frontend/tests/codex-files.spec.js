import { test, expect } from "@playwright/test";
import { conversationFile } from "../src/codexFiles.js";

const cwd = "/project/模型";
test("conversation file targets preserve line references and project boundaries", () => {
  for (const [value, path, line] of [
    ["notes/train.py#L2", "notes/train.py", 2],
    ["README.md:12", "README.md", 12],
    ["/project/模型/README.md:12:3", "README.md", 12],
    [
      "file:///project/%E6%A8%A1%E5%9E%8B/report%20one.html",
      "report one.html",
      null,
    ],
    ["./notes/../README.md", "README.md", null],
    ["notes/train.py#L2-L8", "notes/train.py", 2],
  ])
    expect(conversationFile(value, cwd)).toEqual({ path, line });
  for (const value of [
    "https://example.com/a.md",
    "//example.com/a.md",
    "javascript:alert(1)",
    "#L10",
    "file://other-server/path",
    "%00.txt",
  ])
    expect(conversationFile(value, cwd)).toBeNull();
  for (const value of ["/etc/passwd", "../../secret.md", "/project/模型2/a.md"])
    expect(conversationFile(value, cwd).error).toContain("项目目录之外");
});

const base = "/api/servers/local/codex";
const headers = { "X-DeepQueue": "1" };
test("conversation files open in preview and failed terminals recover only after explicit choice", async ({
  page,
  request,
}) => {
  test.setTimeout(60000);
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
  try {
    await page.goto(`/#view=codex&server=local&thread=${fixture.thread_id}`);
    const chat = page.getByRole("log", { name: "Codex 对话内容" });
    const panel = page.getByRole("complementary", { name: "工作区工具面板" });
    const url = page.url();
    await chat.getByRole("button", { name: "项目说明", exact: true }).click();
    await expect(
      panel.getByRole("heading", { name: "工作区预览验证" }),
    ).toBeVisible();
    await expect(panel.locator(".workspace-file-name")).toContainText(
      "第 2 行",
    );
    expect(page.url()).toBe(url);
    await chat.getByRole("button", { name: "训练代码", exact: true }).click();
    await expect(panel.locator(".workspace-code")).toContainText(
      "accuracy = 0.93",
    );
    await chat.getByRole("button", { name: "HTML 报告", exact: true }).click();
    await expect(
      page
        .frameLocator('iframe[title="预览 page.html"]')
        .getByRole("heading", { name: "项目 HTML 文件" }),
    ).toBeVisible();
    await chat.getByRole("button", { name: "精度图表", exact: true }).click();
    await expect(panel.getByRole("img", { name: "chart.svg" })).toBeVisible();
    await chat
      .locator(".codex-tool summary")
      .filter({ hasText: "文件变更" })
      .click();
    await chat.getByRole("button", { name: /notes\/train.py.*预览/ }).click();
    await expect(panel.locator(".workspace-code")).toContainText(
      "accuracy = 0.93",
    );
    await chat
      .getByRole("button", { name: "不存在的文件", exact: true })
      .click();
    await expect(panel.getByRole("alert")).toContainText("不存在");
    await chat.getByRole("button", { name: "外部文件", exact: true }).click();
    await expect(panel.getByRole("alert")).toContainText("项目目录之外");
    await expect(
      chat.getByRole("link", { name: "外部网页", exact: true }),
    ).toHaveAttribute("href", "https://example.com/docs");
    await panel.getByRole("button", { name: "收起右侧面板" }).click();
    await chat.getByRole("button", { name: "项目说明", exact: true }).click();
    await expect(
      panel.getByRole("heading", { name: "工作区预览验证" }),
    ).toBeVisible();

    const threadEndpoint = `${base}/threads/${fixture.thread_id}`;
    const before = (await (await request.get(threadEndpoint)).json()).thread;
    await request.post("/api/fixture/namespace-failure", { headers });
    await page.getByRole("button", { name: "打开终端", exact: true }).click();
    const recover = panel.getByRole("button", {
      name: "仅本次以服务器账户重新打开",
      exact: true,
    });
    await expect(recover).toBeVisible();
    const failed = await (
      await request.get(`${threadEndpoint}/terminal`)
    ).json();
    expect(failed.state).toBe("exited");
    expect(failed.execution_mode).toBe("session");
    expect(
      (
        await request.post(`${threadEndpoint}/terminal/recover`, {
          headers,
          data: { id: failed.id },
        })
      ).status(),
    ).toBe(422);
    await expect(panel.getByText(/完整文件与网络权限/)).toBeVisible();
    await recover.click();
    await expect(
      panel.getByText("服务器账户终端", { exact: true }),
    ).toBeVisible();
    await expect(recover).not.toBeVisible();
    await panel
      .locator(".xterm-helper-textarea")
      .pressSequentially("recovered-terminal");
    await expect
      .poll(async () =>
        Buffer.from(
          (await (await request.get(`${threadEndpoint}/terminal`)).json())
            .output_base64,
          "base64",
        ).toString(),
      )
      .toContain("recovered-terminal");
    const after = (
      await (await request.get(`${threadEndpoint}?force=true`)).json()
    ).thread;
    expect(after.sandbox).toEqual(before.sandbox);
    expect(after.approvalPolicy).toEqual(before.approvalPolicy);
    await panel
      .getByRole("button", { name: "关闭当前终端", exact: true })
      .click();
    await expect(panel.getByText("终端已退出", { exact: true })).toBeVisible();
    await panel
      .getByRole("button", { name: "重新打开终端", exact: true })
      .click();
    await expect(recover).toBeVisible();
    expect(
      (await (await request.get(`${threadEndpoint}/terminal`)).json())
        .execution_mode,
    ).toBe("session");
    expect(errors).toEqual([]);
  } finally {
    await request.post("/api/fixture/namespace-failure?enabled=false", {
      headers,
    });
    await request.post(`${base}/settings`, {
      headers,
      data: { ...config, enabled: false },
    });
  }
});
