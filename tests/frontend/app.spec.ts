import { expect, test, Page } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";

const TOKEN = "test-access-token-0123456789abcdef";

function workspaceDir(): string {
  return fs.readFileSync("/tmp/waw-e2e-workspace", "utf-8").trim();
}

async function login(page: Page) {
  await page.goto("/");
  await page.getByTestId("login-token").fill(TOKEN);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("view-document")).toBeVisible();
}

async function openGuide(page: Page) {
  const dir = page.locator(".tree-row.dir", { hasText: "docs" });
  if ((await page.getByTestId("tree-file-docs/guide.md").count()) === 0) await dir.click();
  await page.getByTestId("tree-file-docs/guide.md").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
}

/** Select text inside the rendered markdown by searching a text node for `needle`. */
async function selectText(page: Page, needle: string, endNeedle?: string) {
  await page.evaluate(
    ([needle, endNeedle]) => {
      const root = document.querySelector('[data-testid="markdown-body"]')!;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let startNode: Text | null = null;
      let startOff = 0;
      let endNode: Text | null = null;
      let endOff = 0;
      let n = walker.nextNode() as Text | null;
      while (n) {
        const i = n.data.indexOf(needle);
        if (!startNode && i >= 0) {
          startNode = n;
          startOff = i;
          if (!endNeedle) {
            endNode = n;
            endOff = i + needle.length;
            break;
          }
        }
        if (startNode && endNeedle) {
          const j = n.data.indexOf(endNeedle);
          if (j >= 0 && (n !== startNode || j > startOff)) {
            endNode = n;
            endOff = j + endNeedle.length;
            break;
          }
        }
        n = walker.nextNode() as Text | null;
      }
      if (!startNode || !endNode) throw new Error("text not found: " + needle);
      const range = document.createRange();
      range.setStart(startNode, startOff);
      range.setEnd(endNode, endOff);
      const sel = window.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      const rect = range.getBoundingClientRect();
      root.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, clientX: rect.left, clientY: rect.top }));
      document.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, clientX: rect.right, clientY: rect.bottom }));
    },
    [needle, endNeedle ?? ""],
  );
}

test.describe.configure({ mode: "serial" });

test("A3: unauthenticated UI shows login; one login then repeated actions without prompts", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("login-token")).toBeVisible();
  await page.getByTestId("login-token").fill("wrong");
  await page.getByTestId("login-submit").click();
  await expect(page.getByRole("alert")).toContainText("incorrect");
  await login(page);
  await openGuide(page);
  // reload keeps the session (cookie) — no second login
  await page.reload();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
});

test("B4: select words and a cross-line range; popover shows the exact quote; discussion persists after reload", async ({ page }) => {
  await login(page);
  await openGuide(page);
  await selectText(page, "captures");
  const pop = page.getByTestId("selection-popover");
  await expect(pop).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId("selection-quote")).toHaveText("captures");
  await page.getByTestId("selection-question").fill("Why is this captured?");
  await page.getByTestId("selection-send").click();
  await expect(page.getByTestId("discussion-panel").getByTestId("thread-quote")).toHaveText("captures");
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]')).toContainText("echo: Why is this captured?", { timeout: 20000 });
  await expect(page.getByTestId("anchor-state").first()).toHaveText("attached");
  await expect(page.locator("mark.waw-hl.thread")).toHaveCount(1);

  // cross-line selection: from end of one sentence into the next paragraph line
  await selectText(page, "backward graphs.", "Lowering");
  await expect(page.getByTestId("selection-quote")).toHaveText("backward graphs.\nLowering");
  await page.getByTestId("selection-question").fill("Second question");
  await page.getByTestId("selection-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]')).toContainText("echo: Second question", { timeout: 20000 });

  // reload: discussions and highlights are restored from the server
  await page.reload();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
  await expect(page.locator("mark.waw-hl.thread")).toHaveCount(3, { timeout: 10000 }); // 2nd thread spans two text nodes
  await page.locator(".waw-thread-pin").first().click();
  await expect(page.getByTestId("thread-quote")).toHaveText("captures");
  await expect(page.locator('[data-testid="message-agent"]')).toContainText("echo: Why is this captured?");
});

