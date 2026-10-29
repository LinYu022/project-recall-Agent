/** Real browser interaction; demo generation is explicitly synthetic, never a quality test. */
import { chromium, expect as baseExpect } from "@playwright/test";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createHash } from "node:crypto";

const session = JSON.parse(
  await readFile(
    resolve(
      process.env.RECALL_PRODUCT_SESSION ||
        "../.local/P1-17/synthetic-v1/session.json",
    ),
    "utf8",
  ),
);
const output = resolve(
  process.env.RECALL_PRODUCT_E2E_OUTPUT ||
    `../.local/P1-17/browser-${Date.now()}`,
);
await mkdir(output, { recursive: false });
const browser = await chromium.launch({
  channel: process.env.RECALL_BROWSER_CHANNEL || "msedge",
  headless: true,
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  recordVideo: { dir: output, size: { width: 1440, height: 1000 } },
});
const page = await context.newPage();
page.setDefaultTimeout(180000);
const errors = [],
  checks = [],
  http = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("response", (r) => {
  const u = new URL(r.url());
  if (u.origin === session.url)
    http.push({
      path: u.pathname,
      status: r.status(),
      method: r.request().method(),
    });
});
const expect = baseExpect.configure({
  timeout: session.profile === "official-demo" ? 180000 : 30000,
});
async function click(name) {
  const b = page.getByRole("button", { name, exact: true }).first();
  await expect(b).toBeEnabled();
  await b.click();
}
async function check(name, fn) {
  await fn();
  checks.push({ name, status: "passed" });
  console.log(JSON.stringify(checks.at(-1)));
}
async function create(preset) {
  await click("新建调查");
  await click(preset);
  const submitted = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/tasks" && r.request().method() === "POST",
  );
  await click("创建研究计划");
  const created = await (await submitted).json();
  await click("任务");
  await expect(
    page.getByRole("heading", { name: created.task_id, exact: true }),
  ).toBeVisible();
}
async function execute() {
  await click("提出执行版本");
  await expect(
    page.getByRole("heading", { name: "批准当前具体执行版本" }),
  ).toBeVisible();
  await click("批准此版本");
  await click("执行已授权计划");
  await expect(page.getByTestId("report")).toBeVisible({ timeout: 240000 });
}
async function taskID() {
  return (await page.getByRole("heading", { name: /^UI-/ }).innerText()).trim();
}
async function request(path, body) {
  const r = await context.request.fetch(session.url + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${session.token}` },
    timeout: 180000,
    ...(body === undefined ? {} : { data: body }),
  });
  return { status: r.status(), body: await r.json() };
}
const artifacts = {};
const runtimeInfo = (await request("/product/info")).body;
let success = false;
try {
  await page.goto(session.url);
  await check("quality_reservation_and_simulation_visible", async () => {
    await expect(
      page.getByText("模型质量验收未获证实", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("当前为模拟生成 · 外部模型调用 0"),
    ).toBeVisible();
  });
  await check("invalid_token_denied", async () => {
    await page.getByLabel("本地访问凭据").fill("synthetic-invalid-token");
    await click("连接");
    await expect(page.getByRole("alert")).toContainText("权限");
  });
  await page.getByLabel("本地访问凭据").fill(session.token);
  await click("连接");
  await expect(
    page.getByText("已连接。凭据仅保留在当前页面内存中。"),
  ).toBeVisible();
  await check("credential_not_persisted", async () => {
    expect(
      await page.evaluate(() => ({
        local: localStorage.length,
        session: sessionStorage.length,
      })),
    ).toEqual({ local: 0, session: 0 });
    await expect(page.getByLabel("本地访问凭据")).toHaveValue("");
  });
  await check("clarification_does_not_guess_year", async () => {
    await create("缺少年份");
    await expect(
      page.getByRole("heading", { name: "补充调查条件" }),
    ).toBeVisible();
    await page.getByLabel("补充 model_year", { exact: true }).fill("2010");
    await click("提交澄清");
    await expect(page.getByText("计划就绪", { exact: true })).toBeVisible();
  });
  const simple = await taskID();
  await check("simple_query_frozen_sources", async () => {
    await click("查询冻结资料");
    await expect(page.getByTestId("research")).toBeVisible();
    await expect(page.getByTestId("research")).toContainText(/10V039|PE10006/);
  });
  await check("pause_and_resume_same_plan", async () => {
    // Hold a background list read: it must not disable or swallow the user's mutation.
    let observed;
    const pollingStarted = new Promise((resolve) => {
      observed = resolve;
    });
    await page.route("**/product/tasks?*", async (route) => {
      observed();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await route.continue();
    });
    await pollingStarted;
    await expect(
      page.getByRole("button", { name: "暂停", exact: true }),
    ).toBeEnabled({ timeout: 500 });
    await click("暂停");
    await expect(page.getByText("已暂停", { exact: true })).toBeVisible();
    await page.unroute("**/product/tasks?*");
    await click("恢复计划");
    await expect(page.getByText("计划就绪", { exact: true })).toBeVisible();
  });
  await check("explicit_approval_then_insufficient_evidence", async () => {
    await execute();
    await expect(page.getByTestId("report")).toContainText("证据不足");
    await expect(page.getByText("独立语义核查：待审")).toBeVisible();
  });
  const report = (await request(`/tasks/${simple}/report`)).body;
  artifacts.simple = { task: (await request(`/tasks/${simple}`)).body, report };
  const textAlias = Object.keys(report.aliases).find(
    (a) =>
      report.cards.find((c) => c.evidence_id === report.aliases[a]).kind ===
      "text",
  );
  await check("evidence_original_replay", async () => {
    const summary = page
      .locator("summary")
      .filter({ hasText: new RegExp(`^${textAlias} ·`) });
    await summary.click();
    await click(`定位原文 ${textAlias}`);
    await expect(page.getByTestId("detail")).toContainText("verified");
    artifacts.original = (
      await request(`/product/tasks/${simple}/original/${textAlias}`)
    ).body;
  });
  await check("execution_history_replay", async () => {
    await click("读取执行回放");
    await expect(page.getByText(/report_published/).first()).toBeVisible();
    artifacts.history = (await request(`/tasks/${simple}/history`)).body;
  });
  await page.screenshot({
    path: resolve(output, "evidence-desktop.png"),
    fullPage: true,
  });
  await check("revocation_hides_report_and_original", async () => {
    await click("撤销任务授权");
    await expect(page.getByTestId("report")).toHaveCount(0);
    await expect(page.getByTestId("detail")).toHaveCount(0);
    expect((await request(`/tasks/${simple}/report`)).status).toBe(403);
    expect(
      (await request(`/product/tasks/${simple}/original/${textAlias}`)).status,
    ).toBe(403);
  });
  await check("bounded_zero_match_counterexample", async () => {
    await create("年份不匹配");
    await execute();
    await expect(page.getByTestId("report")).toContainText("允许来源内无匹配");
    artifacts.wrong_year = (
      await request(`/tasks/${await taskID()}/report`)
    ).body;
    expect(JSON.stringify(artifacts.wrong_year)).toContain(
      "does_not_establish_no_recalls_in_the_world",
    );
  });
  await check("cancel_is_terminal_and_copy_creates_new_task", async () => {
    await create("Prius 示例");
    const id = await taskID();
    await click("取消任务");
    expect(
      (
        await request(`/tasks/${id}/resume`, {
          expected_version: (await request(`/tasks/${id}`)).body.version,
        })
      ).status,
    ).toBe(409);
    const copied = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/tasks" &&
        r.request().method() === "POST",
    );
    await click("复制为新计划");
    const copy = await (await copied).json();
    await expect(
      page.getByRole("heading", { name: copy.task_id, exact: true }),
    ).toBeVisible();
    expect(copy.task_id).not.toBe(id);
  });
  await check("stale_version_rejected_with_ui_explanation", async () => {
    const id = await taskID();
    await page.route(`**/tasks/${id}/pause`, async (route) => {
      const body = route.request().postDataJSON();
      await route.continue({
        postData: JSON.stringify({
          ...body,
          expected_version: body.expected_version - 1,
        }),
      });
    });
    await click("暂停");
    await expect(page.getByRole("alert")).toContainText("版本已过期");
    await page.unroute(`**/tasks/${id}/pause`);
    artifacts.stale_version_status = 409;
    await expect(page.getByText("计划就绪", { exact: true })).toBeVisible();
  });
  await check("historical_request_waits_without_auto_acceptance", async () => {
    await click("新建调查");
    await click("Prius 示例");
    await page
      .getByLabel("时间模式", { exact: true })
      .selectOption("historical_as_of");
    await click("创建研究计划");
    await click("任务");
    await expect(
      page.getByText("接受上方冻结时间，放弃本次历史时点要求"),
    ).toBeVisible();
    await expect(
      page.getByRole("checkbox", {
        name: "接受上方冻结时间，放弃本次历史时点要求",
      }),
    ).not.toBeChecked();
    artifacts.historical = (await request(`/tasks/${await taskID()}`)).body;
  });
  await check("two_record_relationship_and_unknowns", async () => {
    await create("F-150 对照");
    await click("查询冻结资料");
    const buttons = page.getByRole("button", {
      name: "选入关系对照",
      exact: true,
    });
    await expect(buttons.first()).toBeVisible();
    await buttons.nth(0).click();
    await buttons.nth(0).click();
    await click("固定这两条记录");
    await click("查询冻结资料");
    const linksResponse = page
      .waitForResponse(
        (r) =>
          new URL(r.url()).pathname.endsWith("/research") &&
          r.request().postDataJSON()?.operation === "links",
      )
      .catch((error) => ({ errorType: error.name }));
    await click("查询已选关系");
    const links = await linksResponse;
    if (links.errorType)
      throw new Error("relationship_response_" + links.errorType);
    expect(links.status()).toBe(200);
    const relationship = await links.json();
    await expect(page.getByTestId("research")).toContainText(
      "完整检索或关系结果",
    );
    const t = (await request(`/tasks/${await taskID()}`)).body;
    expect(t.plan.request.pairs).toHaveLength(1);
    artifacts.relationship = relationship;
    await execute();
    artifacts.complex_report = (
      await request(`/tasks/${t.task_id}/report`)
    ).body;
    expect(
      artifacts.complex_report.cards.some((c) => c.kind === "relationship"),
    ).toBe(true);
  });
  await check("memory_candidate_review_source_and_revoke", async () => {
    await click("记忆");
    await click("准备演示偏好候选");
    await expect(
      page.getByRole("heading", { name: "检查演示记忆候选" }),
    ).toBeVisible();
    await click("确认保存此演示偏好");
    const item = page
      .locator(".claim")
      .filter({ hasText: "演示偏好：输出英文（可撤销）" })
      .last();
    await item.locator("summary").click();
    await item.getByRole("button", { name: "查看原始记忆来源" }).click();
    await expect(page.getByTestId("detail")).toContainText("模拟用户偏好");
    await item.getByRole("button", { name: "停用记忆" }).click();
    await expect(item).toContainText("disabled");
    artifacts.memory = (await request("/product/memories")).body;
  });
  await check("explicit_local_failure_is_not_styled_as_answer", async () => {
    await create("模拟请求失败");
    await click("提出执行版本");
    await click("批准此版本");
    await click("执行已授权计划");
    await expect(page.getByText("已暂停", { exact: true })).toBeVisible({
      timeout: 240000,
    });
    await expect(page.getByTestId("report")).toHaveCount(0);
    await expect(page.getByRole("alert")).toContainText("没有发布可交付报告");
    const id = await taskID();
    artifacts.failure = {
      task: (await request(`/tasks/${id}`)).body,
      dispatch: (await request(`/tasks/${id}/dispatch`)).body,
      history: (await request(`/tasks/${id}/history`)).body,
    };
    await page.screenshot({
      path: resolve(output, "partial-failure.png"),
      fullPage: true,
    });
  });
  await check("mobile_readable_without_horizontal_overflow", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: resolve(output, "memory-mobile.png"),
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
  await check("browser_no_javascript_errors", async () =>
    expect(errors).toEqual([]),
  );
  success = true;
} catch (error) {
  // Playwright transport errors may include request headers; never print its raw stack/message.
  const message = error.message
    .split("Call log:")[0]
    .replace(/Bearer\s+\S+/g, "Bearer [REDACTED]")
    .slice(0, 1200);
  errors.push({
    type: error.name,
    message,
    after: checks.at(-1)?.name || "startup",
  });
  console.error(
    JSON.stringify({
      status: "failed",
      error_type: error.name,
      message,
      after: checks.at(-1)?.name,
    }),
  );
  process.exitCode = 1;
  await page
    .screenshot({ path: resolve(output, "failed-state.png"), fullPage: true })
    .catch(() => {});
} finally {
  await context.close();
  await browser.close();
  // No traces, authorization headers, token entry values, or raw sessions are exported.
  const result = {
    timestamp: new Date().toISOString(),
    profile: session.profile,
    classification:
      "real_browser_http_postgresql_with_explicit_simulated_generation",
    status: success ? "passed" : "failed",
    checks,
    errors,
    http,
    artifacts,
    runtime_info: runtimeInfo,
    new_external_model_calls: 0,
    new_source_requests: 0,
    second_human_user: false,
  };
  await writeFile(
    resolve(output, "result.json"),
    JSON.stringify(result, null, 2) + "\n",
  );
  const video = await page.video()?.path();
  if (video)
    result.video = {
      file: video.split(/[\\/]/).at(-1),
      sha256: createHash("sha256")
        .update(await readFile(video))
        .digest("hex"),
    };
  await writeFile(
    resolve(output, "result.json"),
    JSON.stringify(result, null, 2) + "\n",
  );
  console.log(
    JSON.stringify({ status: result.status, checks: checks.length, output }),
  );
}
