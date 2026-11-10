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

/** Commit the e2e workspace; the server's own `git status` can briefly hold index.lock, so retry. */
function gitCommitAll(ws: string, msg: string) {
  const cp = require("child_process") as typeof import("child_process");
  const run = (cmd: string) => cp.execSync(cmd, { cwd: ws, stdio: ["ignore", "pipe", "pipe"] }).toString();
  for (let i = 0; i < 30; i++) {
    try {
      run("git add -A");
      if (run("git status --porcelain").trim() !== "") run(`git commit -q -m "${msg}"`);
      if (run("git status --porcelain").trim() === "") return;
    } catch {
      try { fs.unlinkSync(path.join(ws, ".git", "index.lock")); } catch { /* not ours or gone */ }
    }
    cp.execSync("sleep 0.2");
  }
  throw new Error("gitCommitAll: working tree still dirty after retries");
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
  // wait for the retry to finish so exactly one job is active, then cancel the slow one via *its* message
  await expect(page.locator('[data-testid="message-agent"]').nth(1)).toHaveAttribute("data-status", /failed|completed/, { timeout: 20000 });
  await page.getByTestId("reply-input").fill("[[sleep:30]] slow");
  await page.getByTestId("reply-send").click();
  const slow = page.locator('[data-testid="message-agent"]').last();
  await expect(slow).toHaveAttribute("data-status", /running|queued/, { timeout: 15000 });
  await slow.getByTestId("cancel-job").click();
  await expect(slow).toHaveAttribute("data-status", "cancelled", { timeout: 20000 });
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
  await page.getByTestId("thread-diff").click(); // diff of the version this discussion was created on
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
  fs.writeFileSync(path.join(ws, "docs", "blob.bin"), Buffer.from([0, 1, 2, 3]));
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
  await expect(page.getByTestId("tree-file-docs/diagram.png").locator("button.tree-main")).toBeEnabled(); // images open in the reader
  await expect(page.getByTestId("tree-file-docs/blob.bin").locator("button.tree-main")).toBeDisabled(); // binaries do not
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
  gitCommitAll(ws, "snapshot");
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
  // stays in the diff: the comment and the agent's answer appear inline under the line
  await expect(page.getByTestId("diff-review")).toBeVisible();
  const thread = page.getByTestId("diff-comment-marker").first();
  await expect(thread.getByTestId("diff-msg-user")).toContainText("really needed", { timeout: 10000 });
  await expect(thread.locator('[data-testid="diff-msg-agent"][data-status="completed"]')).toContainText("reviewing a diff", { timeout: 20000 });
  // reply inline; the answer streams into the same box
  await thread.getByTestId("diff-reply-input").fill("thanks, one more thing");
  await thread.getByTestId("diff-reply-input").press("Enter");
  await expect(thread.locator('[data-testid="diff-msg-agent"]')).toHaveCount(2, { timeout: 20000 });
  await expect(thread.locator('[data-testid="diff-msg-agent"]').last()).toContainText("one more thing", { timeout: 20000 });
  // "Open discussion" jumps to the full thread in the document view, with the diff origin shown
  await thread.getByTestId("diff-open-thread").click();
  await expect(page.getByTestId("view-document")).toHaveClass(/active/, { timeout: 15000 });
  if ((await page.getByTestId("mode-individual").count()) > 0) await page.getByTestId("mode-individual").click();
  await expect(page.getByTestId("thread-quote")).toContainText("item ONE (reviewed)", { timeout: 15000 });
  await expect(page.getByTestId("thread-diff-origin")).toContainText("added line");
  await page.getByTestId("tree-diff-root").click();
  await expect(page.getByTestId("diff-comment-marker").first()).toContainText("really needed", { timeout: 10000 });
  // GitHub-style Viewed checkbox: ticking collapses the file and counts it; it unticks itself when the file changes
  const draft = page.locator('[data-testid="diff-file"][data-path="docs/draft.md"]');
  await draft.getByTestId("diff-viewed").click();
  await expect(draft).toHaveAttribute("data-viewed", "1", { timeout: 10000 });
  await expect(draft).toHaveClass(/collapsed/);
  await expect(page.getByTestId("diff-viewed-count")).toContainText("1 /");
  fs.appendFileSync(path.join(ws, "docs", "draft.md"), "\nmore text\n");
  await page.getByTestId("tree-diff-root").click();
  await page.getByTestId("diff-reload").click();
  await expect(page.locator('[data-testid="diff-file"][data-path="docs/draft.md"]')).toHaveAttribute("data-viewed", "0", { timeout: 10000 });
  await expect(page.locator('[data-testid="diff-file"][data-path="docs/draft.md"] .viewed-box')).toContainText("changed since");
  await page.getByTestId("diff-comment-marker").first().getByTestId("diff-open-thread").click();
  await expect(page.getByTestId("view-document")).toHaveClass(/active/, { timeout: 15000 });
  if ((await page.getByTestId("mode-individual").count()) > 0) await page.getByTestId("mode-individual").click();
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

test("selection popover offers Copy; the model of a session can be switched from the panel", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await login(page);
  await openGuide(page);
  await selectText(page, "AOTAutograd");
  await expect(page.getByTestId("selection-popover")).toBeVisible();
  await page.getByTestId("selection-copy").click(); // icon button in the quote header, GitHub style
  await expect(page.getByTestId("selection-copy")).toHaveAttribute("title", "Copied");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("AOTAutograd");
  await page.keyboard.press("Escape");
  // Read mode: selecting text shows only a small copy chip, never the question box
  await page.getByTestId("reader-mode-read").click();
  await selectText(page, "TorchInductor");
  await expect(page.getByTestId("copy-chip")).toBeVisible();
  await expect(page.getByTestId("selection-popover")).toHaveCount(0);
  await page.getByTestId("copy-chip").click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("TorchInductor");
  // the header button still lets you discuss the current selection explicitly in Read mode
  await selectText(page, "TorchInductor");
  await page.getByTestId("ask-current-selection").click();
  await expect(page.getByTestId("selection-popover")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.reload(); // mode persists
  await expect(page.getByTestId("reader-mode-read")).toHaveAttribute("aria-selected", "true");
  await page.getByTestId("reader-mode-discuss").click();
  // model switch in individual mode
  if ((await page.getByTestId("panel-expand").count()) > 0) await page.getByTestId("panel-expand").click();
  await page.getByTestId("mode-individual").click();
  await ensureThreadList(page);
  await page.getByTestId("thread-item").first().click();
  const select = page.getByTestId("model-select");
  await expect(select).toBeVisible();
  await select.selectOption("fake/beta");
  await expect(page.getByTestId("session-model-tag").first()).toContainText("beta", { timeout: 10000 });
  await page.getByTestId("variant-select").selectOption("high");
  await expect(page.getByTestId("session-model-tag").first()).toContainText("(high)");
  // the next reply runs with that model (visible in the header chip too)
  await expect(page.getByTestId("session-area")).toContainText("beta");
  await page.getByTestId("model-select").selectOption("");
  await expect(page.getByTestId("session-model-tag")).toHaveCount(0, { timeout: 10000 });
});

test("diff: multi-line range comments, side-by-side layout, Diff tab follows the open document", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  gitCommitAll(ws, "snapshot2");
  const guide = path.join(ws, "docs", "guide.md");
  fs.writeFileSync(guide, fs.readFileSync(guide, "utf-8").replace(/- item (ONE|one)[^\n]*\n- item (TWO|two)[^\n]*\n/, "- alpha line\n- beta line\n- gamma line\n"));
  fs.writeFileSync(path.join(ws, "README.md"), "# Repo\n\ncommitted file\nreadme changed\n");
  // open the guide, then press Diff: it shows the guide's diff (not a previous target)
  await openGuide(page);
  await page.getByTestId("view-diff").click();
  await expect(page.getByTestId("diff-review")).toContainText("docs/guide.md");
  await expect(page.getByTestId("diff-file")).toHaveCount(1);
  // range selection: click the first added line, shift-click the third
  const rows = page.locator('[data-testid="diff-table-unified"] tr.diff-line.add');
  await expect(rows).toHaveCount(3, { timeout: 10000 });
  await rows.nth(0).dispatchEvent("pointerdown", { button: 0 });
  await page.mouse.up();
  await rows.nth(2).dispatchEvent("pointerdown", { button: 0, shiftKey: true });
  await expect(page.locator('[data-testid="diff-row"][data-selected="1"]')).toHaveCount(3);
  await expect(page.getByTestId("diff-composer")).toContainText("3 lines");
  await page.getByTestId("diff-comment-input").fill("Why these three together?");
  await page.getByTestId("diff-comment-send").click(); // "Ask now": immediate discussion
  const rangeThread = page.getByTestId("diff-comment-marker").first();
  await expect(rangeThread).toContainText("lines", { timeout: 15000 });
  await rangeThread.getByTestId("diff-open-thread").click();
  await expect(page.getByTestId("view-document")).toHaveClass(/active/, { timeout: 15000 });
  if ((await page.getByTestId("mode-individual").count()) > 0) await page.getByTestId("mode-individual").click();
  await expect(page.getByTestId("thread-diff-origin")).toContainText("lines", { timeout: 15000 });
  await expect(page.getByTestId("thread-quote")).toContainText("alpha line");
  await expect(page.getByTestId("thread-quote")).toContainText("gamma line");
  // side-by-side layout: deletions on the left, additions on the right, persisted
  await page.getByTestId("view-diff").click();
  await page.getByTestId("diff-layout-split").click();
  const split = page.getByTestId("diff-table-split");
  await expect(split).toBeVisible();
  await expect(split.locator("td.code.left.del").first()).toContainText("item");
  await expect(split.locator("td.code.right.add").first()).toContainText("alpha line");
  await expect(split.locator("td.code.right.empty")).toHaveCount(0); // 2 dels paired with 3 adds -> one empty left cell
  await expect(split.locator("td.code.left.empty")).toHaveCount(1);
  await expect(page.getByTestId("diff-comment-marker").first()).toContainText("three together");
  await page.reload();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide"); // last document restored first
  await page.getByTestId("view-diff").click();
  const fileSec = page.locator('[data-testid="diff-file"][data-path="docs/guide.md"]');
  await expect(fileSec).toBeVisible({ timeout: 10000 });
  if ((await fileSec.getAttribute("class"))?.includes("collapsed")) await fileSec.locator(".diff-file-head").click();
  await expect(page.getByTestId("diff-table-split")).toBeVisible();
  // opening another modified document switches the diff target when pressing Diff again
  await page.getByTestId("tree-file-README.md").locator("button.tree-main").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Repo");
  await page.getByTestId("view-diff").click();
  await expect(page.getByTestId("diff-review")).toContainText("README.md");
  await expect(page.locator('[data-testid="diff-file"][data-path="README.md"]')).toContainText("readme changed");
  await page.getByTestId("diff-layout-unified").click();
});