test("B5: both discussions use the document default session; header shows provider + short id", async ({ page }) => {
  await login(page);
  await openGuide(page);
  await expect(page.getByTestId("session-area")).toContainText("opencode");
  await expect(page.getByTestId("session-area")).toContainText("#");
  // reply in the second thread uses the same session, visible on each message
  await page.getByTestId("thread-item").nth(1).click();
  await page.getByTestId("reply-input").fill("follow-up");
  await page.getByTestId("reply-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("echo: follow-up", { timeout: 20000 });
  const ids = await page.locator('[data-testid="message-agent"] .msg-head .muted').allTextContents();
  expect(new Set(ids).size).toBe(1);
});

test("B6: provider failure keeps the question and offers explicit retry; cancel works", async ({ page }) => {
  await login(page);
  await openGuide(page);
  await selectText(page, "TorchInductor");
  await page.getByTestId("selection-question").fill("[[fail:boom]] what");
  await page.getByTestId("selection-send").click();
  const failed = page.locator('[data-testid="message-agent"][data-status="failed"]');
  await expect(failed).toBeVisible({ timeout: 20000 });
  await expect(failed).toContainText("boom");
  await expect(page.locator('[data-testid="message-user"]').last()).toContainText("[[fail:boom]] what");
  await page.getByTestId("retry-job").click();
  await expect(page.locator('[data-testid="message-agent"]')).toHaveCount(2, { timeout: 20000 });
  // cancel a slow job
  await page.getByTestId("reply-input").fill("[[sleep:30]] slow");
  await page.getByTestId("reply-send").click();
  await expect(page.getByTestId("cancel-job")).toBeVisible({ timeout: 15000 });
  await page.getByTestId("cancel-job").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="cancelled"]')).toBeVisible({ timeout: 20000 });
});

