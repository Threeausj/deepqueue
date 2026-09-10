import { test, expect } from "@playwright/test";

test("public setup, scoped credentials, exported skill, and administrator login", async ({
  page,
  context,
  browser,
}, testInfo) => {
  test.setTimeout(90000);
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
  const password = "fixture-browser-password";
  const replacement = "fixture-browser-new-password";
  const passwordSettings = page.getByRole("region", { name: "管理员密码设置" });
  await passwordSettings.getByLabel("启用密码登录").check();
  await passwordSettings
    .getByLabel("新管理员密码", { exact: true })
    .fill(password);
  await passwordSettings
    .getByLabel("确认管理员密码")
    .fill("fixture-mismatch-password");
  await passwordSettings.getByRole("button", { name: "保存密码设置" }).click();
  await expect(passwordSettings.getByRole("alert")).toHaveText(
    "两次输入的密码不一致",
  );
  await passwordSettings.getByLabel("确认管理员密码").fill(password);
  await passwordSettings.getByRole("button", { name: "保存密码设置" }).click();
  await expect(passwordSettings.getByRole("status")).toHaveText(
    "密码设置已保存",
  );
  await passwordSettings.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("password-settings-desktop.png"),
  });
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page.getByLabel("管理员密码", { exact: true })).toBeVisible();
  await expect(page.getByLabel("在此浏览器记住登录 30 天")).toBeChecked();
  await page.setViewportSize({ width: 320, height: 740 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("password-login-mobile.png"),
  });
  await page
    .getByLabel("管理员密码", { exact: true })
    .fill("fixture-wrong-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("管理员密码无效或未启用");
  await page.getByLabel("管理员密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "退出登录", exact: true }),
  ).toBeVisible();
  const cookies = await context.cookies();
  const session = cookies.find((cookie) => cookie.name === "deepqueue_session");
  expect(session.httpOnly).toBe(true);
  expect(session.sameSite).toBe("Strict");
  expect(session.expires - Date.now() / 1000).toBeGreaterThan(29 * 86400);
  expect(await page.evaluate(() => document.cookie)).not.toContain(
    "deepqueue_session",
  );
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(
    password,
  );
  // A fresh browser context with only persisted cookies must sign in without a password.
  const restored = await browser.newContext({
    storageState: { cookies, origins: [] },
  });
  try {
    const restoredPage = await restored.newPage();
    await restoredPage.goto("http://127.0.0.1:18765/#view=settings");
    await expect(
      restoredPage.getByRole("button", { name: "退出登录", exact: true }),
    ).toBeVisible();
    await page.reload();
    await expect(passwordSettings.getByLabel("当前密码")).toBeVisible();
    await passwordSettings.getByLabel("当前密码").fill(password);
    await passwordSettings
      .getByLabel("新管理员密码", { exact: true })
      .fill(replacement);
    await passwordSettings.getByLabel("确认管理员密码").fill(replacement);
    await passwordSettings
      .getByRole("button", { name: "保存密码设置" })
      .click();
    await expect(passwordSettings.getByRole("status")).toHaveText(
      "密码设置已保存",
    );
    await passwordSettings.scrollIntoViewIfNeeded();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await page.screenshot({
      path: testInfo.outputPath("password-settings-mobile.png"),
    });
    expect(
      (await restored.request.get("http://127.0.0.1:18765/api/state")).status(),
    ).toBe(401);
  } finally {
    await restored.close();
  }
  await passwordSettings.getByLabel("启用密码登录").uncheck();
  await passwordSettings.getByLabel("当前密码").fill(replacement);
  await passwordSettings.getByRole("button", { name: "保存密码设置" }).click();
  await expect(page.getByLabel("管理员访问令牌")).toBeVisible();
  expect(
    (await context.cookies()).some(
      (cookie) => cookie.name === "deepqueue_session",
    ),
  ).toBe(false);
  await page.getByLabel("管理员访问令牌").fill(admin);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "退出登录", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1000 });
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
  await page.getByLabel("在此浏览器记住登录 30 天").uncheck();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "退出登录", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "退出登录", exact: true }),
  ).toBeVisible();
  expect(
    (await context.cookies()).find(
      (cookie) => cookie.name === "deepqueue_session",
    ).expires,
  ).toBe(-1);
  expect(errors).toEqual([]);
});