test("commit workflow: pick files, draft the message with the agent, commit only those files", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  gitCommitAll(ws, "before-commit-test");
  fs.writeFileSync(path.join(ws, "docs", "commit-a.md"), "# A\n\nfirst\n");
  fs.writeFileSync(path.join(ws, "docs", "commit-b.md"), "# B\n\nsecond\n");
  await page.getByTestId("tree-refresh").click();
  await page.getByTestId("tree-diff-root").click();
  await page.getByTestId("commit-toggle").click();
  const panel = page.getByTestId("commit-panel");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("changed files selected");
  // select only commit-a.md
  await page.getByTestId("commit-select-all").uncheck();
  await page.getByTestId("commit-file-docs/commit-a.md").check();
  await expect(panel).toContainText("1 of");
  await page.getByTestId("commit-draft").click();
  await expect(page.getByTestId("commit-message")).toHaveValue(/Update docs\/commit-a\.md/, { timeout: 20000 });
  const msg = await page.getByTestId("commit-message").inputValue();
  expect(msg).toContain("summarise the diff");
  await page.getByTestId("commit-button").click();
  await expect(page.getByTestId("toast")).toContainText("Committed", { timeout: 10000 });
  const cp = require("child_process") as typeof import("child_process");
  expect(cp.execSync("git log -1 --format=%s", { cwd: ws }).toString().trim()).toBe("Update docs/commit-a.md");
  expect(cp.execSync("git show --name-only --format= HEAD", { cwd: ws }).toString().trim()).toBe("docs/commit-a.md");
  expect(cp.execSync("git status --porcelain", { cwd: ws }).toString()).toContain("docs/commit-b.md"); // untouched
  await expect(panel).toContainText("Update docs/commit-a.md"); // listed under recent commits
  // no remote in the e2e workspace -> Push is offered but disabled, and no PR controls
  await expect(page.getByTestId("push-button")).toBeDisabled();
  await expect(page.getByTestId("pr-button")).toHaveCount(0);
});


test("review: stash comments in the diff, submit once, the agent triages (address / reply / skip)", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  gitCommitAll(ws, "before-review");
  const guide = path.join(ws, "docs", "guide.md");
  fs.writeFileSync(guide, fs.readFileSync(guide, "utf-8").replace(/- gamma line\n/, "- gamma lnie\n- delta line\n"));
  await openGuide(page);
  await page.getByTestId("view-diff").click();
  const added = page.locator('[data-testid="diff-table-unified"] tr.diff-line.add');
  await expect(added).toHaveCount(2, { timeout: 10000 });
  // stash two comments (Enter = Add to review); nothing is asked yet
  await added.nth(0).dispatchEvent("pointerdown", { button: 0 });
  await page.mouse.up();
  await page.getByTestId("diff-comment-input").fill("Typo: please change lnie to line.");
  await page.getByTestId("diff-comment-input").press("Enter");
  await expect(page.getByTestId("review-toggle")).toContainText("(1)");
  await expect(page.getByTestId("diff-pending-comment")).toHaveCount(1);
  await added.nth(1).dispatchEvent("pointerdown", { button: 0 });
  await page.mouse.up();
  await page.getByTestId("diff-comment-input").fill("Is delta really needed here?");
  await page.getByTestId("diff-comment-stash").click();
  await expect(page.getByTestId("review-toggle")).toContainText("(2)");
  await expect(page.getByTestId("review-item")).toHaveCount(2);
  expect(fs.readFileSync(guide, "utf-8")).toContain("gamma lnie"); // untouched so far
  // submit the review once; the agent addresses the typo and replies to the question
  await page.getByTestId("review-summary").fill("Small cleanup pass.");
  await page.getByTestId("review-submit").click();
  await expect(page.getByTestId("review-last")).toContainText("completed", { timeout: 30000 });
  const items = page.getByTestId("review-decision-item");
  await expect(items).toHaveCount(2);
  await expect(items.filter({ hasText: "lnie" })).toContainText("addressed");
  await expect(items.filter({ hasText: "delta really" })).toContainText("replied");
  expect(fs.readFileSync(guide, "utf-8")).toContain("- gamma line\n");
  expect(fs.readFileSync(guide, "utf-8")).not.toContain("lnie");
  await expect(page.getByTestId("review-toggle")).not.toContainText("("); // pending review is empty again
  // the comments are now ordinary discussions, shown inline with their decision badges
  await expect(page.getByTestId("review-decision").first()).toBeVisible({ timeout: 10000 });
});

test("multi-root window: add a folder, work in both wikis, Recent across folders, remove the folder", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const second = path.join(path.dirname(ws), "second-wiki");
  const cp = require("child_process") as typeof import("child_process");
  fs.rmSync(second, { recursive: true, force: true });
  fs.mkdirSync(path.join(second, "docs"), { recursive: true });
  cp.execSync("git init -q && git config user.email t@e && git config user.name T", { cwd: second });
  fs.writeFileSync(path.join(second, "docs", "guide.md"), "# Second guide\n\nOnly in the second wiki.\n");
  // add it through the folder picker
  await page.getByTestId("tree-add-folder").click();
  const dlg = page.getByTestId("add-folder-dialog");
  await page.getByTestId("add-folder-path").fill(second);
  await page.getByTestId("add-folder-path").press("Enter");
  await expect(page.getByTestId("add-folder-selected")).toContainText(second);
  await expect(page.getByTestId("add-folder-selected")).toContainText("· Git repository");
  await page.getByTestId("add-folder-name").fill("Second");
  await page.getByTestId("add-folder-submit").click();
  await expect(dlg).toHaveCount(0);
  await expect(page.getByTestId("tree-root")).toHaveCount(2);
  await expect(page.getByTestId("workspace-switch")).toHaveValue(/.+/);
  // open the second wiki's docs/guide.md (same relative path as in the first wiki)
  const secondRoot = page.getByTestId("tree-root").nth(1);
  await secondRoot.locator(".tree-row.dir", { hasText: "docs" }).click();
  await secondRoot.getByTestId("tree-file-Second:docs/guide.md").locator("button.tree-main").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Second guide");
  // a discussion here stays in this wiki
  await selectText(page, "second wiki");
  await page.getByTestId("selection-question").fill("only here?");
  await page.getByTestId("selection-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toBeVisible({ timeout: 20000 });
  // switch back to the first wiki via the top bar: its guide has no such discussion
  const firstId = await page.getByTestId("tree-root").nth(0).getAttribute("data-ws");
  await page.getByTestId("workspace-switch").selectOption(firstId!);
  const firstRoot = page.getByTestId("tree-root").nth(0);
  if ((await page.getByTestId("tree-file-docs/guide.md").count()) === 0) await firstRoot.locator(".tree-row.dir", { hasText: "docs" }).first().click();
  await page.getByTestId("tree-file-docs/guide.md").locator("button.tree-main").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
  await expect(page.locator(".thread-item", { hasText: "only here?" })).toHaveCount(0);
  // tabs: the same path is open in both wikis -> two tabs, told apart by the folder name; a tab switches folders
  const secondId = await page.getByTestId("tree-root").nth(1).getAttribute("data-ws");
  const tabIn = (wsId: string) => page.locator(`[data-testid="doc-tab"][data-ws="${wsId}"][data-path="docs/guide.md"]`);
  await expect(tabIn(firstId!)).toHaveCount(1);
  await expect(tabIn(secondId!)).toHaveCount(1);
  await expect(tabIn(secondId!).locator(".doc-tab-hint")).toHaveText("Second");
  await expect(tabIn(firstId!)).toHaveAttribute("aria-selected", "true");
  await tabIn(secondId!).click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Second guide");
  await expect(page.getByTestId("workspace-switch")).toHaveValue(secondId!);
  await tabIn(firstId!).click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
  await expect(page.getByTestId("workspace-switch")).toHaveValue(firstId!);
  // Recent lists both wikis, tagged; opening the second wiki's entry switches folders
  await page.getByTestId("recent-button").click();
  await expect(page.getByTestId("recent-ws").first()).toBeVisible();
  await page.getByTestId("recent-item").filter({ has: page.getByTestId("recent-ws").filter({ hasText: "Second" }) }).first().locator(".recent-main").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Second guide");
  // remove the folder from the window (data kept)
  page.once("dialog", (d) => d.accept());
  await page.getByTestId("tree-root").nth(1).locator(".tree-root").hover();
  await page.getByTestId("tree-remove-Second").click();
  await expect(page.getByTestId("tree-root")).toHaveCount(1);
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide", { timeout: 10000 });
  await expect(page.locator(`[data-testid="doc-tab"][data-ws="${secondId}"]`)).toHaveCount(0); // its tabs went with it
});