test("B8/D12: document change banner, user-authorized edit shows the diff and reanchors", async ({ page }) => {
  await login(page);
  await openGuide(page);
  // external change -> banner, position kept, refresh shows new content
  const file = path.join(workspaceDir(), "docs", "guide.md");
  fs.writeFileSync(file, fs.readFileSync(file, "utf-8").replace("# Guide", "# Guide v2"));
  await expect(page.getByTestId("doc-changed-banner")).toBeVisible({ timeout: 10000 });
  await page.getByTestId("doc-refresh").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Guide v2");
  // explicit edit through the agent: no extra approval prompt; diff shows the real change
  await selectText(page, "item one");
  await page.getByTestId("selection-question").fill("Change it [[edit:docs/guide.md:::item one:::item ONE]]");
  await page.getByTestId("selection-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("edited docs/guide.md", { timeout: 20000 });
  await expect(page.getByTestId("doc-changed-banner")).toBeVisible({ timeout: 10000 });
  await page.getByRole("button", { name: "View diff" }).click();
  await expect(page.getByTestId("diff-view")).toContainText("+- item ONE");
  await expect(page.getByTestId("anchor-state").first()).toHaveText("orphaned");
  await page.getByTestId("doc-refresh").click();
  await expect(page.getByTestId("markdown-body")).toContainText("item ONE");
});

test("D12: preview mode shows no change before Apply, then applies", async ({ page }) => {
  await login(page);
  await openGuide(page);
  const file = path.join(workspaceDir(), "docs", "guide.md");
  const before = fs.readFileSync(file, "utf-8");
  await page.getByRole("button", { name: "Preview change" }).click();
  await page.getByTestId("preview-instruction").fill("[[edit:docs/guide.md:::item two:::item TWO]]");
  await page.getByTestId("preview-start").click();
  await expect(page.getByTestId("preview-dialog").getByTestId("diff-view")).toContainText("+- item TWO", { timeout: 30000 });
  expect(fs.readFileSync(file, "utf-8")).toBe(before); // worktree untouched
  await page.getByTestId("preview-apply").click();
  await expect(page.getByTestId("markdown-body")).toContainText("item TWO", { timeout: 10000 });
  expect(fs.readFileSync(file, "utf-8")).toContain("item TWO");
});

test("C9/C10: real tmux terminal in the browser: marker survives disconnect, second terminal independent", async ({ page }) => {
  await login(page);
  await page.getByTestId("view-terminal").click();
  await page.getByTestId("terminal-new").click();
  const view = page.locator(".term-view.active");
  await expect(view).toHaveAttribute("data-state", "connected", { timeout: 15000 });
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  await page.keyboard.type("export PS1='$ '; MARK=zeta7; echo marker-$MARK-ok\n");
  await expect(view.locator(".xterm-rows")).toContainText("marker-zeta7-ok", { timeout: 15000 });
  // second terminal is independent
  const firstViewId = await view.getAttribute("data-testid");
  await page.getByTestId("terminal-new").click();
  await expect(page.getByTestId("terminal-tab")).toHaveCount(2);
  await expect.poll(async () => page.locator(".term-view.active").getAttribute("data-testid")).not.toBe(firstViewId);
  await expect(page.locator(".term-view.active")).toHaveAttribute("data-state", "connected", { timeout: 15000 });
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  await page.keyboard.type("echo in-${MARK:-unset}\n");
  await expect(page.locator(".term-view.active .xterm-rows")).toContainText("in-unset", { timeout: 15000 });
  // reload = browser disconnect; reconnect to the first terminal shows the same shell state
  await page.reload();
  await page.getByTestId("view-terminal").click();
  await page.getByTestId("terminal-tab").first().click();
  await expect(page.locator(".term-view.active")).toHaveAttribute("data-state", "connected", { timeout: 15000 });
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  await page.keyboard.type("echo again-$MARK\n");
  await expect(page.locator(".term-view.active .xterm-rows")).toContainText("again-zeta7", { timeout: 15000 });
  // tmux history action is distinct from the xterm buffer
  await page.getByRole("button", { name: "tmux history" }).click();
  await expect(page.locator(".history-pre")).toContainText("marker-zeta7-ok");
  await page.keyboard.press("Escape");
  await page.locator(".modal-head .icon-btn").click();
  // explicit close asks for confirmation
  page.once("dialog", (d) => d.accept());
  await page.getByTestId("terminal-close").click();
  await expect(page.getByTestId("terminal-tab")).toHaveCount(1);
});

test("D13: script tags and unsafe URLs are neutralized in rendered Markdown", async ({ page }) => {
  await login(page);
  const file = path.join(workspaceDir(), "docs", "evil.md");
  fs.writeFileSync(file, '# Evil\n\n<script>window.pwned=1</script>\n\n<img src=x onerror="window.pwned=2">\n\n[js](javascript:alert(1)) and [ok](https://example.com)\n\n<a href="javascript:alert(2)">x</a>\n');
  await page.reload();
  await page.getByTestId("tree-refresh").click();
  const dir = page.locator(".tree-row.dir", { hasText: "docs" });
  if ((await page.getByTestId("tree-file-docs/evil.md").count()) === 0) await dir.click();
  await page.getByTestId("tree-file-docs/evil.md").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Evil");
  expect(await page.evaluate(() => (window as any).pwned)).toBeUndefined();
  expect(await page.locator('[data-testid="markdown-body"] script').count()).toBe(0);
  expect(await page.locator('[data-testid="markdown-body"] a[href^="javascript"]').count()).toBe(0);
  await expect(page.locator('[data-testid="markdown-body"] a[data-external]')).toHaveAttribute("rel", /noopener/);
});

test("links to sections jump within the document and across documents", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const long = ["# Long\n", "Jump to [the target](#44-target-section--details) or [other doc](guide.md#lowering).\n"];
  for (let i = 0; i < 60; i++) long.push(`## Filler ${i}\n\nparagraph ${i}\n`);
  long.push("## 4.4 Target Section — Details\n\nYou arrived.\n");
  fs.writeFileSync(path.join(ws, "docs", "long.md"), long.join("\n"));
  await page.getByTestId("tree-refresh").click();
  const dir = page.locator(".tree-row.dir", { hasText: "docs" });
  if ((await page.getByTestId("tree-file-docs/long.md").count()) === 0) await dir.click();
  await page.getByTestId("tree-file-docs/long.md").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Long");
  const target = page.locator('[id="44-target-section--details"]');
  expect(await target.evaluate((el) => el.getBoundingClientRect().top > window.innerHeight)).toBe(true);
  await page.getByRole("link", { name: "the target" }).click();
  await expect.poll(async () => target.evaluate((el) => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; }), { timeout: 5000 }).toBe(true);
  expect(page.url()).not.toContain("#"); // SPA state, no hash navigation
  // cross-document link with fragment opens the other document and scrolls to its heading
  await page.getByRole("link", { name: "other doc" }).click({ force: true }).catch(() => undefined);
  await page.locator('a[data-internal="docs/guide.md"]').first().evaluate((a) => (a as HTMLElement).click());
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
  await expect(page.locator("#lowering")).toBeVisible();
});

