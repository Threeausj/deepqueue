import { test, expect } from "@playwright/test";

test("public setup, scoped credentials, exported skill, and administrator login", async ({
  page,
  context,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
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
  await page.goto("/#view=settings");
  await page.getByLabel("公网访问地址").fill("https://queue.example.test");
  await page.getByRole("button", { name: "保存接入设置" }).click();
  await expect(page.getByText("访问认证已启用", { exact: true })).toBeVisible();
  const admin = await page
    .getByLabel("新访问令牌", { exact: true })
    .inputValue();
  expect(admin.startsWith("dq_")).toBeTruthy();
  await expect(
    page.getByRole("button", { name: "退出登录", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "服务器资源", exact: true }).click();
  await page
    .getByRole("button", { name: "local 接入配置", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "local 接入配置",
    exact: true,
  });
  await expect(
    dialog.getByText("https://queue.example.test", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "创建提交令牌", exact: true })
    .click();
  const tokenField = dialog.getByLabel("新访问令牌", { exact: true });
  await expect(tokenField).toBeVisible();
  const token = await tokenField.inputValue();
  const scopedHeaders = {
    Authorization: `Bearer ${token}`,
    "X-DeepQueue": "1",
  };
  expect(
    (await page.request.get("/api/state", { headers: scopedHeaders })).status(),
  ).toBe(403);
  expect(
    (
      await page.request.post("/api/jobs", {
        headers: scopedHeaders,
        data: {
          server: "another-server",
          command: "true",
          cwd: "/tmp",
        },
      })
    ).status(),
  ).toBe(403);
  const downloadEvent = page.waitForEvent("download");
  await dialog.getByRole("link", { name: "下载服务器 Skill" }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("deepqueue-local-skill.zip");
  await download.saveAs(testInfo.outputPath(download.suggestedFilename()));
  await dialog
    .getByLabel("来源任务回传 Socket")
    .fill("/tmp/fixture-forwarded-codex.sock");
  await dialog.getByRole("button", { name: "保存回传连接" }).click();
  await expect
    .poll(
      async () =>
        (await (await page.request.get("/api/servers")).json()).find(
          (server) => server.name === "local",
        ).config.return_agent_socket,
    )
    .toBe("/tmp/fixture-forwarded-codex.sock");
  await page.screenshot({
    path: testInfo.outputPath("public-server-access-desktop.png"),
  });
  await page.setViewportSize({ width: 320, height: 740 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("public-server-access-mobile.png"),
  });
  await dialog.getByRole("button", { name: /^撤销令牌 / }).click();
  await expect(tokenField).not.toBeVisible();
  expect(
    (await page.request.get("/api/jobs", { headers: scopedHeaders })).status(),
  ).toBe(401);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "展开导航", exact: true }).click();
  await page.getByRole("link", { name: "通用设置", exact: true }).click();
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page.getByLabel("管理员访问令牌")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("public-login-mobile.png"),
  });
  await page.getByLabel("管理员访问令牌").fill("invalid-token");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("管理员访问令牌无效");
  await page.getByLabel("管理员访问令牌").fill(admin);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "退出登录", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "退出登录", exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});