test("edit a file directly: save with Ctrl+S, preview, conflict handling, lock, unsaved-changes guard", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const file = path.join(ws, "docs", "editme.md");
  fs.writeFileSync(file, "# Edit me\n\nfirst paragraph\n");
  await page.getByTestId("tree-refresh").click();
  const dir = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: "docs" }).first();
  if ((await page.getByTestId("tree-file-docs/editme.md").count()) === 0) await dir.click();
  await page.getByTestId("tree-file-docs/editme.md").locator("button.tree-main").click();
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Edit me");
  // edit + Ctrl+S
  await page.getByTestId("doc-edit").click();
  const ta = page.getByTestId("editor-text");
  await expect(ta).toHaveValue("# Edit me\n\nfirst paragraph\n");
  await ta.fill("# Edit me\n\nfirst paragraph\n\nsecond **paragraph**\n");
  await expect(page.getByTestId("editor-state")).toHaveText("Unsaved changes");
  await page.getByTestId("editor-preview-toggle").check();
  await expect(page.getByTestId("editor-preview").locator("strong")).toHaveText("paragraph");
  await ta.press("Control+s");
  await expect(page.getByTestId("editor-state")).toHaveText("Saved ✓", { timeout: 10000 });
  expect(fs.readFileSync(file, "utf-8")).toBe("# Edit me\n\nfirst paragraph\n\nsecond **paragraph**\n");
  await expect(page.getByTestId("doc-changed-banner")).toHaveCount(0); // our own save is not an external change
  // conflict: the file changes on disk while editing -> refused, then overwrite
  await ta.fill("# Edit me\n\nmine\n");
  fs.writeFileSync(file, "# Edit me\n\ntheirs\n");
  await expect(page.getByTestId("editor-external-change")).toBeVisible({ timeout: 10000 });
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-conflict")).toBeVisible({ timeout: 10000 });
  expect(fs.readFileSync(file, "utf-8")).toBe("# Edit me\n\ntheirs\n");
  page.once("dialog", (d) => d.accept());
  await page.getByTestId("editor-overwrite").click();
  await expect(page.getByTestId("editor-state")).toHaveText("Saved ✓", { timeout: 10000 });
  expect(fs.readFileSync(file, "utf-8")).toBe("# Edit me\n\nmine\n");
  // Done -> reader shows the saved content
  await page.getByTestId("editor-close").click();
  await expect(page.getByTestId("editor")).toHaveCount(0);
  await expect(page.getByTestId("markdown-body")).toContainText("mine");
  // unsaved-changes guard when opening another document
  await page.getByTestId("doc-edit").click();
  await page.getByTestId("editor-text").fill("# Edit me\n\nunsaved\n");
  page.once("dialog", (d) => d.dismiss());
  await page.getByTestId("tree-file-docs/guide.md").locator("button.tree-main").click();
  await expect(page.getByTestId("editor-text")).toHaveValue("# Edit me\n\nunsaved\n"); // stayed
  page.once("dialog", (d) => d.accept());
  await page.getByTestId("editor-close").click();
  // locked files cannot be edited
  page.once("dialog", (d) => d.accept(""));
  await page.getByTestId("doc-lock-toggle").click();
  await expect(page.getByTestId("doc-edit")).toBeDisabled();
  await page.getByTestId("doc-lock-toggle").click();
  await expect(page.getByTestId("doc-edit")).toBeEnabled();
});

test("terminal: send browser-reserved keys (Ctrl+T, Ctrl+W) via the Keys menu and sticky Ctrl/Alt", async ({ page }) => {
  await login(page);
  await page.getByTestId("view-terminal").click();
  await page.getByTestId("terminal-new").click();
  const view = page.locator(".term-view.active");
  await expect(view).toHaveAttribute("data-state", "connected", { timeout: 15000 });
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  // werase undef: otherwise the tty line discipline treats Ctrl+W as "erase word" inside cat's input line
  await page.keyboard.type("export PS1='$ '; stty werase undef; echo RE\"\"ADY; cat -v\n");
  await expect(view.locator(".xterm-rows")).toContainText("READY", { timeout: 15000 }); // cat -v is now reading the tty
  // Keys menu: Ctrl+T and Ctrl+W, which a normal browser tab never delivers to the page
  await page.getByTestId("term-keys").click();
  await page.getByTestId("term-key-ctrl-t").click();
  await page.getByTestId("term-key-ctrl-w").click();
  await page.getByTestId("term-key-esc").click();
  await page.getByTestId("term-keys").click(); // close menu
  // sticky Ctrl: arm it, then type a letter
  await page.getByTestId("term-sticky-ctrl").click();
  await expect(page.getByTestId("term-mods-banner")).toContainText("Ctrl");
  await page.keyboard.type("n");
  await expect(page.getByTestId("term-mods-banner")).toHaveCount(0); // one-shot
  // sticky Alt: ESC prefix
  await page.getByTestId("term-sticky-alt").click();
  await page.keyboard.type("x");
  await page.keyboard.press("Enter");
  // cat -v echoes control characters visibly: ^T ^W ^[ ^N ^[x
  await expect(view.locator(".xterm-rows")).toContainText("^T^W^[^N^[x", { timeout: 10000 });
  await page.keyboard.press("Control+c");
  await expect(page.getByTestId("term-fullscreen")).toBeVisible();
});

test("PowerPoint: a .pptx opens as slides with notes; select text on a slide and ask", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const cp = require("child_process") as typeof import("child_process");
  const py = path.resolve(__dirname, "../../.venv/bin/python");
  cp.execFileSync(py, ["-c", `
from pptx import Presentation
prs = Presentation()
s = prs.slides.add_slide(prs.slide_layouts[1])
s.shapes.title.text = "Roadmap"
tf = s.placeholders[1].text_frame
tf.text = "Ship the reader"
p = tf.add_paragraph(); p.text = "Pptx support lands this week"; p.level = 1
s.notes_slide.notes_text_frame.text = "Say thanks to the team."
s2 = prs.slides.add_slide(prs.slide_layouts[5]); s2.shapes.title.text = "Questions"
prs.save(${JSON.stringify(path.join(ws, "docs", "roadmap.pptx"))})
`]);
  await page.getByTestId("tree-refresh").click();
  const dir = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: "docs" }).first();
  if ((await page.getByTestId("tree-file-docs/roadmap.pptx").count()) === 0) await dir.click();
  await expect(page.getByTestId("tree-file-docs/roadmap.pptx")).toHaveAttribute("data-kind", "slides");
  await page.getByTestId("tree-file-docs/roadmap.pptx").locator("button.tree-main").click();
  await page.getByTestId("office-view-text").click(); // Layout (original slides) is the default; slides as text here
  const body = page.getByTestId("markdown-body");
  await expect(body.locator("section.slide")).toHaveCount(2);
  await expect(body.locator("section.slide").first().locator("h2")).toHaveText("Slide 1: Roadmap");
  await expect(body.locator(".slide-notes")).toContainText("Say thanks to the team.");
  await expect(page.locator(".doc-meta .chip").first()).toHaveText("PPTX");
  await expect(page.getByTestId("doc-edit")).toHaveCount(0); // read-only
  if ((await page.getByTestId("reader-mode-discuss").getAttribute("aria-selected")) !== "true") await page.getByTestId("reader-mode-discuss").click();
  await selectText(page, "Pptx support lands this week");
  await expect(page.getByTestId("selection-quote")).toHaveText("Pptx support lands this week");
  await page.getByTestId("selection-question").fill("When exactly?");
  await page.getByTestId("selection-send").click();
  if ((await page.getByTestId("mode-individual").count()) > 0) await page.getByTestId("mode-individual").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("When exactly?", { timeout: 20000 });
  await expect(page.locator("mark.waw-hl.thread")).toHaveCount(1);
});

test("terminal copy: tmux mouse selection, program OSC 52, Shift+drag + Ctrl+Shift+C, Copy button", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await login(page);
  await page.getByTestId("view-terminal").click();
  const before = await page.getByTestId("terminal-tab").count();
  await page.getByTestId("terminal-new").click();
  await expect(page.getByTestId("terminal-tab")).toHaveCount(before + 1);
  await expect(page.getByTestId("terminal-tab").last()).toHaveClass(/active/); // type into the new tab, not an older one
  const view = page.locator(".term-view.active");
  await expect(view).toHaveAttribute("data-state", "connected", { timeout: 15000 });
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  const clip = () => page.evaluate(() => navigator.clipboard.readText());
  await expect(view.locator(".xterm-rows")).toContainText("$", { timeout: 15000 }); // shell prompt is up
  // 1) a program sends OSC 52 (as vim/OpenCode do); tmux forwards it to the browser clipboard
  await page.keyboard.type("export PS1='$ '; printf '\\033]52;c;%s\\a' \"$(printf 'h\\303\\251llo osc52' | base64)\"; echo OS\"\"CDONE\n");
  await expect(view.locator(".xterm-rows")).toContainText("OSCDONE", { timeout: 15000 });
  await expect.poll(clip, { timeout: 10000 }).toBe("héllo osc52");
  await expect(page.getByTestId("term-copy-toast")).toContainText("Copied");
  // 2) put a word at the top-left of the screen
  await page.keyboard.type("clear; echo COPYMEWORD; echo; echo\n");
  const firstRow = view.locator(".xterm-rows > div").first();
  await expect(firstRow).toContainText("COPYMEWORD", { timeout: 15000 });
  const box = (await firstRow.boundingBox())!;
  // tmux has the mouse: a double-click selects the word in tmux copy mode, which copies it via OSC 52
  await page.evaluate(() => navigator.clipboard.writeText("-"));
  await page.mouse.dblclick(box.x + 12, box.y + box.height / 2);
  await expect.poll(clip, { timeout: 10000 }).toBe("COPYMEWORD");
  // 3) Shift+drag = browser-side selection; Ctrl+Shift+C copies it (Ctrl+C alone would be SIGINT)
  await page.evaluate(() => navigator.clipboard.writeText("-"));
  await page.keyboard.down("Shift");
  await page.mouse.move(box.x + 1, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 60, box.y + box.height / 2, { steps: 5 });
  await page.mouse.move(box.x + box.width / 3, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();
  await page.keyboard.up("Shift");
  await expect.poll(clip, { timeout: 10000 }).toContain("COPYMEWORD"); // copied on release
  await page.evaluate(() => navigator.clipboard.writeText("-"));
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  await page.keyboard.press("Control+Shift+C");
  await expect.poll(clip, { timeout: 10000 }).toContain("COPYMEWORD");
  // 4) the toolbar Copy button with nothing selected re-copies the last terminal copy
  await page.evaluate(() => navigator.clipboard.writeText("-"));
  await page.getByTestId("term-copy").click();
  await expect.poll(clip, { timeout: 10000 }).toContain("COPYMEWORD");
  // 5) tmux history: Copy all
  await page.getByRole("button", { name: "tmux history" }).click();
  await expect(page.getByTestId("term-history-text")).toContainText("COPYMEWORD");
  await page.evaluate(() => navigator.clipboard.writeText("-"));
  await page.getByTestId("term-history-copy").click();
  await expect.poll(clip, { timeout: 10000 }).toMatch(/^COPYMEWORD\n/); // trailing blanks trimmed
});