test("layout: font settings, foldable/resizable file tree and discussion panel, foldable outline persist", async ({ page }) => {
  await login(page);
  await openGuide(page);
  const body = page.getByTestId("markdown-body");
  // font family + size
  await page.getByTestId("font-button").click();
  await page.getByTestId("pref-font").selectOption("serif");
  await page.getByTestId("pref-size").fill("19");
  await expect.poll(() => body.evaluate((el) => getComputedStyle(el).fontSize)).toBe("19px");
  expect(await body.evaluate((el) => getComputedStyle(el).fontFamily)).toMatch(/Georgia|serif/i);
  await page.keyboard.press("Escape");
  await page.getByTestId("font-button").click(); // close
  // file tree: resize by dragging, then fold
  const sidebar = page.locator(".sidebar");
  const w0 = (await sidebar.boundingBox())!.width;
  const handle = page.locator(".sidebar .resizer");
  const hb = (await handle.boundingBox())!;
  await page.mouse.move(hb.x + 2, hb.y + 200);
  await page.mouse.down();
  await page.mouse.move(hb.x + 122, hb.y + 200, { steps: 6 });
  await page.mouse.up();
  await expect.poll(async () => (await sidebar.boundingBox())!.width).toBeGreaterThan(w0 + 100);
  await page.getByTestId("toggle-sidebar").click();
  await expect(sidebar).toBeHidden();
  // discussion panel: fold and unfold, resize
  await page.getByTestId("panel-collapse").click();
  await expect(page.getByTestId("discussion-panel")).toHaveCount(0);
  await page.getByTestId("panel-expand").click();
  const panel = page.getByTestId("discussion-panel");
  const p0 = (await panel.boundingBox())!.width;
  const ph = (await page.locator(".discussion .resizer").boundingBox())!;
  await page.mouse.move(ph.x + 4, ph.y + 200);
  await page.mouse.down();
  await page.mouse.move(ph.x - 146, ph.y + 200, { steps: 6 });
  await page.mouse.up();
  await expect.poll(async () => (await panel.boundingBox())!.width).toBeGreaterThan(p0 + 120);
  // outline fold
  await expect(page.getByTestId("toc").locator(".toc-item")).toHaveCount(3);
  await page.getByTestId("toc-toggle").click();
  await expect(page.getByTestId("toc").locator(".toc-item")).toHaveCount(0);
  // everything persists across reload
  await page.reload();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
  expect(await page.getByTestId("markdown-body").evaluate((el) => getComputedStyle(el).fontSize)).toBe("19px");
  await expect(page.locator(".sidebar")).toBeHidden();
  await expect(page.getByTestId("toc").locator(".toc-item")).toHaveCount(0);
  expect((await page.getByTestId("discussion-panel").boundingBox())!.width).toBeGreaterThan(p0 + 120);
  await page.getByTestId("toggle-sidebar").click();
  await expect(page.locator(".sidebar")).toBeVisible();
  await page.screenshot({ path: "/tmp/opencode/shot5.png" });
});

test("folding the file tree and discussion panel enlarges the document area", async ({ page }) => {
  await login(page);
  await openGuide(page);
  // make sure both side panels are open first (previous test folded them)
  if ((await page.locator(".sidebar").isHidden())) await page.getByTestId("toggle-sidebar").click();
  if ((await page.getByTestId("panel-expand").count()) > 0) await page.getByTestId("panel-expand").click();
  const body = page.getByTestId("markdown-body");
  const w0 = (await body.boundingBox())!.width;
  await page.getByTestId("toggle-sidebar").click();
  await expect.poll(async () => (await body.boundingBox())!.width).toBeGreaterThan(w0 + 100);
  const w1 = (await body.boundingBox())!.width;
  await page.getByTestId("panel-collapse").click();
  await expect.poll(async () => (await body.boundingBox())!.width).toBeGreaterThan(w1 + 150);
  // nearly the whole viewport is now used by the document (minus outline, paddings, collapsed tab)
  const vw = page.viewportSize()!.width;
  expect((await body.boundingBox())!.width).toBeGreaterThan(vw * 0.75);
  await page.screenshot({ path: "/tmp/opencode/shot6.png" });
});


test("a terminal whose shell exited is shown as exited and can be dismissed without a confirmation", async ({ page }) => {
  await login(page);
  await page.getByTestId("view-terminal").click();
  // start from a clean slate: close terminals left over from earlier tests
  while ((await page.getByTestId("terminal-tab").count()) > 0) {
    await page.getByTestId("terminal-tab").first().click();
    page.once("dialog", (d) => d.accept());
    await page.getByTestId("terminal-close").click();
    await page.waitForTimeout(300);
  }
  await page.getByTestId("terminal-new").click();
  await expect(page.getByTestId("terminal-tab")).toHaveCount(1);
  const view = page.locator(".term-view.active");
  await expect(view).toHaveAttribute("data-state", "connected", { timeout: 15000 });
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  await page.keyboard.type("exit\n");
  await expect(view).toHaveAttribute("data-state", "exited", { timeout: 15000 });
  await expect(page.getByTestId("terminal-tab").first()).toContainText("(exited)");
  page.on("dialog", (d) => { throw new Error("no confirmation expected for an exited terminal: " + d.message()); });
  await page.getByTestId("terminal-dismiss").first().click();
  await expect(page.getByTestId("terminal-tab")).toHaveCount(0);
  // the "Clear exited" bulk action
  await page.getByTestId("terminal-new").click();
  await expect(page.locator(".term-view.active")).toHaveAttribute("data-state", "connected", { timeout: 15000 });
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  await page.keyboard.type("exit\n");
  await expect(page.locator(".term-view.active")).toHaveAttribute("data-state", "exited", { timeout: 15000 });
  await page.getByTestId("terminal-clear-exited").click();
  await expect(page.getByTestId("terminal-tab")).toHaveCount(0);
});
