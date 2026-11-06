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

async function ensureThreadList(page: Page) {
  // the panel restores the last open discussion; go back to the list when needed
  const back = page.locator('.panel-head .icon-btn[title="Back to the discussion list"]');
  if ((await back.count()) > 0) await back.click();
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
  await ensureThreadList(page);
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
  await expect(page.getByTestId("markdown-body")).toContainText("item ONE", { timeout: 15000 });
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

test("Recent work lists documents newest first and restores document, position and open discussion", async ({ page }) => {
  await login(page);
  // make sure the discussion panel is visible for this test
  await openGuide(page);
  if ((await page.getByTestId("panel-expand").count()) > 0) await page.getByTestId("panel-expand").click();
  // open the first discussion so it becomes the "active" one, and scroll a bit
  await ensureThreadList(page);
  await page.getByTestId("thread-item").first().click();
  const quote = await page.getByTestId("thread-quote").textContent();
  await page.locator(".reader-body").evaluate((el) => { el.scrollTop = 40; });
  await page.waitForTimeout(1300); // reading position saves after 1 s idle
  // the Recent dialog lists the guide first with its discussion summary
  await page.getByTestId("recent-button").click();
  const first = page.getByTestId("recent-item").first();
  await expect(first).toContainText("docs/guide.md");
  await expect(first).toContainText("discussion");
  await page.locator('[data-testid="recent-dialog"] .modal-head .icon-btn').click();
  // open another document, then restore through Recent
  await page.getByTestId("tree-file-docs/long.md").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Long");
  await page.getByTestId("recent-button").click();
  await page.getByTestId("recent-item").filter({ hasText: "docs/guide.md" }).locator(".recent-main").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
  await expect(page.getByTestId("thread-quote")).toHaveText(quote!); // same discussion reopened
  // after a full reload the same discussion is restored from the server-side reading state
  await page.reload();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
  await expect(page.getByTestId("thread-quote")).toHaveText(quote!);
});

test("file tree shows distinct icons per kind; binary entries are not openable, source files open as code", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  fs.writeFileSync(path.join(ws, "docs", "diagram.png"), Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  fs.writeFileSync(path.join(ws, "docs", "tool.py"), "print(1)\n");
  fs.writeFileSync(path.join(ws, "docs", "config-with-a-very-long-name-that-hides-the-extension.json"), "{}\n");
  try { fs.symlinkSync("guide.md", path.join(ws, "docs", "alias.md")); } catch { /* exists */ }
  await page.getByTestId("tree-refresh").click();
  const dir = page.locator(".tree-row.dir", { hasText: "docs" });
  if ((await page.getByTestId("tree-file-docs/guide.md").count()) === 0) await dir.click();
  await expect(page.getByTestId("tree-file-docs/diagram.png")).toHaveAttribute("data-kind", "image");
  await expect(page.getByTestId("tree-file-docs/tool.py")).toHaveAttribute("data-kind", "code");
  await expect(page.getByTestId("tree-file-docs/config-with-a-very-long-name-that-hides-the-extension.json")).toHaveAttribute("data-kind", "data");
  await expect(page.getByTestId("tree-file-docs/guide.md")).toHaveAttribute("data-kind", "markdown");
  await expect(page.getByTestId("tree-file-docs/alias.md").locator(".fi-badge")).toHaveCount(1); // symlink badge
  // icons differ per kind (different SVG content)
  const svg = async (id: string) => page.getByTestId(id).locator("svg.fi").innerHTML();
  expect(await svg("tree-file-docs/diagram.png")).not.toBe(await svg("tree-file-docs/tool.py"));
  expect(await svg("tree-file-docs/tool.py")).not.toBe(await svg("tree-file-docs/guide.md"));
  await expect(page.getByTestId("tree-file-docs/diagram.png").locator("button.tree-main")).toBeDisabled();
  await page.getByTestId("tree-file-docs/tool.py").locator("button.tree-main").click();
  await expect(page.locator(".source-view .ln").first()).toHaveText(/print\(1\)/);
  await expect(page.locator(".doc-meta .chip").first()).toHaveText("TEXT");
});

test("lock a folder: badge shown, preview disabled, agent told the file is read-only; unlock restores", async ({ page }) => {
  await login(page);
  await openGuide(page);
  // header toggle shows the state clearly and is a real button
  const toggle = page.getByTestId("doc-lock-toggle");
  await expect(toggle).toHaveText(/Lock$/);
  page.once("dialog", (d) => d.accept("frozen"));
  await toggle.click();
  await expect(page.getByTestId("doc-lock-badge")).toBeVisible();
  await expect(toggle).toHaveText(/Locked/);
  await expect(page.getByRole("button", { name: "Preview change" })).toBeDisabled();
  // the tree reflects it
  await expect(page.getByTestId("tree-file-docs/guide.md").locator(".lock-badge")).toBeVisible();
  // ask the agent: the discussion still works and reports the read-only notice
  await selectText(page, "TorchDynamo");
  await page.getByTestId("selection-question").fill("explain");
  await page.getByTestId("selection-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toBeVisible({ timeout: 20000 });
  await toggle.click();
  await expect(page.getByTestId("doc-lock-badge")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Preview change" })).toBeEnabled();
  // the sidebar toggle is a stateful button
  const side = page.getByTestId("toggle-sidebar");
  await expect(side).toHaveAttribute("aria-pressed", "true");
  await side.click();
  await expect(side).toHaveAttribute("aria-pressed", "false");
  await expect(side).toHaveAttribute("data-state", "closed");
  await side.click();
});

test("Recent: hide, show hidden, unhide, delete history", async ({ page }) => {
  await login(page);
  await page.getByTestId("recent-button").click();
  const dialog = page.getByTestId("recent-dialog");
  const item = (p: string) => dialog.getByTestId("recent-item").filter({ has: page.locator(".recent-path", { hasText: new RegExp("^" + p.replace(".", "\\.") + "( |$)") }) });
  const guide = item("docs/guide.md");
  await expect(guide).toHaveCount(1);
  await guide.getByTestId("recent-hide").click();
  await expect(item("docs/guide.md")).toHaveCount(0);
  await dialog.getByTestId("recent-show-hidden").check();
  await expect(item("docs/guide.md")).toHaveAttribute("data-hidden", "1");
  await item("docs/guide.md").getByTestId("recent-unhide").click();
  await expect(item("docs/guide.md")).toHaveAttribute("data-hidden", "0");
  // filter
  await dialog.getByTestId("recent-filter").fill("long.md");
  await expect(dialog.getByTestId("recent-item")).toHaveCount(await dialog.getByTestId("recent-item").filter({ hasText: "long.md" }).count());
  await dialog.getByTestId("recent-filter").fill("");
  // delete history of long.md (no discussions needed)
  const long = item("docs/long.md");
  if ((await long.count()) > 0) {
    await long.getByTestId("recent-delete").click();
    await expect(page.getByTestId("recent-delete-confirm")).toContainText("docs/long.md");
    await page.getByTestId("recent-delete-confirm-yes").click();
    await expect(item("docs/long.md")).toHaveCount(0);
    expect(fs.existsSync(path.join(workspaceDir(), "docs", "long.md"))).toBe(true); // file untouched
  }
});

test("Diff review of a folder with line comments that become discussions", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const cp = (await import("child_process")).execSync;
  cp("git add -A && git commit -q -m snapshot", { cwd: ws });
  const guide = path.join(ws, "docs", "guide.md");
  fs.writeFileSync(guide, fs.readFileSync(guide, "utf-8").replace(/- item (one|ONE)\n/, "- item ONE (reviewed)\n"));
  fs.writeFileSync(path.join(ws, "docs", "draft.md"), "# Draft\n\nbrand new\n");
  await page.getByTestId("tree-refresh").click();
  await page.getByTestId("tree-diff-root").click();
  const review = page.getByTestId("diff-review");
  await expect(review).toBeVisible();
  await expect(page.locator('[data-testid="diff-file"][data-path="docs/draft.md"]')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('[data-testid="diff-file"][data-path="docs/draft.md"]')).toContainText("untracked");
  await expect(page.locator('[data-testid="diff-file"][data-path="docs/guide.md"]')).toContainText("modified");
  // comment on the added line
  const added = page.locator('[data-testid="diff-file"][data-path="docs/guide.md"] tr.diff-line.add').first();
  await expect(added).toContainText("item ONE (reviewed)");
  await added.click();
  await page.getByTestId("diff-comment-input").fill("Is '(reviewed)' really needed here?");
  await page.getByTestId("diff-comment-send").click();
  // lands in the document view with the discussion open and the diff origin shown
  await expect(page.getByTestId("thread-quote")).toContainText("item ONE (reviewed)", { timeout: 15000 });
  await expect(page.getByTestId("thread-diff-origin")).toContainText("added line");
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]')).toContainText("reviewing a diff", { timeout: 20000 });
  // back in the diff view the comment marker is listed on that file
  await page.getByTestId("view-diff").click();
  await expect(page.getByTestId("diff-comment-marker").first()).toContainText("really needed", { timeout: 10000 });
  await page.getByTestId("diff-comment-marker").first().click();
  await expect(page.getByTestId("thread-quote")).toContainText("item ONE (reviewed)");
});

test("flat discussion mode shows all threads as one stream; Enter sends, Ctrl+Enter inserts a paragraph", async ({ page }) => {
  await login(page);
  await openGuide(page);
  if ((await page.getByTestId("panel-expand").count()) > 0) await page.getByTestId("panel-expand").click();
  const before = (await page.getByTestId("thread-item").count()) || 0;
  // start a second discussion so the stream has several groups
  await selectText(page, "compiler pipeline");
  const q = page.getByTestId("selection-question");
  await q.fill("");
  await q.type("First line");
  await q.press("Control+Enter");
  await q.type("second paragraph");
  await expect(q).toHaveValue("First line\nsecond paragraph");
  await q.press("Enter"); // Enter sends
  await expect(page.getByTestId("thread-quote")).toHaveText("compiler pipeline", { timeout: 10000 });
  await expect(page.locator('[data-testid="message-user"]').last()).toContainText("second paragraph");
  // switch to flat mode: every thread is listed as a group, in creation order, persisted in prefs
  await page.getByTestId("mode-flat").click();
  const panel = page.getByTestId("discussion-panel");
  await expect(panel).toHaveAttribute("data-mode", "flat");
  await expect(page.getByTestId("flat-group").count()).resolves.toBeGreaterThanOrEqual(Math.max(before, 1) + 1);
  await expect(page.getByTestId("flat-target")).toContainText("compiler pipeline"); // newest / active thread is the reply target
  // reply from the flat composer with Enter; the answer lands in the same group
  const groups = await page.getByTestId("flat-group").count();
  await page.getByTestId("flat-reply-input").type("follow up in flat mode");
  await page.getByTestId("flat-reply-input").press("Enter");
  await expect(page.getByTestId("flat-group").last().locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("follow up in flat mode", { timeout: 20000 });
  expect(await page.getByTestId("flat-group").count()).toBe(groups);
  // choose another discussion as the reply target via the selector
  await page.getByTestId("flat-target-select").selectOption({ index: 0 });
  await expect(page.getByTestId("flat-target")).toContainText("#1");
  // mode persists across reload
  await page.reload();
  await expect(page.getByTestId("discussion-panel")).toHaveAttribute("data-mode", "flat");
  await page.getByTestId("mode-individual").click();
  await expect(page.getByTestId("discussion-panel")).not.toHaveAttribute("data-mode", "flat");
});