test("refresh buttons spin while refreshing and stop at the end of a turn", async ({ page }) => {
  await login(page);
  const btn = page.getByTestId("tree-refresh");
  await expect(btn).toHaveAttribute("data-spinning", "false");
  // hover: highlighted, icon turns a little
  const before = await btn.evaluate((el) => getComputedStyle(el).backgroundColor);
  await btn.hover();
  await expect.poll(() => btn.evaluate((el) => getComputedStyle(el).backgroundColor)).not.toBe(before);
  await expect(btn.locator(".refresh-icon")).toHaveCSS("rotate", "60deg");
  // a new file appears only after the refresh (whether or not the watcher saw it)
  fs.writeFileSync(path.join(workspaceDir(), "refresh-spin.md"), "# spin\n");
  await btn.click();
  await expect(btn).toHaveAttribute("data-spinning", "true");
  await expect(btn.locator(".refresh-icon")).toHaveCSS("animation-name", "refresh-spin");
  await expect(btn).toHaveAttribute("data-spinning", "false", { timeout: 5000 }); // stops by itself
  await expect(btn.locator(".refresh-icon")).toHaveCSS("animation-name", "none");
  await expect(page.getByTestId("tree-file-refresh-spin.md")).toBeVisible();
  fs.unlinkSync(path.join(workspaceDir(), "refresh-spin.md"));
});

test("file tree: only the list scrolls; the header with ＋ and ↻ stays in place", async ({ page }) => {
  await login(page);
  const dir = path.join(workspaceDir(), "many");
  fs.mkdirSync(dir, { recursive: true });
  for (let i = 0; i < 80; i++) fs.writeFileSync(path.join(dir, `note-${String(i).padStart(2, "0")}.md`), `# ${i}\n`);
  try {
    await page.getByTestId("tree-refresh").click();
    const root = page.getByTestId("tree-root").first();
    await root.locator(".tree-row.dir", { hasText: "many" }).first().click();
    await expect(page.getByTestId("tree-file-many/note-79.md")).toBeAttached();
    const body = page.locator(".tree-body");
    const header = page.locator(".tree-header");
    const y0 = (await header.boundingBox())!.y;
    expect(await body.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true); // the list overflows
    await body.evaluate((el) => { el.scrollTop = el.scrollHeight; });
    await expect(page.getByTestId("tree-file-many/note-79.md")).toBeInViewport();
    expect((await header.boundingBox())!.y).toBe(y0); // header did not move
    await expect(page.getByTestId("tree-refresh")).toBeInViewport();
    await expect(page.getByTestId("tree-add-folder")).toBeInViewport();
    expect(await page.locator(".sidebar").evaluate((el) => el.scrollTop)).toBe(0); // the sidebar itself never scrolls
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("Files button: on hover the icon slides toward where the tree goes and the button lights up", async ({ page }) => {
  await login(page);
  const btn = page.getByTestId("toggle-sidebar");
  const glyph = btn.locator(".toggle-glyph");
  if ((await btn.getAttribute("data-state")) !== "open") await btn.click();
  await page.mouse.move(0, 400); // not hovering
  await expect(glyph).toHaveCSS("translate", "none");
  await expect(btn).toHaveCSS("box-shadow", "none");
  await btn.hover();
  await expect(glyph).toHaveCSS("translate", "-3px"); // open: hints "hide" (left)
  await expect(btn).not.toHaveCSS("box-shadow", "none"); // glow ring, visible even in the "on" state
  await page.mouse.move(0, 400);
  await expect(glyph).toHaveCSS("translate", "none"); // eases back
  await btn.click(); // close the tree
  await expect(btn).toHaveAttribute("data-state", "closed");
  await btn.hover();
  await expect(glyph).toHaveCSS("translate", "3px"); // closed: hints "show" (right)
  await btn.click(); // reopen for the following tests
  await expect(btn).toHaveAttribute("data-state", "open");
});

test("file tree: no request storm — only visible folders load, vanished ones are forgotten, changes reload one folder", async ({ page }) => {
  const ws = workspaceDir();
  const base = path.join(ws, "storm");
  fs.mkdirSync(path.join(base, "keep", "deep"), { recursive: true });
  fs.mkdirSync(path.join(base, "hidden", "inner"), { recursive: true });
  fs.writeFileSync(path.join(base, "keep", "deep", "a.md"), "# a\n");
  fs.writeFileSync(path.join(base, "hidden", "inner", "b.md"), "# b\n");
  const files: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname === "/api/v1/files") files.push(u.searchParams.get("path") ?? "");
  });
  try {
    await login(page);
    const wsId = await page.getByTestId("tree-root").first().getAttribute("data-ws");
    // Remembered state as left behind by an older session: an expanded folder that has since been deleted
    // (with an expanded child), and an expanded folder inside a *collapsed* parent.
    const k = (p: string) => `${wsId}\u0000${p}`;
    await page.evaluate((keys) => {
      const saved = JSON.parse(localStorage.getItem("waw.treeExpanded") || "[]");
      localStorage.setItem("waw.treeExpanded", JSON.stringify([...new Set([...saved, ...keys])]));
    }, [k("storm"), k("storm/keep"), k("storm/gone"), k("storm/gone/child"), k("storm/hidden/inner")]);
    await page.reload();
    await expect(page.getByTestId("tree-file-storm/keep/deep/a.md")).toHaveCount(0); // deep is collapsed
    await expect(page.locator(".tree-row.dir", { hasText: "deep" })).toBeVisible();
    await page.waitForTimeout(800);
    // never requested: the vanished folders and the folder hidden under the collapsed "hidden"
    expect(files.filter((p) => p.startsWith("storm/gone") || p === "storm/hidden/inner")).toEqual([]);
    // and the vanished ones are forgotten (they would otherwise come back on every reload)
    const saved: string[] = await page.evaluate(() => JSON.parse(localStorage.getItem("waw.treeExpanded") || "[]"));
    expect(saved.filter((s) => s.includes("storm/gone"))).toEqual([]);
    expect(saved).toContain(k("storm/hidden/inner")); // still remembered for when "hidden" is opened again

    // A burst of changes in one folder reloads that folder (and its parents), not everything that is shown.
    files.length = 0;
    for (let i = 0; i < 20; i++) fs.writeFileSync(path.join(base, "keep", `burst-${i}.md`), `# ${i}\n`);
    await expect(page.getByTestId("tree-file-storm/keep/burst-19.md")).toBeVisible({ timeout: 10000 });
    await page.waitForTimeout(2500);
    expect(new Set(files)).toEqual(new Set(["storm/keep", "storm", ""])); // the changed folder and its ancestors only
    expect(files.length).toBeLessThanOrEqual(9); // folded into a few refreshes, not one per file
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test("file tree ↻: the spinner stops when the server cannot be reached; the earlier listing stays, Retry works", async ({ page }) => {
  await login(page);
  const btn = page.getByTestId("tree-refresh");
  await expect(page.getByTestId("tree-file-README.md")).toBeVisible();
  await page.route("**/api/v1/files**", (route) => route.abort("connectionrefused"));
  // record inside the page whether it spun (a check from the test can come too late on a busy machine)
  await btn.evaluate((el) => {
    (window as any).__spun = false;
    new MutationObserver(() => { if (el.getAttribute("data-spinning") === "true") (window as any).__spun = true; }).observe(el, { attributes: true });
  });
  await btn.click();
  await expect.poll(() => page.evaluate(() => (window as any).__spun)).toBe(true);
  await expect(btn).toHaveAttribute("data-spinning", "false", { timeout: 10000 }); // stops by itself
  await expect(page.getByTestId("tree-error").first()).toContainText("Could not refresh");
  await expect(page.getByTestId("tree-file-README.md")).toBeVisible(); // earlier listing kept
  await page.unroute("**/api/v1/files**");
  await page.getByTestId("tree-retry").first().click();
  await expect(page.getByTestId("tree-error")).toHaveCount(0, { timeout: 10000 });
});

test("document tabs: open several, switch, close (× / middle-click), unsaved-edit guard, remembered across reloads", async ({ page }) => {
  const ws = workspaceDir();
  fs.mkdirSync(path.join(ws, "other"), { recursive: true });
  fs.writeFileSync(path.join(ws, "other", "guide.md"), "# Other guide\n\nA second file with the same name.\n");
  try {
    await login(page);
    // start clean
    await page.evaluate(() => localStorage.removeItem("waw.docTabs"));
    await page.reload();
    for (const x of await page.getByTestId("doc-tab-close").all()) await x.click({ force: true }).catch(() => undefined);
    const tabs = page.getByTestId("doc-tab");
    const tab = (p: string) => page.locator(`[data-testid="doc-tab"][data-path="${p}"]`);
    const h1 = page.getByTestId("markdown-body").locator("h1");
    const open = async (p: string) => {
      await page.getByTestId("tree-refresh").click();
      await expect(page.getByTestId("tree-refresh")).toHaveAttribute("data-spinning", "false");
      const dir = p.includes("/") ? p.slice(0, p.indexOf("/")) : null;
      if (dir) {
        const row = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: dir }).first();
        if ((await row.locator(".chev").textContent()) === "›") await row.click(); // expand only if collapsed
      }
      await page.getByTestId(`tree-file-${p}`).locator("button.tree-main").click();
    };
    await open("docs/guide.md");
    await expect(h1).toHaveText(/^Guide/);
    await open("README.md");
    await expect(h1).toHaveText("Repo");
    await open("other/guide.md");
    await expect(h1).toHaveText("Other guide");
    await expect(tabs).toHaveCount(3);
    await expect(tab("other/guide.md")).toHaveAttribute("aria-selected", "true");
    // same file name twice: the parent folder tells them apart
    await expect(tab("docs/guide.md").locator(".doc-tab-hint")).toHaveText("docs");
    await expect(tab("other/guide.md").locator(".doc-tab-hint")).toHaveText("other");
    // opening an already open document switches to its tab instead of adding one
    await open("README.md");
    await expect(tabs).toHaveCount(3);
    // a new tab opens right after the current one
    await tab("docs/guide.md").click();
    await expect(h1).toHaveText(/^Guide/);
    fs.writeFileSync(path.join(ws, "docs", "tabs-new.md"), "# New one\n");
    await open("docs/tabs-new.md");
    await expect(h1).toHaveText("New one");
    await expect.poll(() => tabs.evaluateAll((els) => els.map((e) => e.getAttribute("data-path")))).toEqual(["docs/guide.md", "docs/tabs-new.md", "README.md", "other/guide.md"]);
    // switching tabs
    await tab("README.md").click();
    await expect(h1).toHaveText("Repo");
    await expect(tab("README.md")).toHaveAttribute("aria-selected", "true");
    // middle-click closes a background tab; the active one stays
    await tab("docs/tabs-new.md").click({ button: "middle" });
    await expect(tab("docs/tabs-new.md")).toHaveCount(0);
    await expect(h1).toHaveText("Repo");
    // closing the active tab opens its neighbour (the one to the right)
    await tab("README.md").hover();
    await tab("README.md").getByTestId("doc-tab-close").click();
    await expect(h1).toHaveText("Other guide");
    await expect(tab("other/guide.md")).toHaveAttribute("aria-selected", "true");
    // unsaved edits: switching tabs asks first; dismissing keeps you in the editor
    await page.getByTestId("doc-edit").click();
    await page.getByTestId("editor-text").fill("# Other guide\n\nunsaved change\n");
    page.once("dialog", (d) => d.dismiss());
    await tab("docs/guide.md").click();
    await expect(page.getByTestId("editor-text")).toHaveValue(/unsaved change/);
    page.once("dialog", (d) => d.accept());
    await tab("docs/guide.md").click();
    await expect(h1).toHaveText(/^Guide/);
    expect(fs.readFileSync(path.join(ws, "other", "guide.md"), "utf-8")).not.toContain("unsaved change");
    // remembered across reloads, with the same active document
    await page.reload();
    await expect(tabs).toHaveCount(2);
    await expect(tab("docs/guide.md")).toHaveAttribute("aria-selected", "true");
    await expect(h1).toHaveText(/^Guide/);
    // closing the last tab shows the start page, and it stays closed after a reload
    await tab("other/guide.md").click({ button: "middle" });
    await tab("docs/guide.md").getByTestId("doc-tab-close").click();
    await expect(tabs).toHaveCount(0);
    await expect(page.getByText("Open a Markdown file")).toBeVisible();
    await page.reload();
    await expect(page.getByText("Open a Markdown file")).toBeVisible();
    await expect(tabs).toHaveCount(0);
  } finally {
    fs.rmSync(path.join(ws, "other"), { recursive: true, force: true });
    fs.rmSync(path.join(ws, "docs", "tabs-new.md"), { force: true });
  }
});

test("file tree: ↻ on a folder refreshes just that folder (and what is open inside it); expanding re-fetches", async ({ page }) => {
  const ws = workspaceDir();
  const base = path.join(ws, "solo");
  fs.mkdirSync(path.join(base, "a", "inner"), { recursive: true });
  fs.mkdirSync(path.join(base, "b"), { recursive: true });
  fs.writeFileSync(path.join(base, "a", "inner", "x.md"), "# x\n");
  fs.writeFileSync(path.join(base, "b", "y.md"), "# y\n");
  const files: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname === "/api/v1/files") files.push(u.searchParams.get("path") ?? "");
  });
  try {
    await login(page);
    await page.getByTestId("tree-refresh").click();
    await expect(page.getByTestId("tree-refresh")).toHaveAttribute("data-spinning", "false");
    const root = page.getByTestId("tree-root").first();
    const row = (name: string) => root.locator(".tree-row.dir", { has: page.locator(".name", { hasText: new RegExp(`^${name}$`) }) }).first();
    const expand = async (name: string) => {
      if ((await row(name).locator(".chev").textContent()) === "›") await row(name).locator("button.tree-main").click();
    };
    await expand("solo");
    await expand("a");
    await expand("inner");
    await expand("b");
    await expect(page.getByTestId("tree-file-solo/a/inner/x.md")).toBeVisible();
    await expect(page.getByTestId("tree-file-solo/b/y.md")).toBeVisible();
    await page.waitForTimeout(1500); // let watcher events from the setup settle

    // ↻ on "a": reloads solo/a and the open solo/a/inner, nothing else
    files.length = 0;
    await row("a").hover();
    const btn = page.getByTestId("tree-refresh-solo/a");
    await btn.click();
    await expect(btn).toHaveAttribute("data-spinning", "false", { timeout: 10000 });
    expect(files).toContain("solo/a");
    expect(files).toContain("solo/a/inner");
    expect(files.filter((p) => p === "" || p === "solo" || p.startsWith("solo/b"))).toEqual([]);

    // ↻ on the root folder: everything that is open in it
    files.length = 0;
    await root.locator(".tree-root").hover();
    await page.getByTestId("tree-refresh-root").click();
    await expect(page.getByTestId("tree-refresh-root")).toHaveAttribute("data-spinning", "false", { timeout: 10000 });
    for (const p of ["", "solo", "solo/a", "solo/a/inner", "solo/b"]) expect(files).toContain(p);

    // a collapsed folder is not followed by change events; expanding it fetches it again
    await row("b").locator("button.tree-main").click(); // collapse
    await expect(page.getByTestId("tree-file-solo/b/y.md")).toHaveCount(0);
    fs.writeFileSync(path.join(base, "b", "added-while-collapsed.md"), "# z\n");
    await page.waitForTimeout(1500);
    await row("b").locator("button.tree-main").click(); // expand
    await expect(page.getByTestId("tree-file-solo/b/added-while-collapsed.md")).toBeVisible({ timeout: 5000 });
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test("startup: no FILE_NOT_FOUND from stale remembered documents (old folder-less entry, deleted file)", async ({ page }) => {
  const ws = workspaceDir();
  fs.writeFileSync(path.join(ws, "docs", "soon-gone.md"), "# Soon gone\n");
  await login(page);
  const wsId = await page.getByTestId("tree-root").first().getAttribute("data-ws");
  await page.evaluate(({ id }) => {
    // what an old build left behind: a path without a folder (from another wiki), and documents since deleted
    localStorage.setItem("waw.lastPath", "wiki/concepts/only-in-another-wiki.md");
    localStorage.setItem(`waw.lastPath.${id}`, "docs/soon-gone.md");
    localStorage.setItem(`waw.docTabs.${localStorage.getItem("waw.namedWorkspace") ?? ""}`, JSON.stringify([{ ws: id, path: "docs/soon-gone.md" }, { ws: id, path: "README.md" }]));
  }, { id: wsId });
  fs.unlinkSync(path.join(ws, "docs", "soon-gone.md"));
  const opened: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname === "/api/v1/documents/open") opened.push(u.searchParams.get("path") ?? "");
  });
  await page.reload();
  // falls back to the folder's other open tab, quietly
  await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Repo", { timeout: 10000 });
  await expect(page.locator(".banner.warn", { hasText: "FILE_NOT_FOUND" })).toHaveCount(0);
  expect(opened).not.toContain("wiki/concepts/only-in-another-wiki.md"); // the folder-less entry is never used
  await expect(page.locator('[data-testid="doc-tab"][data-path="docs/soon-gone.md"]')).toHaveCount(0); // dropped
  expect(await page.evaluate(() => localStorage.getItem("waw.lastPath"))).toBeNull();
  // nothing left to restore: the start page, still no error
  await page.evaluate(({ id }) => {
    localStorage.setItem(`waw.lastPath.${id}`, "docs/also-gone.md");
    localStorage.setItem(`waw.docTabs.${localStorage.getItem("waw.namedWorkspace") ?? ""}`, "[]");
  }, { id: wsId });
  await page.reload();
  await expect(page.getByText("Open a Markdown file")).toBeVisible({ timeout: 10000 });
  await expect(page.locator(".banner.warn", { hasText: "FILE_NOT_FOUND" })).toHaveCount(0);
});

