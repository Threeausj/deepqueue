import { test, expect } from "@playwright/test";
const headers = { "X-DeepQueue": "1" };
const base = "/api/servers/local/codex";
const wrapper = "/home/train/.nvm/versions/node/v24.14.1/bin/codex";
const native =
  "/home/train/.nvm/versions/node/v24.14.1/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex";
const node = "/home/train/.nvm/versions/node/v24.14.1/bin/node";

test("npm installation discovery fills a draft, offers native binaries and handles missing installs", async ({
  page,
  request,
}) => {
  const config = (await (await request.get(base)).json()).config;
  let mode = "found",
    release;
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route(`**${base}/executables?*`, async (route) => {
    if (mode === "delayed")
      await new Promise((resolve) => {
        release = resolve;
      });
    if (mode === "error") {
      await route.fulfill({
        status: 502,
        json: { detail: "SSH 连接失败（测试）" },
      });
      return;
    }
    await route
      .fulfill({
        json: {
          server: "local",
          candidates:
            mode === "empty"
              ? []
              : [
                  {
                    path: wrapper,
                    kind: "npm",
                    source: "nvm",
                    version: "0.153.4",
                    node,
                    ready: mode !== "missing-node",
                    problem: mode === "missing-node" ? "缺少 Node" : null,
                  },
                  {
                    path: native,
                    kind: "npm-native",
                    source: "npm",
                    version: "0.153.4",
                    node: null,
                    ready: true,
                    problem: null,
                  },
                ],
          recommended:
            mode === "empty"
              ? null
              : mode === "missing-node"
                ? native
                : wrapper,
          warnings: [],
        },
      })
      .catch(() => {});
  });
  try {
    await page.goto("/#view=codex&server=local");
    await page.getByRole("button", { name: "连接设置", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "连接 local 的 Codex" });
    const input = dialog.getByLabel("Codex 可执行文件", { exact: true });
    const detect = dialog.getByRole("button", {
      name: "自动查找",
      exact: true,
    });
    await detect.click();
    await expect(input).toHaveValue(wrapper);
    await expect(
      dialog.getByText(`Node：${node}`, { exact: true }),
    ).toBeVisible();
    expect((await (await request.get(base)).json()).config).toEqual(config);
    const choices = dialog.getByRole("combobox", {
      name: "检测到的 Codex 安装",
    });
    await choices.selectOption(native);
    await expect(input).toHaveValue(native);
    await expect(
      dialog.getByText(`Node：${node}`, { exact: true }),
    ).not.toBeVisible();
    await dialog.getByRole("button", { name: "保存连接设置" }).click();
    await expect(dialog).not.toBeVisible();
    expect((await (await request.get(base)).json()).config.executable).toBe(
      native,
    );

    await page.getByRole("button", { name: "连接设置", exact: true }).click();
    mode = "missing-node";
    await detect.click();
    await expect(input).toHaveValue(native);
    await expect(
      choices.locator(`option[value="${wrapper}"]`),
    ).toHaveJSProperty("disabled", true);
    mode = "empty";
    await input.fill("/custom/bin/codex");
    await detect.click();
    await expect(dialog.getByText(/未找到 Codex/)).toBeVisible();
    await expect(input).toHaveValue("/custom/bin/codex");
    mode = "error";
    await detect.click();
    await expect(dialog.getByRole("alert")).toHaveText("SSH 连接失败（测试）");
    mode = "delayed";
    await detect.click();
    await expect(
      dialog.getByRole("button", { name: "正在查找…" }),
    ).toBeVisible();
    await input.fill("/keep/manual/codex");
    await expect.poll(() => typeof release).toBe("function");
    release();
    await expect(input).toHaveValue("/keep/manual/codex");
    await expect(dialog.locator(".codex-executable-result")).toHaveCount(0);
    mode = "found";
    await detect.click();
    await expect(input).toHaveValue(wrapper);
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(detect).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    expect((await (await request.get(base)).json()).config.executable).toBe(
      native,
    );
    expect(errors).toEqual([]);
  } finally {
    release?.();
    await request.post(`${base}/settings`, { headers, data: config });
  }
});