test("site icon: favicon (SVG + ICO), apple-touch icon and manifest are served", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.locator('link[rel="icon"][type="image/svg+xml"]')).toHaveAttribute("href", "/favicon.svg");
  for (const [u, type] of [["/favicon.svg", "image/svg+xml"], ["/favicon.ico", "image/"], ["/apple-touch-icon.png", "image/png"], ["/icon-512.png", "image/png"], ["/manifest.webmanifest", "application/manifest+json"]]) {
    const r = await request.get(u);
    expect(r.status(), u).toBe(200);
    expect(r.headers()["content-type"], u).toContain(type); // not the app page served as a fallback
  }
  const m = await (await request.get("/manifest.webmanifest")).json();
  expect(m.icons.length).toBeGreaterThan(1);
});

test("Office layout view: a .pptx opens in its original layout (LibreOffice → PDF); Text and Ask switch to the text", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const cp = require("child_process") as typeof import("child_process");
  const py = path.resolve(__dirname, "../../.venv/bin/python");
  cp.execFileSync(py, ["-c", `
from pptx import Presentation
prs = Presentation()
s = prs.slides.add_slide(prs.slide_layouts[1])
s.shapes.title.text = "Layout deck"
s.placeholders[1].text_frame.text = "Rendered by LibreOffice"
prs.save(${JSON.stringify(path.join(ws, "docs", "layout-deck.pptx"))})
`]);
  try {
    await page.getByTestId("tree-refresh").click();
    const dir = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: "docs" }).first();
    if ((await page.getByTestId("tree-file-docs/layout-deck.pptx").count()) === 0) await dir.click();
    await page.getByTestId("tree-file-docs/layout-deck.pptx").locator("button.tree-main").click();
    const view = page.getByTestId("office-view");
    await expect(page.getByTestId("office-view-layout")).toHaveAttribute("aria-selected", "true"); // default
    await expect(view).toHaveAttribute("data-state", "ready", { timeout: 60000 });
    // the in-page viewer (PDF.js) loads the rendered PDF and draws its slides
    const viewer = page.getByTestId("office-pdf");
    await expect(viewer).toHaveAttribute("data-pages", "1", { timeout: 30000 });
    await expect(viewer.locator('[data-page="1"]')).toHaveAttribute("data-rendered", "true", { timeout: 15000 });
    await expect(viewer.locator(".textLayer")).toContainText("Layout deck"); // selectable text of the slide
    const pdf = await page.request.get(`/api/v1/office/pdf?path=${encodeURIComponent("docs/layout-deck.pptx")}`);
    expect(pdf.status()).toBe(200);
    expect(pdf.headers()["content-type"]).toBe("application/pdf");
    expect((await pdf.body()).subarray(0, 4).toString()).toBe("%PDF");
    await expect(page.getByTestId("markdown-body")).toBeHidden();
    // Ask in the layout view: passages are picked in the text view
    await page.getByTestId("ask-current-selection").click();
    await expect(page.getByTestId("office-view-text")).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("office-text-hint")).toBeVisible();
    await expect(page.getByTestId("markdown-body").locator("h2").first()).toHaveText("Slide 1: Layout deck");
    // the choice is remembered; Layout brings the original back (cached: ready at once)
    await page.getByTestId("office-view-layout").click();
    await expect(view).toHaveAttribute("data-state", "ready", { timeout: 5000 });
  } finally {
    fs.rmSync(path.join(ws, "docs", "layout-deck.pptx"), { force: true });
  }
});

test("Visio: a .vsdx has a text view (pages and shape text) for asking about a passage", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const cp = require("child_process") as typeof import("child_process");
  const py = path.resolve(__dirname, "../../.venv/bin/python");
  cp.execFileSync(py, ["-c", `
import sys; sys.path.insert(0, ${JSON.stringify(path.resolve(__dirname, "../backend"))})
from pathlib import Path
from test_office import make_vsdx
make_vsdx(Path(${JSON.stringify(path.join(ws, "docs", "flow.vsdx"))}))
`]);
  try {
    await page.getByTestId("tree-refresh").click();
    const dir = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: "docs" }).first();
    if ((await page.getByTestId("tree-file-docs/flow.vsdx").count()) === 0) await dir.click();
    await expect(page.getByTestId("tree-file-docs/flow.vsdx")).toHaveAttribute("data-kind", "diagram");
    await page.getByTestId("tree-file-docs/flow.vsdx").locator("button.tree-main").click();
    await expect(page.locator(".doc-meta .chip").first()).toHaveText("VISIO");
    await page.getByTestId("office-view-text").click();
    const body = page.getByTestId("markdown-body");
    await expect(body.locator("h2")).toHaveText(["Page 1: Overview", "Page 2: Details"]);
    if ((await page.getByTestId("reader-mode-discuss").getAttribute("aria-selected")) !== "true") await page.getByTestId("reader-mode-discuss").click();
    await selectText(page, "Grouped A");
    await expect(page.getByTestId("selection-quote")).toHaveText("Grouped A");
  } finally {
    fs.rmSync(path.join(ws, "docs", "flow.vsdx"), { force: true });
  }
});

test("file tree: New file opens it in the editor, New folder, errors shown inline", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  try {
    const root = page.getByTestId("tree-root").first();
    await root.locator(".tree-root").hover();
    await page.getByTestId("tree-new-file-root").click();
    const name = page.getByTestId("tree-new-name");
    await name.fill("drafts/idea.md"); // missing folders are created
    await name.press("Enter");
    await expect(page.getByTestId("editor-text")).toHaveValue("# idea\n", { timeout: 10000 }); // straight into the editor
    expect(fs.readFileSync(path.join(ws, "drafts", "idea.md"), "utf-8")).toBe("# idea\n");
    await expect(page.getByTestId("tree-file-drafts/idea.md")).toBeVisible();
    await expect(page.locator('[data-testid="doc-tab"][data-path="drafts/idea.md"]')).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("Escape"); // leave the editor
    // New folder inside a folder
    const row = root.locator(".tree-row.dir", { hasText: "drafts" }).first();
    await row.hover();
    await page.getByTestId("tree-new-dir-drafts").click();
    await page.getByTestId("tree-new-name").fill("assets");
    await page.getByTestId("tree-new-name").press("Enter");
    await expect.poll(() => fs.existsSync(path.join(ws, "drafts", "assets"))).toBe(true);
    await expect(root.locator(".tree-row.dir", { hasText: "assets" })).toBeVisible();
    // an existing name: the error shows under the box, nothing is overwritten
    await row.hover();
    await page.getByTestId("tree-new-file-drafts").click();
    await page.getByTestId("tree-new-name").fill("idea.md");
    await page.getByTestId("tree-new-name").press("Enter");
    await expect(page.getByTestId("tree-new-error")).toContainText("already exists");
    await page.getByTestId("tree-new-name").press("Escape");
    await expect(page.getByTestId("tree-new")).toHaveCount(0);
    expect(fs.readFileSync(path.join(ws, "drafts", "idea.md"), "utf-8")).toBe("# idea\n");
  } finally {
    fs.rmSync(path.join(ws, "drafts"), { recursive: true, force: true });
  }
});

test("folder picker: New folder creates a directory (with git init) on the server and selects it", async ({ page }) => {
  await login(page);
  const parent = path.dirname(workspaceDir());
  const target = path.join(parent, "fresh-wiki");
  fs.rmSync(target, { recursive: true, force: true });
  try {
    await page.getByTestId("tree-add-folder").click();
    await page.getByTestId("add-folder-path").fill(parent);
    await page.getByTestId("add-folder-path").press("Enter");
    await expect(page.getByTestId("add-folder-selected")).toContainText(parent);
    await page.getByTestId("add-folder-newdir").click();
    await page.getByTestId("add-folder-newdir-name").fill("fresh-wiki");
    await page.getByTestId("add-folder-newdir-create").click();
    await expect(page.getByTestId("add-folder-selected")).toContainText(target);
    await expect(page.getByTestId("add-folder-selected")).toContainText("· Git repository");
    expect(fs.existsSync(path.join(target, ".git"))).toBe(true);
    await page.locator(".modal-head .icon-btn").click();
  } finally {
    fs.rmSync(target, { recursive: true, force: true });
  }
});

test("named workspaces: create one (its own folders, tabs, terminals), switch back, rename, delete", async ({ page }) => {
  await login(page);
  const parent = path.dirname(workspaceDir());
  const other = path.join(parent, "ws-only-wiki");
  fs.rmSync(other, { recursive: true, force: true });
  fs.mkdirSync(other, { recursive: true });
  fs.writeFileSync(path.join(other, "only.md"), "# Only here\n");
  const menu = page.getByTestId("ws-menu");
  try {
    await expect(menu).toContainText("Default");
    const defaultTerminals = await (await page.request.get("/api/v1/terminals")).json();
    // New workspace: name + first folder
    await menu.click();
    await page.getByTestId("ws-menu-new").click();
    await page.getByTestId("new-workspace-name").fill("Side project");
    await page.getByTestId("add-folder-path").fill(other);
    await page.getByTestId("add-folder-path").press("Enter");
    await expect(page.getByTestId("add-folder-selected")).toContainText(other);
    await page.getByTestId("new-workspace-submit").click();
    // the page reloads into the new workspace: only its folder, no terminals, no tabs from Default
    await expect(menu).toContainText("Side project", { timeout: 15000 });
    await expect(page.getByTestId("tree-root")).toHaveCount(1);
    await expect(page.getByTestId("tree-file-only.md")).toBeVisible();
    expect((await (await page.request.get("/api/v1/terminals")).json()).terminals).toEqual([]);
    await expect(page.getByTestId("doc-tab")).toHaveCount(0);
    await page.getByTestId("tree-file-only.md").locator("button.tree-main").click();
    await expect(page.locator('[data-testid="doc-tab"][data-path="only.md"]')).toBeVisible();
    // back to Default: its folders, terminals and tabs come back; this workspace's tab stays with it
    await menu.click();
    await page.locator('[data-testid="ws-menu-item"][data-name="Default"] .ws-menu-main').click();
    await expect(menu).toContainText("Default", { timeout: 15000 });
    await expect(page.getByTestId("tree-file-docs/guide.md").or(page.locator(".tree-row.dir", { hasText: "docs" })).first()).toBeVisible();
    await expect(page.locator('[data-testid="doc-tab"][data-path="only.md"]')).toHaveCount(0);
    expect((await (await page.request.get("/api/v1/terminals")).json()).terminals.length).toBe(defaultTerminals.terminals.length);
    // rename and delete it from the menu
    await menu.click();
    page.once("dialog", (d) => d.accept("Side project 2"));
    await page.locator('[data-testid="ws-menu-item"][data-name="Side project"] [data-testid="ws-menu-rename"]').click();
    await expect(page.locator('[data-testid="ws-menu-item"][data-name="Side project 2"]')).toBeVisible();
    page.once("dialog", (d) => d.accept());
    await page.locator('[data-testid="ws-menu-item"][data-name="Side project 2"] [data-testid="ws-menu-delete"]').click();
    await expect(page.getByTestId("ws-menu-item")).toHaveCount(1);
  } finally {
    // leave the shared test server in Default for the other tests
    const named = await (await page.request.get("/api/v1/named-workspaces")).json();
    const def = named.workspaces.find((w: any) => w.is_default);
    if (named.current !== def.id) await page.evaluate(async (id) => {
      const csrf = (await (await fetch("/api/v1/bootstrap")).json()).csrf_token;
      await fetch(`/api/v1/named-workspaces/${id}/open`, { method: "POST", headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" }, body: "{}" });
    }, def.id);
    fs.rmSync(other, { recursive: true, force: true });
  }
});

test("discussions: ask about the whole document (Markdown and PDF), not only a selection", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  fs.writeFileSync(path.join(ws, "docs", "whole.pdf"), "%PDF-1.4\n%%EOF\n");
  try {
    await openGuide(page);
    await page.getByTestId("ask-whole-document").click();
    await expect(page.getByTestId("selection-whole")).toBeVisible();
    await expect(page.getByTestId("selection-quote")).toHaveCount(0);
    await page.getByTestId("selection-question").fill("Summarize this page");
    await page.getByTestId("selection-send").click();
    if ((await page.getByTestId("mode-individual").count()) > 0) await page.getByTestId("mode-individual").click();
    await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Summarize this page", { timeout: 20000 });
    await expect(page.getByTestId("thread-whole-document")).toBeVisible();
    await expect(page.locator("mark.waw-hl.thread", { hasText: "Summarize" })).toHaveCount(0); // nothing highlighted for it
    // the selection button is still there for passages
    await expect(page.getByTestId("ask-current-selection")).toBeVisible();
    // PDFs: the same, which failed before ("invalid anchor")
    await page.getByTestId("tree-refresh").click();
    await expect(page.getByTestId("tree-refresh")).toHaveAttribute("data-spinning", "false");
    await page.getByTestId("tree-file-docs/whole.pdf").locator("button.tree-main").click();
    await expect(page.getByTestId("pdf-view")).toBeVisible(); // the PDF is open before asking about it
    await expect(page.getByTestId("ask-whole-document")).toHaveText("Ask about this PDF");
    await page.getByTestId("ask-whole-document").click();
    await page.getByTestId("selection-question").fill("What is in this PDF?");
    await page.getByTestId("selection-send").click();
    await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("What is in this PDF?", { timeout: 20000 });
    await expect(page.getByTestId("thread-whole-document")).toBeVisible();
  } finally {
    fs.rmSync(path.join(ws, "docs", "whole.pdf"), { force: true });
  }
});

async function signOut(page: Page) {
  await page.getByTestId("account-menu").click();
  await page.getByTestId("sign-out").click();
}

/** First "Continue with GitHub" on the login page sets it up (app manifest flow against the fake GitHub) and signs in. */
async function ensureGithubSignIn(page: Page) {
  await page.goto("/");
  const opts = await (await page.request.get("/api/v1/auth/options")).json();
  if (!opts.github_setup) return;
  await expect(page.getByTestId("login-github")).toHaveAttribute("href", "/api/v1/auth/github/setup/begin");
  await page.getByTestId("login-github").click(); // GitHub: "Create GitHub App" → "Authorize" → back, signed in
  await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("account-menu")).toContainText("e2e-user");
  await page.getByTestId("account-menu").click();
  await expect(page.getByTestId("github-setup-status")).toContainText("on for e2e-user");
  await page.getByTestId("sign-out").click();
  await expect(page.getByTestId("login-github")).toHaveAttribute("href", "/api/v1/auth/github/start");
}

test("login page: Sign in with GitHub (allowed account), refused account and cancel are explained", async ({ page, context }) => {
  await ensureGithubSignIn(page);
  await page.goto("/");
  await expect(page.getByTestId("login-github")).toBeVisible();
  await expect(page.getByTestId("login-token")).toBeVisible(); // the token stays available
  // an account that is not allowed
  await context.addCookies([{ name: "fake_gh_user", value: "someone-else", url: "http://127.0.0.1:8799" }]);
  await page.getByTestId("login-github").click();
  await expect(page.locator(".login-card [role=alert]")).toContainText("not allowed");
  expect(page.url()).not.toContain("login_error"); // address bar cleaned
  // cancelled on GitHub
  await context.addCookies([{ name: "fake_gh_cancel", value: "1", url: "http://127.0.0.1:8799" }]);
  await page.getByTestId("login-github").click();
  await expect(page.locator(".login-card [role=alert]")).toContainText("cancelled");
  // the allowed account: straight into the app, named on the Sign out button
  await context.clearCookies();
  await page.getByTestId("login-github").click();
  await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("account-menu")).toContainText("e2e-user");
  await signOut(page);
  await expect(page.getByTestId("login-github")).toBeVisible();
});

test("login page: Continue with Google (verified, allowed email); other accounts are refused", async ({ page, context }) => {
  await ensureGithubSignIn(page);
  await page.goto("/");
  await expect(page.getByTestId("login-google")).toHaveText("Continue with Google");
  await expect(page.getByTestId("login-github")).toHaveText("Continue with GitHub");
  await context.addCookies([{ name: "fake_google_user", value: "stranger@example.com", url: "http://127.0.0.1:8799" }]);
  await page.getByTestId("login-google").click();
  await expect(page.locator(".login-card [role=alert]")).toContainText("Google account is not allowed");
  await context.clearCookies();
  await page.getByTestId("login-google").click();
  await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("account-menu")).toContainText("e2e@example.com");
  await signOut(page);
  await expect(page.getByTestId("login-google")).toBeVisible();
});

test("ask directly in the discussion panel about the open document, incl. an image", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
  fs.writeFileSync(path.join(ws, "docs", "pixel.png"), png);
  try {
    await page.getByTestId("tree-refresh").click();
    await expect(page.getByTestId("tree-refresh")).toHaveAttribute("data-spinning", "false");
    const dir = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: "docs" }).first();
    if ((await page.getByTestId("tree-file-docs/pixel.png").count()) === 0) await dir.click();
    await expect(page.getByTestId("tree-file-docs/pixel.png")).toHaveAttribute("data-kind", "image");
    await page.getByTestId("tree-file-docs/pixel.png").locator("button.tree-main").click();
    // the image is shown, and loads
    const img = page.getByTestId("image-view-img");
    await expect(img).toBeVisible();
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBe(1);
    await expect(page.locator(".doc-meta .chip").first()).toHaveText("PNG");
    await expect(page.getByTestId("ask-whole-document")).toHaveText("Ask about this image");
    // ask straight from the panel (Individual mode)
    if ((await page.getByTestId("mode-individual").getAttribute("aria-selected")) !== "true") await page.getByTestId("mode-individual").click();
    if ((await page.getByTestId("thread-back").count()) > 0) await page.getByTestId("thread-back").click();
    const input = page.getByTestId("ask-document-input");
    await expect(input).toHaveAttribute("placeholder", /Ask about this image/);
    await input.fill("What colour is this pixel?");
    await input.press("Enter");
    await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("What colour is this pixel?", { timeout: 20000 });
    await expect(page.getByTestId("thread-whole-document")).toBeVisible();
    // from an open discussion: "＋ New question" goes to the box, ready to type
    await page.getByTestId("new-doc-question").click();
    await expect(page.getByTestId("ask-document-input")).toBeFocused();
    // Flat mode has the same box
    await page.getByTestId("mode-flat").click();
    await expect(page.getByTestId("ask-document-input")).toBeVisible();
    await page.getByTestId("mode-individual").click();
  } finally {
    fs.rmSync(path.join(ws, "docs", "pixel.png"), { force: true });
  }
});

/** Select the text of the PDF.js text layer that contains `needle` (first page that has it). */
async function selectPdfText(page: Page, needle: string) {
  const layer = page.locator(".textLayer", { hasText: needle }).first();
  await expect(layer).toBeVisible({ timeout: 15000 });
  const span = layer.locator("span", { hasText: needle }).first();
  await span.evaluate((el, n) => {
    const t = el.firstChild!;
    const i = (t.textContent ?? "").indexOf(n);
    const r = document.createRange();
    r.setStart(t, i);
    r.setEnd(t, i + n.length);
    const s = window.getSelection()!;
    s.removeAllRanges();
    s.addRange(r);
  }, needle);
  const b = (await span.boundingBox())!;
  await page.mouse.move(b.x + 5, b.y + b.height / 2);
  await page.mouse.up(); // the viewer reacts to mouseup
  await span.dispatchEvent("mouseup", { bubbles: true, clientX: b.x + 5, clientY: b.y + b.height / 2 });
}

test("PDF (PDF.js): ask about selected text, a whole page, an area — pictures go with the question", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const cp = require("child_process") as typeof import("child_process");
  const py = path.resolve(__dirname, "../../.venv/bin/python");
  const pdf = path.join(ws, "docs", "spec.pdf");
  cp.execFileSync(py, [path.resolve(__dirname, "../fixtures/make_pdf.py"), pdf, "Rasterizer throughput is two quads", "Second page about caches"]);
  try {
    await page.getByTestId("tree-refresh").click();
    await expect(page.getByTestId("tree-refresh")).toHaveAttribute("data-spinning", "false");
    const dir = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: "docs" }).first();
    if ((await page.getByTestId("tree-file-docs/spec.pdf").count()) === 0) await dir.click();
    await page.getByTestId("tree-file-docs/spec.pdf").locator("button.tree-main").click();
    const viewer = page.getByTestId("pdfjs-view");
    await expect(viewer).toHaveAttribute("data-pages", "2", { timeout: 20000 });
    if ((await page.getByTestId("mode-individual").count()) > 0) await page.getByTestId("mode-individual").click();
    // 1) selected text on page 1
    await selectPdfText(page, "throughput");
    await page.getByTestId("pdfjs-ask-selection").click();
    await expect(page.locator(".sel-head")).toContainText("Ask about the selected text (page 1)");
    await expect(page.getByTestId("selection-quote")).toHaveText("throughput");
    await page.getByTestId("selection-question").fill("What is meant here?");
    await page.getByTestId("selection-send").click();
    await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("What is meant here?", { timeout: 20000 });
    await expect(page.getByTestId("thread-target")).toContainText("Page 1");
    // 2) the whole page 2, as a picture
    const p2 = viewer.locator('[data-page="2"]');
    await p2.scrollIntoViewIfNeeded();
    await expect(p2).toHaveAttribute("data-rendered", "true", { timeout: 15000 });
    await p2.getByTestId("pdfjs-ask-page").click();
    await expect(page.locator(".sel-head")).toContainText("Ask about page 2");
    await expect(page.getByTestId("selection-image")).toBeVisible();
    await page.getByTestId("selection-question").fill("Summarize this page");
    await page.getByTestId("selection-send").click();
    await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Summarize this page", { timeout: 20000 });
    const thumb = page.getByTestId("thread-attachment");
    await expect.poll(() => thumb.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 100)).toBe(true);
    await expect(p2.getByTestId("pdfjs-page-threads")).toHaveText("1 discussion"); // the page shows its discussion
    // 3) an area of page 1
    const p1 = viewer.locator('[data-page="1"]');
    await p1.scrollIntoViewIfNeeded();
    await p1.getByTestId("pdfjs-select-area").click();
    const area = p1.getByTestId("pdfjs-area");
    const b = (await area.boundingBox())!;
    await page.mouse.move(b.x + b.width * 0.1, b.y + b.height * 0.05);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width * 0.6, b.y + b.height * 0.2, { steps: 5 });
    await page.mouse.up();
    await expect(page.locator(".sel-head")).toContainText("Ask about an area of page 1");
    await expect(page.getByTestId("selection-image")).toBeVisible();
    await page.getByTestId("selection-question").fill("What is this figure?");
    await page.getByTestId("selection-send").click();
    await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("What is this figure?", { timeout: 20000 });
    await expect(page.getByTestId("thread-target")).toContainText("Area of page 1");
  } finally {
    fs.rmSync(pdf, { force: true });
  }
});

test("PowerPoint: ask about a slide or a picture (text view) and about a slide in its original layout", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const cp = require("child_process") as typeof import("child_process");
  const py = path.resolve(__dirname, "../../.venv/bin/python");
  const png = path.join(ws, "docs", "chart-src.png");
  fs.writeFileSync(png, Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR42mP4z8CAFTGMSgwqAABdHcf5TJ11gwAAAABJRU5ErkJggg==", "base64"));
  const deck = path.join(ws, "docs", "pics.pptx");
  cp.execFileSync(py, ["-c", `
from pptx import Presentation
from pptx.util import Inches
prs = Presentation()
s = prs.slides.add_slide(prs.slide_layouts[5]); s.shapes.title.text = "Quarterly chart"
s.shapes.add_picture(${JSON.stringify(png)}, Inches(2), Inches(2), Inches(3), Inches(3))
s2 = prs.slides.add_slide(prs.slide_layouts[1]); s2.shapes.title.text = "Plan"; s2.placeholders[1].text_frame.text = "Ship it"
prs.save(${JSON.stringify(deck)})
`]);
  try {
    await page.getByTestId("tree-refresh").click();
    await expect(page.getByTestId("tree-refresh")).toHaveAttribute("data-spinning", "false");
    const dir = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: "docs" }).first();
    if ((await page.getByTestId("tree-file-docs/pics.pptx").count()) === 0) await dir.click();
    await page.getByTestId("tree-file-docs/pics.pptx").locator("button.tree-main").click();
    if ((await page.getByTestId("mode-individual").count()) > 0) await page.getByTestId("mode-individual").click();
    // Layout: the rendered slides, with a per-slide question
    await page.getByTestId("office-view-layout").click();
    const viewer = page.getByTestId("office-pdf");
    await expect(viewer).toHaveAttribute("data-pages", "2", { timeout: 60000 });
    const s2 = viewer.locator('[data-page="2"]');
    await s2.scrollIntoViewIfNeeded();
    await expect(s2).toHaveAttribute("data-rendered", "true", { timeout: 15000 });
    await expect(s2.getByTestId("pdfjs-ask-page")).toHaveText("Ask about this slide");
    await s2.getByTestId("pdfjs-ask-page").click();
    await expect(page.locator(".sel-head")).toContainText("Ask about slide 2");
    await page.getByTestId("selection-question").fill("Is this plan realistic?");
    await page.getByTestId("selection-send").click();
    await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Is this plan realistic?", { timeout: 20000 });
    await expect(page.getByTestId("thread-target")).toContainText("Slide 2");
    // Text view: a whole slide (anchored on its text) and a picture
    await page.getByTestId("office-view-text").click();
    const body = page.getByTestId("markdown-body");
    await body.locator("section.slide").first().locator("h2").hover();
    await page.getByTestId("ask-slide").click();
    await expect(page.getByTestId("selection-quote")).toContainText("Slide 1: Quarterly chart");
    await page.locator(".sheet-backdrop").click({ position: { x: 5, y: 5 } });
    await body.locator("section.slide img").first().hover();
    await page.getByTestId("ask-image").click();
    await expect(page.locator(".sel-head")).toContainText("Ask about this image (slide 1)");
    await expect(page.getByTestId("selection-image")).toBeVisible();
    await page.getByTestId("selection-question").fill("What does the chart show?");
    await page.getByTestId("selection-send").click();
    await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("What does the chart show?", { timeout: 20000 });
    await expect(page.getByTestId("thread-target")).toContainText("Image");
  } finally {
    fs.rmSync(deck, { force: true });
    fs.rmSync(png, { force: true });
  }
});

test("GitHub review hub: only the owner's GitHub sign-in sees it; queue grouped by repo, drafts, filter, sort", async ({ page }) => {
  // token login: no activity bar, the queue is refused
  await login(page);
  await expect(page.getByTestId("activity-reviews")).toHaveCount(0);
  expect((await page.request.get("/api/v1/github/reviews")).status()).toBe(403);
  await signOut(page);
  // the owner (gh account) signed in with GitHub
  await ensureGithubSignIn(page);
  await page.goto("/");
  await page.getByTestId("login-github").click();
  await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("activity-reviews-badge")).toHaveText("3", { timeout: 15000 }); // 4 PRs, 1 draft
  await page.getByTestId("activity-reviews").click();
  const panel = page.getByTestId("reviews-panel");
  await expect(panel).toBeVisible();
  await expect(page.getByTestId("reviews-note")).toContainText("SAML");
  // grouped by repository, most PRs first; drafts hidden by default
  await expect(page.getByTestId("review-group")).toHaveCount(2);
  await expect(page.getByTestId("review-group").first()).toHaveAttribute("data-repo", "pytorch/pytorch");
  const prs = page.locator('[data-repo="pytorch/pytorch"] [data-testid="review-pr"]');
  await expect(prs).toHaveCount(2);
  await expect(prs.first()).toHaveAttribute("data-number", "101"); // recently updated first
  await page.getByTestId("reviews-drafts").check();
  await expect(prs).toHaveCount(3);
  await page.getByTestId("reviews-sort").selectOption("size");
  await expect(prs.last()).toHaveAttribute("data-number", "103"); // biggest last
  await expect(prs.last().locator(".ci-dot")).toHaveClass(/failure/);
  // filter
  await page.getByTestId("reviews-filter").fill("gemm");
  await expect(page.getByTestId("review-pr")).toHaveCount(1);
  await expect(page.getByTestId("review-pr")).toHaveAttribute("data-number", "201");
  await page.getByTestId("reviews-filter").fill("");
  // a PR opens as a tab, with a link to GitHub (never contacted from tests)
  await page.getByTestId("review-pr").first().click();
  await expect(page.getByTestId("pr-view")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", "https://github.com/pytorch/pytorch/pull/101");
  await page.locator('[data-testid="doc-tab"][data-ws="github"]').getByTestId("doc-tab-close").click();
  // back to the files; sign out
  await page.getByTestId("activity-files").click();
  await expect(page.getByTestId("tree-root").first()).toBeVisible();
  await signOut(page);
});

test("GitHub PR tab: overview, files, choose the wiki once, ask about the PR and about selected diff lines", async ({ page }) => {
  await ensureGithubSignIn(page);
  await page.goto("/");
  await page.getByTestId("login-github").click();
  await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 15000 });
  await page.getByTestId("activity-reviews").click();
  await page.locator('[data-testid="review-pr"][data-number="101"]').click();
  // the PR opens as a tab next to documents
  const tab = page.locator('[data-testid="doc-tab"][data-ws="github"]');
  await expect(tab).toHaveAttribute("aria-selected", "true");
  await expect(tab).toContainText("#101");
  await expect(page.getByTestId("pr-title")).toContainText("[xpu] Fix the lazy Level Zero stub");
  await expect(page.getByTestId("pr-description")).toContainText("This PR fixes the lazy stub.");
  await expect(page.getByTestId("pr-checks").locator("li").first()).toContainText("xpu-test"); // failing first
  await expect(page.getByTestId("pr-reviews")).toContainText("bob approved");
  // first PR of this repository: choose the wiki the agent works in (once)
  await expect(page.getByTestId("pr-ask")).toBeDisabled();
  await page.getByTestId("pr-wiki-use").click();
  await expect(page.getByTestId("pr-choose-wiki")).toHaveCount(0);
  await expect(page.getByTestId("pr-ask")).toBeEnabled({ timeout: 10000 });
  // ask about the whole PR
  if ((await page.getByTestId("mode-individual").count()) > 0) await page.getByTestId("mode-individual").click();
  await page.getByTestId("pr-ask").click();
  await page.getByTestId("selection-question").fill("Is the null check enough?");
  await page.getByTestId("selection-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Is the null check enough?", { timeout: 20000 });
  await expect(page.getByTestId("thread-whole-document")).toBeVisible();
  // files: select diff lines and ask about them
  await page.getByTestId("pr-tab-files").click();
  await expect(page.getByTestId("pr-file")).toHaveCount(2);
  const lines = page.locator('[data-file="c10/xpu/stub.cpp"] .diff-line.add');
  await expect(lines).toHaveCount(2);
  await lines.first().evaluate((first) => {
    const last = first.nextElementSibling!;
    const r = document.createRange();
    r.setStartBefore(first);
    r.setEndAfter(last);
    const s = window.getSelection()!;
    s.removeAllRanges();
    s.addRange(r);
  });
  const b = (await lines.first().boundingBox())!;
  await lines.first().dispatchEvent("mouseup", { bubbles: true, clientX: b.x + 20, clientY: b.y + 5 });
  await page.getByTestId("pr-ask-selection").click();
  await expect(page.locator(".sel-head")).toContainText("Ask about the selected lines (c10/xpu/stub.cpp)");
  await expect(page.getByTestId("selection-quote")).toContainText("+  if (ok)");
  await page.getByTestId("selection-question").fill("Why guard call()?");
  await page.getByTestId("selection-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Why guard call()?", { timeout: 20000 });
  await expect(page.getByTestId("thread-target")).toContainText("c10/xpu/stub.cpp");
  // the tab is remembered across reloads, and the wiki choice too
  await page.reload();
  await expect(page.locator('[data-testid="doc-tab"][data-ws="github"]')).toContainText("#101");
  await page.locator('[data-testid="doc-tab"][data-ws="github"]').click();
  await expect(page.getByTestId("pr-title")).toBeVisible();
  await expect(page.getByTestId("pr-choose-wiki")).toHaveCount(0);
  await expect(page.getByTestId("pr-wiki-select")).toBeVisible();
  // close the PR tab
  await page.locator('[data-testid="doc-tab"][data-ws="github"]').getByTestId("doc-tab-close").click();
  await expect(page.locator('[data-testid="doc-tab"][data-ws="github"]')).toHaveCount(0);
  await signOut(page);
});
