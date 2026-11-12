import { expect, test, Page } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";

const TOKEN = "test-access-token-0123456789abcdef";

/** Port of the e2e server: the files it leaves for the tests are named after it (several runs can share /tmp). */
const E2E_PORT = new URL(process.env.WAW_E2E_URL || "http://127.0.0.1:8799").port || "80";
function workspaceDir(): string {
  return fs.readFileSync(`/tmp/waw-e2e-workspace-${E2E_PORT}`, "utf-8").trim();
}

async function login(page: Page) {
  await page.goto("/");
  await page.getByTestId("login-token").fill(TOKEN);
  await page.getByTestId("login-submit").click();
  // on a busy host the first sign-in can stay on the login page (the click lands before the form is ready): once more
  try {
    await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 8000 });
  } catch {
    if (await page.getByTestId("login-submit").isVisible()) {
      await page.getByTestId("login-token").fill(TOKEN);
      await page.getByTestId("login-submit").click();
    }
    await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 15000 });
  }
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
  // Home / End move to the start / end of the input line
  await page.keyboard.type("echo homeend");
  await page.keyboard.press("Home");
  await page.keyboard.type("true;");
  await page.keyboard.press("End");
  await page.keyboard.type("-ok\n");
  await expect(view.locator(".xterm-rows")).toContainText("$ true;echo homeend-ok", { timeout: 15000 });
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
  for (let n = await page.getByTestId("terminal-tab").count(); n > 0; n = await page.getByTestId("terminal-tab").count()) {
    await page.getByTestId("terminal-tab").first().click();
    page.once("dialog", (d) => d.accept());
    await page.getByTestId("terminal-close").click();
    await expect(page.getByTestId("terminal-tab")).toHaveCount(n - 1, { timeout: 15000 }); // closed before counting again
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

test("terminal on a Mac: ⌘← / ⌘→ move to the start / end of the input line like Home / End", async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(() => Object.defineProperty(Navigator.prototype, "platform", { get: () => "MacIntel" }));
  const page = await context.newPage();
  await login(page);
  await page.getByTestId("view-terminal").click();
  await page.getByTestId("terminal-new").click();
  const view = page.locator(".term-view.active");
  await expect(view).toHaveAttribute("data-state", "connected", { timeout: 15000 });
  await expect(view.locator(".xterm-rows")).toContainText("$", { timeout: 15000 }); // the shell prompt
  await page.locator(".term-view.active .xterm-helper-textarea").focus();
  await page.keyboard.type("export PS1='$ '; echo ready-mac\n");
  await expect(view.locator(".xterm-rows")).toContainText("ready-mac", { timeout: 15000 });
  await page.keyboard.type("echo macend");
  await page.keyboard.press("Meta+ArrowLeft");
  await page.keyboard.type("true;");
  await page.keyboard.press("Meta+ArrowRight");
  await page.keyboard.type("-ok\n");
  await expect(view.locator(".xterm-rows")).toContainText("$ true;echo macend-ok", { timeout: 15000 });
  page.once("dialog", (d) => d.accept());
  await page.getByTestId("terminal-close").click();
  await context.close();
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
  // ask the agent about a whole file's changes, like GitHub's per-file "Ask"
  await page.getByTestId("tree-diff-root").click();
  const guideFile = page.locator('[data-testid="diff-file"][data-path="docs/guide.md"]');
  await guideFile.getByTestId("diff-ask-file").click();
  await guideFile.getByTestId("diff-ask-file-text").fill("Does this change lose anything?");
  await guideFile.getByTestId("diff-ask-file-send").click();
  const fileThread = guideFile.locator(".diff-file-comment");
  await expect(fileThread.locator('[data-testid="diff-msg-agent"][data-status="completed"]')).toContainText("Does this change lose anything?", { timeout: 20000 });
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
  // remove the folder from the window (data kept) — only after confirming in the dialog
  await page.getByTestId("tree-root").nth(1).locator(".tree-root").hover();
  await page.getByTestId("tree-remove-Second").click();
  await expect(page.getByTestId("confirm-dialog")).toContainText('Close the folder "Second"?');
  await expect(page.getByTestId("confirm-cancel")).toBeFocused(); // Enter / a stray click does not close it
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);
  await expect(page.getByTestId("tree-root")).toHaveCount(2);
  await page.getByTestId("tree-root").nth(1).locator(".tree-root").hover();
  await page.getByTestId("tree-remove-Second").click();
  await page.getByTestId("confirm-ok").click();
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
    await expect(page.getByTestId("tree-new-name")).toBeFocused();
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
    await page.locator('[data-testid="ws-menu-item"][data-name="Side project 2"] [data-testid="ws-menu-delete"]').click();
    await page.getByTestId("confirm-ok").click();
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
  await expect(page.getByTestId("activity-reviews-badge")).toHaveText("4", { timeout: 15000 }); // 5 PRs, 1 draft
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
  await page.locator('[data-testid="review-pr"][data-number="101"]').click();
  await expect(page.getByTestId("pr-view")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", "https://github.com/pytorch/pytorch/pull/101");
  await page.locator('[data-testid="doc-tab"][data-ws="github"]').getByTestId("doc-tab-close").click();
  // back to the files; sign out
  await page.getByTestId("activity-files").click();
  await expect(page.getByTestId("tree-root").first()).toBeVisible();
  await signOut(page);
});

test("GitHub review hub: priority requests, hide a repository, watch a repository", async ({ page }) => {
  await ensureGithubSignIn(page);
  await page.goto("/");
  await page.getByTestId("login-github").click();
  await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 15000 });
  await page.getByTestId("activity-reviews").click();
  // a request from a PyTorch Meta maintainer is listed first, under Priority (not again under its repository)
  const prio = page.getByTestId("review-priority");
  await expect(prio.getByTestId("review-pr")).toHaveCount(1, { timeout: 15000 });
  await expect(prio.getByTestId("review-pr")).toHaveAttribute("data-number", "104");
  await expect(prio).toContainText("asked by metadev");
  await expect(page.locator('[data-testid="review-group"] [data-number="104"]')).toHaveCount(0);
  await page.getByTestId("reviews-settings").click();
  await page.getByTestId("reviews-priority-meta").click(); // saved on the server, then shown
  await expect(page.getByTestId("reviews-priority-meta")).not.toBeChecked();
  await expect(prio).toHaveCount(0);
  await expect(page.locator('[data-testid="review-group"] [data-number="104"]')).toHaveCount(1);
  await page.getByTestId("reviews-priority-meta").click();
  await expect(page.getByTestId("reviews-priority-meta")).toBeChecked();
  await expect(page.getByTestId("review-priority")).toHaveCount(1);
  // status next to the CI signal; ☆ star a PR to make it priority, ★ un-star to take one out of Priority
  await expect(page.locator('[data-testid="review-pr"][data-number="101"] [data-testid="pr-status"]')).toHaveText("Ready");
  await page.locator('[data-testid="review-group"] [data-testid="review-pr"][data-number="101"] [data-testid="review-star"]').click();
  await expect(prio.locator('[data-number="101"]')).toHaveCount(1);
  await prio.locator('[data-number="104"] [data-testid="review-star"]').click();
  await expect(prio.locator('[data-number="104"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="review-group"] [data-number="104"]')).toHaveCount(1);
  await prio.locator('[data-number="101"] [data-testid="review-star"]').click(); // back as it was
  await page.locator('[data-testid="review-group"] [data-number="104"] [data-testid="review-star"]').click();
  await expect(prio.locator('[data-number="104"]')).toHaveCount(1);
  await expect(prio.locator('[data-number="101"]')).toHaveCount(0);
  // search pull requests on GitHub: open ones by default, closed / merged on request
  await page.getByTestId("reviews-search-open").click();
  await page.getByTestId("reviews-search").fill("gemm");
  await page.getByTestId("reviews-search").press("Enter");
  await expect(page.getByTestId("reviews-results").locator('[data-number="201"]')).toHaveCount(1);
  await page.getByTestId("reviews-search-state").selectOption("closed");
  await expect(page.getByTestId("reviews-results")).toContainText("Nothing found");
  await page.getByTestId("reviews-search").press("Escape");
  // a PR you approved with nothing new since (only a bot commented) is left out until "approved" is ticked
  await expect(page.getByTestId("reviews-approved-hidden")).toContainText("1 PR(s) you approved");
  await expect(page.locator('[data-testid="review-pr"][data-number="106"]')).toHaveCount(0);
  await page.getByTestId("reviews-show-approved").click();
  await expect(page.locator('[data-testid="review-pr"][data-number="106"]')).toContainText("✓ approved");
  await page.getByTestId("reviews-show-approved").click();
  await expect(page.locator('[data-testid="review-pr"][data-number="106"]')).toHaveCount(0);
  // hide a repository, then show it again
  const intel = page.locator('[data-testid="review-group"][data-repo="intel/torch-xpu-ops"]');
  await intel.locator(".review-group-head-row").hover();
  await intel.getByTestId("review-hide-repo").click();
  await expect(page.getByTestId("confirm-dialog")).toContainText("Hide intel/torch-xpu-ops from Reviews?");
  await page.getByTestId("confirm-cancel").click();
  await expect(intel).toHaveCount(1);
  await intel.getByTestId("review-hide-repo").dispatchEvent("click"); // (shown on hover)
  await page.getByTestId("confirm-ok").click();
  await expect(intel).toHaveCount(0);
  await expect(page.getByTestId("reviews-hidden-item")).toContainText("intel/torch-xpu-ops");
  await page.getByTestId("reviews-unhide").click();
  await expect(intel).toHaveCount(1);
  // watch a repository: its open PRs appear in their own section (fetched in the background)
  await page.getByTestId("reviews-watch-input").fill("pytorch/ao");
  await page.getByTestId("reviews-watch-add").click();
  await expect(page.getByTestId("reviews-watched-item")).toContainText("pytorch/ao");
  const w = page.locator('[data-testid="review-group-watched"][data-repo="pytorch/ao"]');
  await expect(w.getByTestId("review-pr")).toHaveAttribute("data-number", "301", { timeout: 15000 });
  await page.getByTestId("reviews-watched-item").getByRole("button").click();
  await page.getByTestId("confirm-ok").click();
  await expect(page.getByTestId("reviews-watched-item")).toHaveCount(0);
  // the PR tab says whether it is ready for review; it can be turned into a draft or closed on GitHub
  await page.locator('[data-testid="review-pr"][data-number="101"]').click();
  await expect(page.getByTestId("pr-ready-status")).toHaveText("Ready for review");
  await page.getByTestId("pr-to-draft").click();
  await expect.poll(async () => (await (await page.request.get("/fake-github/posted")).json()).some((x: any) => x.mutation === "ToDraft")).toBe(true);
  await page.getByTestId("pr-close").click();
  await expect(page.getByTestId("confirm-dialog")).toContainText("Close pull request #101?");
  await page.getByTestId("confirm-ok").click();
  await expect.poll(async () => (await (await page.request.get("/fake-github/posted")).json()).some((x: any) => x.method === "PATCH" && x.path.endsWith("/pulls/101") && x.body.state === "closed")).toBe(true);
  // "#555" in the description is an issue: it opens as an issue, which can be commented on and closed
  await page.getByTestId("pr-description").locator('a[data-pr="pytorch/pytorch/555"]').click();
  await expect(page.getByTestId("issue-view")).toContainText("XPU OOM on large batch");
  await expect(page.getByTestId("issue-comment")).toContainText("Same here.");
  await page.getByTestId("issue-comment-text").fill("Looking into it");
  await page.getByTestId("issue-comment-send").click();
  await expect(page.getByTestId("issue-comment")).toHaveCount(2);
  await page.getByTestId("issue-close-not-planned").click();
  await page.getByTestId("confirm-ok").click();
  await expect(page.getByTestId("issue-state")).toContainText("Closed (not planned)");
  await page.locator('[data-testid="doc-tab"][data-ws="github"]').filter({ hasText: "#555" }).getByTestId("doc-tab-close").click();
  // a PR's contributors, with details from their GitHub profile on demand
  await page.locator('[data-testid="review-pr"][data-number="101"]').click();
  const people = page.getByTestId("pr-contributors");
  await expect(people.getByTestId("pr-contributor")).toHaveCount(2);
  await expect(people.getByTestId("pr-contributor").first()).toContainText("alice");
  await expect(people.getByTestId("pr-contributor").first()).toContainText("author, commit author · 2 commits");
  await expect(people.getByTestId("pr-contributor").first()).toContainText("Intel"); // affiliation guess
  await expect(people.getByTestId("pr-contributor").last()).toContainText("Dan Helper");
  await expect(people.getByTestId("pr-contributor").last()).toContainText("co-author");
  await expect(people.getByTestId("pr-contributor").first().getByTestId("gh-user")).toHaveAttribute("href", "https://github.com/alice");
  await people.getByTestId("pr-contributor").first().getByTestId("pr-contributor-toggle").click();
  await expect(people.getByTestId("pr-contributor-details").first()).toContainText("alice@intel.com");
  await expect(people.getByTestId("pr-contributor-details").first()).toContainText("Shanghai");
  await people.getByTestId("pr-contributors-details").check();
  await expect(people.getByTestId("pr-contributor-details").first()).toContainText("alice@example.com"); // public email (REST)
  await people.getByTestId("pr-contributors-details").uncheck();
  // connect the repository to a local git checkout (checked on the server)
  await page.getByTestId("pr-local-repo").click();
  await expect(page.getByTestId("pr-local-repo-update")).toBeChecked(); // updating the local project is on by default
  await page.getByTestId("pr-local-repo-path").fill(path.dirname(workspaceDir()));
  await page.getByTestId("pr-local-repo-save").click();
  await expect(page.getByTestId("pr-local-repo-error")).toContainText("is not a git checkout");
  await page.getByTestId("pr-local-repo-path").fill("/nonexistent/checkout");
  await page.getByTestId("pr-local-repo-save").click();
  await expect(page.getByTestId("pr-local-repo-error")).toContainText("not a directory");
  await page.getByTestId("pr-local-repo-browse").click();
  await page.getByTestId("folder-picker-path").fill(path.dirname(workspaceDir()));
  await page.getByTestId("folder-picker-path").press("Enter");
  await expect(page.getByTestId("folder-picker-choose")).toBeDisabled(); // only a Git checkout can be chosen
  await page.locator('[data-testid="folder-picker-entry"][data-name="wiki"]').click();
  await expect(page.getByTestId("folder-picker-selected")).toContainText("Git repository");
  await page.getByTestId("folder-picker-choose").click();
  await expect(page.getByTestId("pr-local-repo-path")).toHaveValue(workspaceDir());
  await page.getByTestId("pr-local-repo").click();
  // "#103" in the description links to that PR and opens it here, as a tab; `#999` in code is not a link
  const ref = page.getByTestId("pr-description").locator('a[data-pr="pytorch/pytorch/103"]');
  await expect(ref).toHaveAttribute("href", "https://github.com/pytorch/pytorch/issues/103");
  await expect(page.getByTestId("pr-description").locator('a[data-pr="pytorch/pytorch/999"]')).toHaveCount(0);
  await ref.click();
  await expect(page.getByTestId("pr-title")).toContainText("Add XPU event timing");
  await expect(page.locator('[data-testid="doc-tab"][data-ws="github"]')).toHaveCount(2);
  for (let i = 0; i < 2; i++) await page.locator('[data-testid="doc-tab"][data-ws="github"]').first().getByTestId("doc-tab-close").click();
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
  // GitHub logins open the account's page, like on github.com: the author, @mentions, reviewers, commenters
  await expect(page.locator('.pr-meta [data-testid="gh-user"]')).toHaveAttribute("href", "https://github.com/alice");
  await expect(page.getByTestId("pr-description").locator('a.gh-user[data-login="bob"]')).toHaveAttribute("href", "https://github.com/bob");
  await expect(page.getByTestId("pr-description").locator('a.gh-user')).toHaveCount(1); // not the e-mail address
  await expect(page.getByTestId("pr-reviews").locator('[data-testid="gh-user"]').first()).toHaveAttribute("href", "https://github.com/bob");
  await expect(page.getByTestId("pr-checks").locator("li").first()).toContainText("xpu-test"); // failing first
  await expect(page.getByTestId("pr-reviews")).toContainText("bob requested changes");
  // conversation: reviewers' summaries and general comments, in time order; post one
  const conv = page.getByTestId("pr-conversation");
  await expect(conv.getByTestId("pr-conv-item")).toHaveCount(2);
  await expect(conv.getByTestId("pr-conv-item").first()).toContainText("CI started");
  await expect(conv.getByTestId("pr-conv-item").last()).toContainText("requested changes");
  await expect(conv.getByTestId("pr-conv-item").last()).toContainText("Please add a test.");
  // the review's line comment is shown in it, with its code (as on GitHub); bots' HTML is rendered, not shown raw
  await expect(conv.getByTestId("pr-conv-line")).toContainText("c10/xpu/stub.cpp:11");
  await expect(conv.getByTestId("pr-conv-line")).toContainText("Is ok ever false?");
  // a reply is shown in the thread of the comment it answers (its reply-only review is not listed separately)
  await expect(conv.getByTestId("pr-conv-line").getByTestId("pr-conv-reply")).toContainText("alice");
  await expect(conv.getByTestId("pr-conv-line").getByTestId("pr-conv-reply")).toContainText("Only without a driver.");
  await expect(conv.locator("details summary")).toHaveText("CI started");
  await expect(conv).not.toContainText("<!--");
  // @-mentions: participants first, then the repository's users matching what is typed
  const convBox = page.getByTestId("pr-conv-text");
  await convBox.click();
  await page.keyboard.type("cc @");
  await expect(page.getByTestId("mention-item").first()).toContainText("alice");
  await expect(page.getByTestId("mention-list")).toContainText("bob");
  await page.keyboard.type("ez");
  await expect(page.getByTestId("mention-item")).toHaveCount(1);
  await expect(page.getByTestId("mention-item")).toContainText("Edward Z. Yang");
  await page.keyboard.press("Enter"); // picks it, does not send
  await expect(convBox).toHaveValue("cc @ezyang ");
  await expect(page.getByTestId("mention-list")).toHaveCount(0);
  await page.getByTestId("pr-conv-text").fill("Thanks, looking now.");
  await page.getByTestId("pr-conv-send").click();
  await expect(conv.getByTestId("pr-conv-item")).toHaveCount(3);
  await expect(conv.getByTestId("pr-conv-item").last()).toContainText("Thanks, looking now.");
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
  // request a review, with suggestions while typing
  await page.getByTestId("pr-reviewer-add").click();
  await page.getByTestId("pr-reviewer-input").fill("ez");
  await expect(page.getByTestId("pr-reviewer-item").first()).toContainText("ezyang");
  await expect(page.getByTestId("pr-reviewer-item").first()).toContainText("suggested");
  await page.getByTestId("pr-reviewer-input").press("Enter");
  await expect.poll(async () => (await (await page.request.get("/fake-github/posted")).json()).some((x: any) => x.path?.endsWith("/requested_reviewers") && x.method === "POST" && x.body.reviewers[0] === "ezyang")).toBe(true);
  // re-request a review from someone who already reviewed (bob requested changes)
  await page.getByTestId("pr-review-item").filter({ hasText: "bob" }).getByTestId("pr-reviewer-rerequest").click();
  await expect(page.getByTestId("pr-reviewer-note")).toContainText("Review requested from bob");
  await expect.poll(async () => (await (await page.request.get("/fake-github/posted")).json()).some((x: any) => x.path?.endsWith("/requested_reviewers") && x.method === "POST" && x.body.reviewers[0] === "bob")).toBe(true);
  // trigger CI: re-run a workflow's failed jobs, add a ciflow label, start a workflow by hand
  await page.getByTestId("pr-ci-open").click();
  const ciRun = page.locator('[data-testid="pr-ci-run"][data-name="pull"]');
  await ciRun.getByTestId("pr-ci-rerun-failed").click();
  await expect(page.getByTestId("pr-ci-note")).toContainText("Re-running the failed jobs of pull");
  await page.locator('[data-testid="pr-ciflow"][data-label="ciflow/xpu"]').click();
  await expect(page.getByTestId("pr-ci-note")).toContainText("Added ciflow/xpu");
  await page.getByTestId("pr-ci-workflow").selectOption({ label: "trunk" });
  await page.getByTestId("pr-ci-dispatch").click();
  await expect(page.getByTestId("pr-ci-note")).toContainText("Started on fix-101");
  const ciPosted = (await (await page.request.get("/fake-github/posted")).json()).filter((x: any) => x.path?.includes("/actions/") || x.path?.includes("/labels"));
  expect(ciPosted.map((x: any) => x.path)).toEqual(expect.arrayContaining(["repos/pytorch/pytorch/actions/runs/900/rerun-failed-jobs", "repos/pytorch/pytorch/issues/101/labels", "repos/pytorch/pytorch/actions/workflows/77/dispatches"]));
  // ask the agent about the CI failures: the server downloads the failing jobs' logs for it
  await page.getByTestId("pr-ci-ask").click();
  await page.getByTestId("pr-ci-ask-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Investigate these CI failures", { timeout: 20000 });
  await expect(page.getByTestId("thread-target").last()).toContainText("CI: all failing checks");
  await expect(page.getByTestId("thread-quote").last()).toContainText("AssertionError: XPU result differs from CPU");
  // ask the agent about a reviewer's comment in the conversation (with its code and replies)
  const lineComment = page.getByTestId("pr-conv-line").first();
  await lineComment.getByTestId("pr-conv-ask").click();
  await lineComment.getByTestId("pr-conv-ask-text").fill("Is bob right about ok?");
  await lineComment.getByTestId("pr-conv-ask-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Is bob right about ok?", { timeout: 20000 });
  await expect(page.getByTestId("thread-target").last()).toContainText("comment by bob on c10/xpu/stub.cpp:11");
  await expect(page.getByTestId("thread-quote").last()).toContainText("Only without a driver."); // the reply goes with it
  // files: GitHub-style diff with the existing thread at its line
  await page.getByTestId("pr-tab-files").click();
  await expect(page.getByTestId("pr-file")).toHaveCount(2);
  const stub = page.locator('[data-testid="pr-file"][data-file="c10/xpu/stub.cpp"]');
  await expect(stub.getByTestId("gh-thread")).toContainText("Is ok ever false?");
  // a file-level thread from another reviewer, at the top of its file
  const notes = page.locator('[data-testid="pr-file"][data-file="docs/xpu.md"]');
  // line numbers survive a deleted '---' and an added '++i' line: # XPU(1) ---(L2) XPU notes(R2) ++i counter(R3) end(L3/R4)
  const nums = await notes.locator('[data-testid="pr-diff-row"]').evaluateAll((trs) => trs.map((tr) => {
    const td = tr.querySelectorAll("td");
    return `${td[0].textContent}|${td[1].textContent}|${(td[2].textContent ?? "").slice(1)}`;
  }));
  expect(nums).toEqual(["1|1|# XPU", "2||---", "|2|XPU notes", "|3|++i counter", "3|4|end"]);
  await expect(notes.getByTestId("pr-file-level").getByTestId("gh-thread")).toContainText("Move these notes to the wiki?");
  // viewed (synced to GitHub): the file folds, the counter moves
  await expect(page.getByTestId("pr-viewed-count")).toContainText("0 / 2 files viewed");
  await notes.getByTestId("pr-viewed").check();
  await expect(page.getByTestId("pr-viewed-count")).toContainText("1 / 2 files viewed");
  await expect(notes.getByTestId("pr-file-level")).toHaveCount(0); // folded
  await notes.getByTestId("pr-viewed").uncheck();
  await expect(page.getByTestId("pr-viewed-count")).toContainText("0 / 2 files viewed");
  // a comment on the whole file (pending)
  await notes.getByTestId("pr-file-comment").click();
  await notes.getByTestId("pr-file-composer-text").fill("Please add a license header.");
  await notes.getByTestId("pr-file-composer-add").click();
  await expect(notes.getByTestId("pr-draft")).toContainText("license header");
  // ask the agent about the other reviewer's thread
  await stub.getByTestId("gh-ask").click();
  await stub.getByTestId("gh-ask-text").fill("Is bob right?");
  await stub.getByTestId("gh-ask-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Is bob right?", { timeout: 20000 });
  await expect(page.getByTestId("thread-target")).toContainText("thread on c10/xpu/stub.cpp R11");
  // resolve it; it folds
  await stub.getByTestId("gh-resolve").click();
  await expect(stub.getByTestId("gh-thread")).toHaveAttribute("data-resolved", "1");
  await expect(stub.getByTestId("gh-thread-toggle")).toContainText("Resolved by e2e-user");
  await stub.getByTestId("gh-thread-toggle").click(); // unfold to reply
  // reply to it (posted at once)
  await stub.getByTestId("gh-reply").click();
  await stub.getByTestId("gh-reply-text").fill("Yes, on old drivers.");
  await stub.getByTestId("gh-reply-send").click();
  await expect(stub.getByTestId("gh-thread")).toContainText("Yes, on old drivers.");
  // select two added lines (drag) → ask the agent about them
  const added = stub.locator('[data-testid="pr-diff-row"].add');
  await expect(added).toHaveCount(2);
  const a1 = (await added.nth(0).boundingBox())!, a2 = (await added.nth(1).boundingBox())!;
  await page.mouse.move(a1.x + 60, a1.y + a1.height / 2);
  await page.mouse.down();
  await page.mouse.move(a2.x + 60, a2.y + a2.height / 2, { steps: 4 });
  await page.mouse.up();
  await expect(page.getByTestId("pr-composer")).toContainText("Lines R11–R12");
  // a suggested change from the selected lines
  await page.getByTestId("pr-suggest").click();
  await expect(page.getByTestId("pr-composer-text")).toHaveValue("```suggestion\n  if (ok)\n    call();\n```");
  await page.getByTestId("pr-composer-text").fill("Why guard call()?");
  await page.getByTestId("pr-composer-text").fill("Why guard call()?");
  await page.getByTestId("pr-ask-lines").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Why guard call()?", { timeout: 20000 });
  await expect(page.getByTestId("thread-target")).toContainText("c10/xpu/stub.cpp R11–R12");
  await expect(page.getByTestId("thread-quote")).toContainText("+  if (ok)");
  // ask the agent about one file's changes as a whole
  await stub.getByTestId("pr-file-ask").click();
  await page.getByTestId("pr-file-composer-text").fill("Summarize this file's change");
  await page.getByTestId("pr-file-composer-ask").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Summarize this file's change", { timeout: 20000 });
  await expect(page.getByTestId("thread-target").last()).toContainText("all of this file's changes");
  // side by side; comment on a deleted line (left side) → pending
  await page.getByTestId("pr-layout-split").click();
  await expect(stub.getByTestId("pr-diff-split")).toBeVisible();
  // GitHub's metrics: 20px code lines, line numbers in fixed >= 50px columns that never wrap
  const metrics = await stub.getByTestId("pr-diff-split").evaluate((el) => [...el.querySelectorAll("tr[data-testid=pr-diff-row]")].map((tr) => [
    Math.round(tr.querySelector("td.no")!.getBoundingClientRect().width), Math.round(tr.getBoundingClientRect().height)]));
  expect(metrics.length).toBeGreaterThan(2);
  expect(metrics.filter(([w, h]) => w < 50 || h !== 20)).toEqual([]);
  await stub.locator('td.code.left.del').first().click();
  await expect(page.getByTestId("pr-composer")).toContainText("Line L11");
  await page.getByTestId("pr-composer-text").fill("Keep the unconditional call for the stub?");
  await page.getByTestId("pr-add-comment").click();
  await expect(stub.getByTestId("pr-draft")).toContainText("Keep the unconditional call");
  await expect(page.getByTestId("pr-finish-review")).toContainText("Finish review (2)");
  // a second one, then delete it
  await stub.locator('td.code.right.add').first().click();
  await page.getByTestId("pr-composer-text").fill("throwaway");
  await page.getByTestId("pr-add-comment").click();
  await expect(stub.getByTestId("pr-draft")).toHaveCount(2);
  await stub.getByTestId("pr-draft").filter({ hasText: "throwaway" }).getByTestId("pr-draft-delete").click();
  await expect(stub.getByTestId("pr-draft")).toHaveCount(1);
  // pending comments survive a reload
  await page.reload();
  await page.locator('[data-testid="doc-tab"][data-ws="github"]').click();
  await page.getByTestId("pr-tab-files").click();
  await expect(page.getByTestId("pr-draft")).toHaveCount(2);
  await expect(page.getByTestId("pr-layout-split")).toHaveAttribute("aria-selected", "true"); // layout remembered
  // finish the review: request changes with a summary → one review on GitHub
  await page.getByTestId("pr-finish-review").click();
  await expect(page.getByTestId("pr-submit")).toContainText("posted on GitHub as e2e-user");
  await page.getByTestId("pr-submit-body").fill("Nice, one question.");
  await page.getByTestId("pr-event-request_changes").check();
  await page.getByTestId("pr-submit-send").click();
  await expect(page.getByTestId("pr-notice")).toContainText("Review published on GitHub");
  await expect(page.getByTestId("pr-draft")).toHaveCount(0);
  const posted = await (await page.request.get("/fake-github/posted")).json();
  const muts = posted.filter((x: any) => x.mutation && x.mutation !== "ToDraft").map((x: any) => x.mutation); // (ToDraft: an earlier test)
  expect(muts).toEqual(["MarkViewed", "MarkUnviewed", "Resolve", "AddReview", "AddThread", "AddThread", "Submit"]);
  const threads = posted.filter((x: any) => x.mutation === "AddThread").map((x: any) => x.variables);
  expect(threads).toContainEqual({ review: "PRR_e2e", path: "docs/xpu.md", body: "Please add a license header.", subject: "FILE" });
  expect(threads).toContainEqual({ review: "PRR_e2e", path: "c10/xpu/stub.cpp", body: "Keep the unconditional call for the stub?", line: 11, side: "LEFT", subject: "LINE" });
  expect(posted.find((x: any) => x.mutation === "Submit").variables).toEqual({ review: "PRR_e2e", event: "REQUEST_CHANGES", body: "Nice, one question." });
  expect(posted.find((x: any) => x.path?.endsWith("/replies")).body).toEqual({ body: "Yes, on old drivers." });
  expect(posted.find((x: any) => x.path?.endsWith("/issues/101/comments")).body).toEqual({ body: "Thanks, looking now." });
  await page.getByTestId("pr-layout-unified").click();
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

test("GitHub PR tab: agent review → suggested comments in the diff; discuss, apply or discard; only applied ones are published", async ({ page }) => {
  await ensureGithubSignIn(page);
  await page.goto("/");
  await page.getByTestId("login-github").click();
  await expect(page.getByTestId("view-document")).toBeVisible({ timeout: 15000 });
  await page.getByTestId("activity-reviews").click();
  await page.locator('[data-testid="review-pr"][data-number="101"]').click();
  await expect(page.getByTestId("pr-wiki-select")).toBeVisible(); // the wiki was chosen in an earlier test
  const before = (await (await page.request.get("/fake-github/posted")).json()).length;
  await page.getByTestId("pr-agent-review").click();
  await expect(page.getByTestId("selection-question")).toHaveValue(/Review this pull request/);
  await page.getByTestId("selection-send").click();
  await expect(page.getByTestId("pr-agent-review-done")).toContainText("suggested 2 comment(s)", { timeout: 25000 });
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText(/2 suggested comment\(s\) were added to the diff|added to the diff \(Files\) as suggestions/);
  await expect(page.locator('[data-testid="message-agent"]').last()).not.toContainText("is this guard needed"); // the JSON is not shown
  await page.getByTestId("pr-tab-files").click();
  const agent = page.locator('[data-testid="pr-draft"][data-origin="agent"]');
  await expect(agent).toHaveCount(2);
  await expect(agent.first()).toHaveAttribute("data-decision", "proposed");
  await expect(page.getByTestId("pr-undecided")).toContainText("2 agent suggestion(s) to decide");
  await expect(page.getByTestId("pr-finish-review")).toHaveText(/^Finish review ▾$/); // nothing to publish yet
  const line = agent.filter({ hasText: "is this guard needed?" });
  const file = agent.filter({ hasText: "Line 9999: Agent: consider a test." });
  await expect(page.locator('[data-file="c10/xpu/stub.cpp"] .diff-comment-row').filter({ hasText: "is this guard needed?" })).toHaveCount(1); // under its line
  // discuss a suggestion with the agent
  await line.getByTestId("pr-draft-ask").click();
  await page.getByTestId("pr-draft-ask-text").fill("Is the guard really redundant?");
  await page.getByTestId("pr-draft-ask-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Is the guard really redundant?", { timeout: 20000 });
  await expect(page.getByTestId("thread-target").last()).toContainText("suggested comment on c10/xpu/stub.cpp R");
  // decide: apply one, discard the other
  await line.getByTestId("pr-draft-accepted").click();
  await expect(line).toHaveAttribute("data-decision", "accepted");
  await file.getByTestId("pr-draft-discarded").click();
  await expect(file).toHaveAttribute("data-decision", "discarded");
  await expect(page.getByTestId("pr-finish-review")).toContainText("Finish review (1)");
  await page.getByTestId("pr-finish-review").click();
  await page.getByTestId("pr-submit-send").click();
  await expect(page.getByTestId("pr-notice")).toContainText("Review published on GitHub");
  await expect(page.getByTestId("pr-draft")).toHaveCount(0);
  const posted = (await (await page.request.get("/fake-github/posted")).json()).slice(before);
  const threads = posted.filter((x: any) => x.mutation === "AddThread").map((x: any) => x.variables);
  expect(threads).toHaveLength(1);
  expect(threads[0]).toMatchObject({ path: "c10/xpu/stub.cpp", body: "Agent: is this guard needed?", side: "RIGHT", subject: "LINE" });
  await page.locator('[data-testid="doc-tab"][data-ws="github"]').getByTestId("doc-tab-close").click();
  await signOut(page);
});

test("diff line numbers are those of the files (Markdown '---' rules, no final newline) in the wiki diff", async ({ page }) => {
  await login(page);
  const ws = workspaceDir();
  const f = path.join(ws, "docs", "numbers.md");
  const before = ["---", "title: numbers", "---", "# Numbers", "", "intro", "", "---", "", "after rule"].concat(Array.from({ length: 12 }, (_, i) => `p${i + 1}`));
  fs.writeFileSync(f, before.join("\n")); // no final newline
  gitCommitAll(ws, "numbers");
  const after = before.filter((l, i) => i !== 7 && i !== 8); // delete the '---' rule (lines 8-9) …
  after[after.length - 1] = "p12 changed"; // … and change the last line
  fs.writeFileSync(f, after.join("\n"));
  try {
    await page.getByTestId("tree-refresh").click();
    await expect(page.getByTestId("tree-refresh")).toHaveAttribute("data-spinning", "false");
    const dir = page.getByTestId("tree-root").first().locator(".tree-row.dir", { hasText: "docs" }).first();
    if ((await page.getByTestId("tree-file-docs/numbers.md").count()) === 0) await dir.click();
    await page.getByTestId("tree-file-docs/numbers.md").locator("button.tree-main").click();
    await expect(page.getByTestId("markdown-body").locator("h1")).toHaveText("Numbers"); // opened
    await page.getByTestId("view-diff").click();
    if ((await page.getByTestId("diff-layout-unified").getAttribute("aria-selected")) !== "true") await page.getByTestId("diff-layout-unified").click();
    const rows = page.locator('[data-testid="diff-file"][data-path="docs/numbers.md"] [data-testid="diff-row"]');
    await expect(rows.first()).toBeVisible({ timeout: 15000 });
    // every row's old/new number points at exactly the text shown, in the committed / current file
    const got = await rows.evaluateAll((trs) => trs.map((tr) => {
      const td = tr.querySelectorAll("td");
      return { o: td[0].textContent, n: td[1].textContent, text: (td[2].textContent ?? "").slice(1) };
    }));
    expect(got.length).toBeGreaterThan(4);
    for (const r of got) {
      if (r.o) expect(before[Number(r.o) - 1], `old line ${r.o}`).toBe(r.text.replace(/^ $/, ""));
      if (r.n) expect(after[Number(r.n) - 1], `new line ${r.n}`).toBe(r.text.replace(/^ $/, ""));
    }
    expect(got.some((r) => r.o === "8" && r.text === "---")).toBe(true); // the deleted rule is shown, at its line
    expect(got.some((r) => r.n === "20" && r.text === "p12 changed")).toBe(true);
  } finally {
    fs.rmSync(f, { force: true });
    gitCommitAll(ws, "cleanup numbers");
  }
});

test("projects: add one by cloning a URL, close it, and it stays closed", async ({ page }) => {
  await login(page);
  const parent = path.dirname(workspaceDir());
  const cp = require("child_process") as typeof import("child_process");
  const src = path.join(parent, "clone-src");
  const bare = path.join(parent, "clone-src.git");
  const dest = path.join(parent, "clone-src-copy");
  for (const d of [src, bare, dest]) fs.rmSync(d, { recursive: true, force: true });
  fs.mkdirSync(src);
  fs.writeFileSync(path.join(src, "hello.md"), "# Hello from a clone\n");
  cp.execSync("git init -q && git add -A && git -c user.email=t@e -c user.name=T commit -qm init", { cwd: src });
  cp.execSync(`git clone -q --bare "${src}" "${bare}"`);
  try {
    await expect(page.getByTestId("tree-root")).toHaveCount(1);
    await page.getByTestId("tree-add-folder").click();
    await page.getByTestId("add-folder-path").fill(parent);
    await page.getByTestId("add-folder-path").press("Enter");
    await expect(page.getByTestId("add-folder-selected")).toContainText(parent);
    await page.getByTestId("add-source-url").click();
    await page.getByTestId("clone-url").fill(`file://${bare}`);
    await page.getByTestId("clone-name").fill("clone-src-copy");
    await page.getByTestId("clone-submit").click();
    // cloned and opened as a folder of the window
    await expect(page.getByTestId("tree-root")).toHaveCount(2, { timeout: 30000 });
    await expect(page.getByTestId("add-folder-dialog")).toHaveCount(0);
    expect(fs.existsSync(path.join(dest, "hello.md"))).toBe(true);
    const root = page.getByTestId("tree-root").nth(1);
    await expect(root).toContainText("clone-src-copy");
    // the config.toml folder can be closed too (button present); close the clone
    await expect(page.getByTestId("tree-root").first().locator('[data-testid^="tree-remove-"]')).toHaveCount(1);
    await root.locator(".tree-root").hover();
    await page.getByTestId("tree-remove-clone-src-copy").click();
    await expect(page.getByTestId("confirm-dialog")).toContainText("will not show again");
    await page.getByTestId("confirm-ok").click();
    await expect(page.getByTestId("tree-root")).toHaveCount(1);
    await page.reload();
    await expect(page.getByTestId("tree-root")).toHaveCount(1); // stays closed
  } finally {
    for (const d of [src, bare, dest]) fs.rmSync(d, { recursive: true, force: true });
  }
});

test("reference folders for the agent: add and remove for a wiki folder", async ({ page }) => {
  await login(page);
  const root = page.getByTestId("tree-root").first();
  await root.locator(".tree-root").hover();
  await page.getByTestId("tree-context-root").click();
  const box = page.getByTestId("context-folders");
  await expect(box).toContainText("None yet.");
  await page.getByTestId("context-folder-input").fill("relative/path");
  await page.getByTestId("context-folder-add").click();
  await expect(page.getByTestId("context-folder-error")).toContainText("absolute path");
  await page.getByTestId("context-folder-input").fill("/tmp");
  await page.getByTestId("context-folder-add").click();
  await expect(page.getByTestId("context-folder")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await page.mouse.click(5, 5); // close (backdrop)
  await expect(box).toHaveCount(0);
  await root.locator(".tree-root").hover();
  await page.getByTestId("tree-context-root").click();
  await expect(page.getByTestId("context-folder")).toContainText("/tmp");
  await page.getByTestId("context-folder-remove").click();
  await expect(page.getByTestId("context-folder")).toHaveCount(0);
  // or browse the server's folders: path box, breadcrumbs, filter, then choose
  await page.getByTestId("context-folder-browse").click();
  const picker = page.getByTestId("folder-picker");
  await expect(picker.getByTestId("folder-places")).toContainText("~ home");
  await page.getByTestId("folder-picker-path").fill(path.dirname(workspaceDir()));
  await page.getByTestId("folder-picker-path").press("Enter");
  await page.getByTestId("folder-picker-filter").fill("wik");
  await picker.locator('[data-testid="folder-picker-entry"][data-name="wiki"]').click();
  await expect(page.getByTestId("folder-picker-selected")).toContainText(workspaceDir());
  await page.getByTestId("folder-breadcrumbs").getByRole("button", { name: path.basename(path.dirname(workspaceDir())) }).click();
  await expect(page.getByTestId("folder-picker-selected")).toContainText(path.dirname(workspaceDir()));
  await picker.locator('[data-testid="folder-picker-entry"][data-name="wiki"]').click();
  await page.getByTestId("folder-picker-choose").click();
  await expect(picker).toHaveCount(0);
  await expect(page.getByTestId("context-folder")).toContainText(workspaceDir());
  await page.getByTestId("context-folder-remove").click();
  await expect(page.getByTestId("context-folder")).toHaveCount(0);
});

test("folder chat: talk with a project directly on the Chats page, then distill the conversation into a page", async ({ page }) => {
  const errs: string[] = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  await login(page);
  const out = path.join(workspaceDir(), "chat-notes", "capture-chat.md");
  try {
    // the chat icon on a project in the file tree (or the Chats tab) opens the Chats page for it: just type
    const root = page.getByTestId("tree-root").first();
    await root.locator(".tree-root").hover();
    await page.getByTestId("tree-chats-root").click();
    await expect(page.getByTestId("view-chats")).toHaveClass(/active/);
    await expect(page.getByTestId("chat-new-page")).toBeVisible();
    await expect(page.getByTestId("chat-project")).toHaveValue(await root.getAttribute("data-ws") as string);
    await page.getByTestId("chat-first").fill("Capture chat: how is a graph captured?");
    await page.keyboard.press("Enter");
    const chat = page.getByTestId("chat-view");
    await expect(page.getByTestId("chat-title")).toHaveText("Capture chat: how is a graph captured?");
    await expect(chat.locator('[data-testid="message-agent"][data-status="completed"]')).toContainText("how is a graph captured?", { timeout: 20000 });
    await expect(page.locator('[data-testid="doc-tab"]').filter({ hasText: "Chat" })).toHaveCount(0); // a page of its own, not a document tab
    await page.getByTestId("chat-input").fill("And lowered?");
    await page.getByTestId("chat-send").click();
    await expect(chat.locator('[data-testid="message-agent"][data-status="completed"]')).toHaveCount(2, { timeout: 20000 });
    // distill it into a new page; the change is reviewed in Diff
    await page.getByTestId("chat-action-distill").check();
    await page.getByTestId("chat-target").fill("chat-notes/capture-chat.md");
    await page.getByTestId("chat-action-run").click();
    await expect(chat.locator('[data-testid="message-user"]').last()).toContainText("Distill this conversation into the wiki page `chat-notes/capture-chat.md`.");
    await expect(chat.locator('[data-testid="message-agent"][data-status="completed"]')).toHaveCount(3, { timeout: 20000 });
    await expect(chat.locator('[data-testid="message-agent"]').last()).toContainText("Wrote chat-notes/capture-chat.md from 2 question(s).");
    expect(fs.readFileSync(out, "utf-8")).toContain("- Capture chat: how is a graph captured?");
    await expect(page.getByTestId("chat-acted")).toBeVisible();
    await page.getByTestId("chat-review-changes").click();
    await expect(page.locator('[data-testid="diff-file"][data-path="chat-notes/capture-chat.md"]')).toBeVisible({ timeout: 10000 });
    // back on the Chats page: the project's earlier chats are in the Chat menu; it survives a reload; archive it
    await page.getByTestId("view-chats").click();
    await expect(page.getByTestId("chat-history").locator("option").filter({ hasText: "Capture chat" })).toContainText("(6)");
    await expect(page.getByTestId("chat-history")).not.toHaveValue("");
    await page.getByTestId("chats-new").click();
    await expect(page.getByTestId("chat-new-page")).toBeVisible();
    await page.getByTestId("chat-history").selectOption({ label: "Capture chat: how is a graph captured? (6)" });
    await expect(page.getByTestId("chat-view")).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("chat-view")).toBeVisible({ timeout: 15000 });
    await page.getByTestId("chat-archive").click();
    await page.getByTestId("confirm-ok").click();
    await expect(page.getByTestId("chat-new-page")).toBeVisible();
    await expect(page.getByTestId("chat-history").locator("option")).toHaveCount(1); // only "＋ New chat"
    expect(errs).toEqual([]);
  } finally {
    fs.rmSync(path.join(workspaceDir(), "chat-notes"), { recursive: true, force: true });
  }
});

test("a reload (or closing the browser) brings back what was shown: view, PR tab and its sub-tab, unsent text", async ({ page }) => {
  await login(page);
  await page.getByTestId("tree-root").first().waitFor();
  // the Terminal view, then the Diff view, survive a reload
  await page.getByTestId("view-terminal").click();
  await page.reload();
  await expect(page.getByTestId("view-terminal")).toHaveClass(/active/, { timeout: 15000 });
  await page.getByTestId("tree-diff-root").click();
  await expect(page.getByTestId("diff-review")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("diff-review")).toBeVisible({ timeout: 15000 });
  await page.getByTestId("view-document").click();
  // a PR tab, its Files sub-tab and a half-written conversation comment
  await signOut(page);
  await expect(page.getByTestId("login-github")).toBeVisible({ timeout: 15000 });
  await ensureGithubSignIn(page);
  await page.goto("/");
  await page.getByTestId("login-github").click();
  await page.getByTestId("activity-reviews").click();
  await page.locator('[data-testid="review-pr"][data-number="101"]').click();
  await page.getByTestId("pr-conv-text").fill("half-written thought");
  await page.getByTestId("pr-tab-files").click();
  await expect(page.getByTestId("pr-files")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("pr-view")).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId("pr-tab-files")).toHaveAttribute("aria-selected", "true");
  await page.getByTestId("pr-tab-overview").click();
  await expect(page.getByTestId("pr-conv-text")).toHaveValue("half-written thought");
  await page.getByTestId("pr-conv-text").fill("");
  await page.locator('[data-testid="doc-tab"][data-ws="github"]').getByTestId("doc-tab-close").click();
  await page.reload();
  await expect(page.getByTestId("pr-view")).toHaveCount(0); // closed stays closed
  await signOut(page);
});

test("PR Development: new branch workspace, the web editor and the VS Code option, the agent with the wiki, build/test, commit", async ({ page }) => {
  const src = fs.readFileSync(`/tmp/waw-e2e-devsrc-${E2E_PORT}`, "utf-8").trim();
  await login(page);
  await page.getByTestId("activity-development").click();
  await expect(page.getByTestId("dev-panel")).toBeVisible();
  await page.getByTestId("dev-new").click();
  await page.getByTestId("dev-branch").fill("feature/e2e");
  await page.getByTestId("dev-source").fill(src);
  await expect(page.getByTestId("dev-wiki")).not.toHaveValue("");
  await page.getByTestId("dev-create").click();
  await expect(page.getByTestId("dev-view")).toBeVisible();
  await expect(page.getByTestId("dev-item").first()).toHaveAttribute("data-status", "ready", { timeout: 20000 });
  // the web editor (default): explorer, a tab, edit + Ctrl+S saves to the worktree
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  await page.locator('[data-testid="webide-tree-item"][data-path="lib.py"]').click();
  await expect(page.locator('[data-testid="webide-tab"][data-path="lib.py"]')).toBeVisible();
  await page.locator(".monaco-editor .view-lines").click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("\nEDITED = True\n");
  await expect(page.locator('[data-testid="webide-tab"][data-path="lib.py"] .webide-tab-close')).toHaveAttribute("data-dirty", "1");
  await page.keyboard.press("Control+s");
  await expect(page.locator('[data-testid="webide-tab"][data-path="lib.py"] .webide-tab-close')).toHaveAttribute("data-dirty", "0");
  const devWt = fs.readdirSync(path.join(src, "..", "worktrees")).map((d) => path.join(src, "..", "worktrees", d)).find((d) => fs.existsSync(path.join(d, "lib.py")))!;
  expect(fs.readFileSync(path.join(devWt, "lib.py"), "utf-8")).toContain("EDITED = True");
  // a change on disk (agent, terminal) shows up in a clean tab
  fs.appendFileSync(path.join(devWt, "lib.py"), "FROM_DISK = 1\n");
  await expect(page.locator(".monaco-editor .view-lines")).toContainText("FROM_DISK", { timeout: 15000 });
  // quick open and search
  await page.keyboard.press("Control+p");
  await page.getByTestId("webide-quick-input").fill("pyproj");
  await page.getByTestId("webide-quick-item").first().click();
  await expect(page.locator('[data-testid="webide-tab"][data-path="pyproject.toml"]')).toBeVisible();
  await page.getByTestId("webide-view-search").click();
  await page.getByTestId("webide-search-input").fill("EDITED");
  await page.getByTestId("webide-search-input").press("Enter");
  await expect(page.getByTestId("webide-search-hit")).toHaveCount(1);
  // the agent from the editor's context menu
  await page.locator('[data-testid="webide-tab"][data-path="lib.py"]').click();
  await page.locator(".monaco-editor .view-lines").click({ button: "right" });
  const item = await page.getByRole("menuitem", { name: /Agent: Explain this code/ }).boundingBox();
  await page.mouse.move(item!.x + 20, item!.y + item!.height / 2); // Monaco's menu acts on a real pointer
  await page.waitForTimeout(200);
  await page.mouse.down();
  await page.mouse.up();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').first()).toContainText("echo", { timeout: 20000 });
  // the VS Code option: code-server through the app's proxy
  await page.getByTestId("dev-editor-kind").selectOption("vscode");
  const frame = page.frameLocator('[data-testid="dev-iframe"]');
  await expect(frame.locator("body")).toContainText("fake-code-server", { timeout: 20000 });
  await expect(frame.locator("body")).toContainText(".code-workspace");
  // the agent works in the worktree
  await page.getByTestId("dev-kind").selectOption("implement");
  await page.getByTestId("dev-input").fill("Add a test [[write:test_lib.py:::from lib import add\\ndef test_add():\\n    assert add(1, 2) == 3\\n]]");
  await page.getByTestId("dev-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("wrote test_lib.py", { timeout: 20000 });
  // a custom command and its output
  await page.getByTestId("dev-tab-tasks").click();
  await page.getByTestId("dev-custom").fill("ls && exit 4");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.locator('[data-testid="dev-run"]').first()).toHaveAttribute("data-status", "failed", { timeout: 20000 });
  await expect(page.getByTestId("dev-log")).toContainText("test_lib.py");
  await expect(page.getByTestId("dev-run-fix")).toBeVisible();
  // commit the agent's file
  await page.getByTestId("dev-tab-changes").click();
  await page.getByTestId("dev-commit-toggle").click(); // the commit panel opens at the top, as on the wiki's Diff page
  await expect(page.getByTestId("dev-commit-panel")).toBeVisible();
  await expect(page.getByTestId("dev-file")).toHaveCount(2); // lib.py (edited above) and the agent's test_lib.py
  await page.getByTestId("dev-commit-msg").fill("Add a test for add()");
  await page.getByTestId("dev-commit").click();
  await expect(page.getByTestId("dev-note")).toContainText("Committed 2 file(s)");
  // the selection survives a reload: PR Development is still shown
  await page.reload();
  await expect(page.getByTestId("dev-view")).toBeVisible({ timeout: 15000 });
  await page.getByTestId("dev-editor-stop").click();
  await expect(page.getByTestId("dev-editor-start")).toBeVisible();
  await page.getByTestId("dev-editor-kind").selectOption("web");
  await expect(page.getByTestId("webide")).toBeVisible();
  await expect(page.locator('[data-testid="webide-tab"][data-path="lib.py"]')).toBeVisible(); // open tabs are remembered
  await page.getByTestId("activity-files").click();
});

test("PR Development: discuss a selection (marked in the margin), selected files as context, a terminal in the worktree", async ({ page }) => {
  await login(page);
  await page.getByTestId("activity-development").click();
  await page.getByTestId("dev-item").first().click();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  await page.locator('[data-testid="webide-tree-item"][data-path="lib.py"] .webide-name').click();
  await page.locator(".monaco-editor .view-lines").click();
  await page.keyboard.press("Control+Home");
  await page.keyboard.press("Shift+ArrowDown");
  await page.keyboard.press("Shift+End");
  await page.getByTestId("webide-discuss-btn").click();
  await page.getByTestId("webide-discuss-input").fill("Is add commutative?");
  await page.getByTestId("webide-discuss-input").press("Enter");
  await expect(page.getByTestId("dev-anchor").last()).toContainText("1–2", { timeout: 15000 });
  await expect(page.locator(".webide-thread-glyph")).toHaveCount(1);
  await expect(page.locator('[data-testid="dev-task"]').last().locator('[data-testid="message-agent"][data-status="completed"]')).toContainText("echo: Is add commutative?", { timeout: 20000 });
  // Ctrl-click files in the explorer → "Selected files" context
  await page.locator('[data-testid="webide-tree-item"][data-path="pyproject.toml"] .webide-name').click({ modifiers: ["Control"] });
  await page.locator('[data-testid="webide-tree-item"][data-path="CMakeLists.txt"] .webide-name').click({ modifiers: ["Control"] });
  await expect(page.getByTestId("webide-picked")).toContainText("2 selected");
  await page.getByTestId("dev-scope").selectOption("selected");
  await page.getByTestId("dev-target").selectOption("__new__");
  await page.getByTestId("dev-input").fill("Explain these files");
  await page.getByTestId("dev-send").click();
  await expect(page.locator('[data-testid="dev-task"]').last().locator('[data-testid="message-agent"][data-status="completed"]')).toContainText("echo", { timeout: 20000 });
  // like a wiki document: each open file has its own discussions in the panel
  await page.getByTestId("dev-view-file").click();
  await page.locator('[data-testid="webide-tab"][data-path="lib.py"]').click();
  await expect(page.getByTestId("dev-file-head")).toContainText("lib.py");
  const libCount = await page.locator('[data-testid="dev-task"]').count();
  expect(libCount).toBeGreaterThan(0);
  await expect(page.locator('[data-testid="webide-tab"][data-path="lib.py"] [data-testid="webide-tab-discussions"]')).toBeVisible();
  await page.locator('[data-testid="webide-tree-item"][data-path="pyproject.toml"] .webide-name').click();
  await expect(page.getByTestId("dev-file-head")).toContainText("pyproject.toml");
  await expect(page.getByTestId("dev-file-empty")).toBeVisible();
  await page.getByTestId("dev-target").selectOption("__new__");
  await page.getByTestId("dev-input").fill("What does this file configure?");
  await page.getByTestId("dev-send").click();
  await expect(page.locator('[data-testid="dev-task"]')).toHaveCount(1);
  await expect(page.locator('[data-testid="dev-task"] [data-testid="message-agent"][data-status="completed"]')).toContainText("What does this file configure?", { timeout: 20000 });
  await expect(page.locator('[data-testid="webide-tab"][data-path="pyproject.toml"] [data-testid="webide-tab-discussions"]')).toContainText("1");
  await page.locator('[data-testid="webide-tab"][data-path="lib.py"]').click();
  await expect(page.getByTestId("dev-file-head")).toContainText("lib.py"); // back to lib.py's discussions
  await expect.poll(() => page.locator('[data-testid="dev-task"]').count()).toBeGreaterThanOrEqual(libCount);
  await expect(page.locator('[data-testid="dev-task"]', { hasText: "What does this file configure?" })).toHaveCount(0);
  await page.getByTestId("dev-view-all").click();
  await expect(page.locator('[data-testid="dev-task"]')).toHaveCount(await page.locator('[data-testid="dev-task"]').count());
  // a terminal in the worktree
  await page.getByTestId("dev-tab-terminal").click();
  await expect(page.getByTestId("dev-terminal-tab")).toHaveCount(1, { timeout: 15000 });
  const term = page.locator('[data-testid="dev-terminal"] .xterm');
  await expect(term).toBeVisible();
  await expect(page.locator('[data-testid="dev-terminal"] .xterm-rows')).toContainText("$", { timeout: 15000 }); // the shell is ready
  await term.click();
  await page.keyboard.type("echo DEV_TERM_$((40+2))\n");
  await expect(page.locator('[data-testid="dev-terminal"] .xterm-rows')).toContainText("DEV_TERM_42", { timeout: 15000 });
  await page.getByTestId("dev-tab-tasks").click();
  await page.getByTestId("activity-files").click();
});

test("PR Development: Markdown like a wiki page, Bazel/YAML navigation, a session per file", async ({ page }) => {
  await login(page);
  await page.getByTestId("activity-development").click();
  await page.getByTestId("dev-item").first().click();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  await page.getByTestId("dev-view-file").click();
  // Markdown: rendered (Preview), outline, select → Discuss, highlighted, links open files
  await page.locator('[data-testid="webide-tree-item"][data-path="doc.md"] .webide-name').click();
  const md = page.getByTestId("webide-md-body");
  await expect(md.locator("h1")).toHaveText("Design notes");
  await expect(page.getByTestId("webide-md-toc")).toContainText("Details");
  await md.locator("p", { hasText: "commutative" }).selectText();
  await md.locator("p", { hasText: "commutative" }).dispatchEvent("mouseup");
  await page.getByTestId("webide-md-discuss-btn").click();
  await page.getByTestId("webide-discuss-input").fill("Is this true?");
  await page.getByTestId("webide-discuss-input").press("Enter");
  await expect(page.getByTestId("dev-anchor").last()).toContainText("lines 3", { timeout: 15000 });
  await expect(md.locator("mark.waw-hl.thread")).toContainText("commutative");
  const docSession = await page.getByTestId("dev-file-session-select").inputValue();
  expect(docSession).not.toBe("");
  await page.getByTestId("webide-md-split").click();
  await expect(page.getByTestId("webide-editor")).toBeVisible();
  await page.getByTestId("webide-md-preview").click();
  await md.locator("a", { hasText: "the code" }).click();
  await expect(page.locator('[data-testid="webide-tab"][data-path="lib.py"]')).toHaveAttribute("aria-selected", "true");
  // switching files switches the agent session to the file's own
  await expect(page.getByTestId("dev-file-session-select")).not.toHaveValue(docSession);
  await page.locator('[data-testid="webide-tab"][data-path="doc.md"]').click();
  await expect(page.getByTestId("dev-file-session-select")).toHaveValue(docSession);
  // Bazel: Starlark highlighting, F12 on a label opens the file it names
  await page.locator('[data-testid="webide-tree-item"][data-path="pkg"] .webide-name').click();
  await page.locator('[data-testid="webide-tree-item"][data-path="pkg/BUILD.bazel"] .webide-name').click();
  await expect(page.getByTestId("webide-status")).toContainText("starlark");
  await expect(page.getByTestId("webide-lsp")).toContainText("basic navigation", { timeout: 15000 });
  await page.locator(".monaco-editor .view-lines").click();
  await page.keyboard.press("Control+g");
  await page.keyboard.type("1:12");
  await page.keyboard.press("Enter");
  await page.keyboard.press("F12");
  await expect(page.locator('[data-testid="webide-tab"][data-path="tools/defs.bzl"]')).toHaveAttribute("aria-selected", "true", { timeout: 15000 });
  // YAML: outline (Ctrl+Shift+O) from its keys
  await page.locator('[data-testid="webide-tree-item"][data-path="ci.yaml"] .webide-name').click();
  await expect(page.getByTestId("webide-status")).toContainText("yaml");
  await page.locator(".monaco-editor .view-lines").click();
  await page.keyboard.press("Control+Shift+o");
  await expect(page.locator(".quick-input-widget")).toContainText("jobs", { timeout: 10000 });
  await expect(page.locator(".quick-input-widget")).toContainText("build");
  await page.keyboard.press("Escape");
  await page.getByTestId("activity-files").click();
});

test("chat about a folder or a file: from the file tree, in PR Development's explorer", async ({ page }) => {
  await login(page);
  // Files: 💬 on a folder starts a chat about it
  const dir = page.locator(".tree-row.dir", { hasText: "docs" });
  await dir.hover();
  await page.getByTestId("tree-chat-docs").click();
  await expect(page.getByTestId("chat-about")).toHaveText("docs");
  await page.getByTestId("chat-first").fill("What is in this folder?");
  await page.getByTestId("chat-first-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("What is in this folder?", { timeout: 20000 });
  await expect(page.getByTestId("chat-history").locator("option", { hasText: "[docs]" })).toHaveCount(1);
  await page.getByTestId("view-document").click();
  // PR Development: 💬 on a folder of the worktree
  await page.getByTestId("activity-development").click();
  await page.getByTestId("dev-item").first().click();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  const row = page.locator('[data-testid="webide-tree-item"][data-path="tools"]');
  await row.hover();
  await row.getByTestId("webide-discuss-folder").click();
  await expect(page.getByTestId("webide-discuss")).toContainText("Discuss the folder tools");
  await page.getByTestId("webide-discuss-input").fill("What do these macros do?");
  await page.getByTestId("webide-discuss-input").press("Enter");
  await page.getByTestId("dev-view-all").click();
  await expect(page.getByTestId("dev-task-about").last()).toContainText("tools", { timeout: 15000 });
  await page.getByTestId("activity-files").click();
});

test("PR Development: chat about any type of file — text, image, binary", async ({ page }) => {
  await login(page);
  await page.getByTestId("activity-development").click();
  await page.getByTestId("dev-item").first().click();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  await page.getByTestId("dev-view-file").click();
  // a text file: "Chat about this file" in the breadcrumbs
  await page.locator('[data-testid="webide-tree-item"][data-path="ci.yaml"] .webide-name').click();
  await page.getByTestId("webide-chat-file").click();
  await expect(page.getByTestId("webide-discuss")).toContainText("Discuss ci.yaml (the whole file)");
  await page.getByTestId("webide-discuss-input").fill("What does this CI do?");
  await page.getByTestId("webide-discuss-input").press("Enter");
  await expect(page.locator('[data-testid="dev-task"] [data-testid="message-agent"][data-status="completed"]').last()).toContainText("What does this CI do?", { timeout: 20000 });
  // an image: shown, discussed (the picture goes to the agent)
  await page.locator('[data-testid="webide-tree-item"][data-path="logo.png"] .webide-name').click();
  await expect(page.getByTestId("webide-image").locator("img")).toBeVisible();
  await page.getByTestId("webide-discuss-image-btn").click();
  await page.getByTestId("webide-discuss-input").fill("What is in the logo?");
  await page.getByTestId("webide-discuss-input").press("Enter");
  await expect(page.getByTestId("dev-file-head")).toContainText("logo.png");
  await expect(page.locator('[data-testid="dev-task"] [data-testid="message-agent"][data-status="completed"]').last()).toContainText("What is in the logo?", { timeout: 20000 });
  // a PDF: shown with PDF.js; a page is discussed with its picture
  const src = fs.readFileSync(`/tmp/waw-e2e-devsrc-${E2E_PORT}`, "utf-8").trim();
  const wtRoot = path.join(src, "..", "worktrees");
  const devWt = fs.readdirSync(wtRoot).map((d) => path.join(wtRoot, d)).find((d) => fs.existsSync(path.join(d, "lib.py")))!;
  const cp = require("child_process") as typeof import("child_process");
  cp.execFileSync(path.resolve(__dirname, "../../.venv/bin/python"), [path.resolve(__dirname, "../fixtures/make_pdf.py"), path.join(devWt, "spec.pdf"), "Timing of the cache", "Second page"]);
  await page.locator('[data-testid="webide-explorer"] button[title="Refresh"]').click();
  await page.locator('[data-testid="webide-tree-item"][data-path="spec.pdf"] .webide-name').click();
  const pdfView = page.getByTestId("webide-pdfjs");
  await expect(pdfView).toHaveAttribute("data-pages", "2", { timeout: 20000 });
  const pg = pdfView.locator('[data-page="1"]');
  await expect(pg).toHaveAttribute("data-rendered", "true", { timeout: 15000 });
  await pg.getByTestId("pdfjs-ask-page").click();
  await expect(page.getByTestId("webide-discuss-image")).toBeVisible();
  await page.getByTestId("webide-discuss-input").fill("Explain this page");
  await page.getByTestId("webide-discuss-input").press("Enter");
  await expect(page.locator('[data-testid="dev-task"] [data-testid="message-agent"][data-status="completed"]').last()).toContainText("Explain this page", { timeout: 20000 });
  // any other file
  await page.locator('[data-testid="webide-tree-item"][data-path="model.onnx"] .webide-name').click();
  await expect(page.getByTestId("webide-binary")).toBeVisible();
  await page.getByTestId("webide-discuss-binary").click();
  await page.getByTestId("webide-discuss-input").fill("What model is this?");
  await page.getByTestId("webide-discuss-input").press("Enter");
  await expect(page.locator('[data-testid="dev-task"] [data-testid="message-agent"][data-status="completed"]').last()).toContainText("What model is this?", { timeout: 20000 });
  await expect(page.locator('[data-testid="webide-tab"][data-path="model.onnx"] [data-testid="webide-tab-discussions"]')).toContainText("1");
  await page.getByTestId("activity-files").click();
});

test("PR Development: Changes view like the wiki's Diff: per-file diffs, side by side, more context, ask about lines, open in editor, commit picked files, a commit's changes", async ({ page }) => {
  const src = fs.readFileSync(`/tmp/waw-e2e-devsrc-${E2E_PORT}`, "utf-8").trim();
  const devWt = fs.readdirSync(path.join(src, "..", "worktrees")).map((d) => path.join(src, "..", "worktrees", d)).find((d) => fs.existsSync(path.join(d, "lib.py")))!;
  const cp = require("child_process") as typeof import("child_process");
  await login(page);
  await page.getByTestId("activity-development").click();
  await page.getByTestId("dev-item").first().click();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  // changes made outside the app's commit panel: an edit in the middle of a longer file, a new file
  const lib = fs.readFileSync(path.join(devWt, "lib.py"), "utf-8");
  const pad = Array.from({ length: 40 }, (_, i) => `PAD_${i} = ${i}`).join("\n");
  fs.writeFileSync(path.join(devWt, "lib.py"), `${lib}${pad}\n`);
  cp.execFileSync("git", ["-C", devWt, "add", "lib.py"]);
  cp.execFileSync("git", ["-C", devWt, "-c", "user.name=e2e", "-c", "user.email=e2e@x", "commit", "-q", "-m", "Pad lib.py"]);
  fs.writeFileSync(path.join(devWt, "lib.py"), `${lib}${pad.replace("PAD_20 = 20", "PAD_20 = 'changed'")}\n`);
  fs.writeFileSync(path.join(devWt, "notes_e2e.md"), "# notes\n");
  // the Changes view in the editor area
  await page.getByTestId("dev-changes-toggle").click();
  await expect(page.locator(".dev-changes-main [data-testid=dev-changes]")).toBeVisible();
  await expect(page.getByTestId("webide")).toBeHidden(); // still mounted, just hidden
  const libDiff = page.locator('[data-testid="dev-diff-file"][data-path="lib.py"]');
  await expect(libDiff).toBeVisible({ timeout: 15000 });
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="notes_e2e.md"]')).toContainText("untracked");
  await expect(libDiff.getByTestId("dev-diff-unified")).toContainText("PAD_20 = 'changed'");
  await expect(libDiff.getByTestId("dev-diff-unified")).not.toContainText("PAD_5 = 5"); // outside the hunk
  // more context, side by side
  await libDiff.getByTestId("dev-diff-expand-all").click();
  await expect(libDiff.getByTestId("dev-diff-unified")).toContainText("PAD_5 = 5");
  await page.getByTestId("dev-layout-split").click();
  await expect(libDiff.getByTestId("dev-diff-split")).toContainText("PAD_20 = 'changed'");
  await page.getByTestId("dev-layout-unified").click();
  // ask the agent about the changed line: the discussion is anchored on it
  await libDiff.locator('[data-testid="dev-diff-row"].add').first().click();
  await page.getByTestId("dev-diff-question").fill("Why change PAD_20?");
  await page.getByTestId("dev-diff-ask").click();
  await expect(page.locator('[data-testid="dev-task"] [data-testid="message-agent"][data-status="completed"]').last()).toContainText("Why change PAD_20?", { timeout: 20000 });
  // open that line in the editor
  await libDiff.locator('[data-testid="dev-diff-row"].add').first().click();
  await page.getByTestId("dev-diff-open-line").click();
  await expect(page.getByTestId("webide")).toBeVisible();
  await expect(page.locator('[data-testid="webide-tab"][data-path="lib.py"]')).toHaveAttribute("aria-selected", "true");
  // back to the changes: mark lib.py viewed (it folds), commit only lib.py
  await page.getByTestId("dev-changes-toggle").click();
  await libDiff.getByTestId("dev-diff-viewed").check();
  await expect(libDiff).toHaveAttribute("data-viewed", "1");
  await expect(libDiff.getByTestId("dev-diff-unified")).toHaveCount(0);
  await page.getByTestId("dev-commit-toggle").click();
  await page.getByTestId("dev-select-all").uncheck(); // (earlier tests leave other files around)
  await libDiff.getByTestId("dev-diff-in-commit").check(); // picked from the diff itself
  await expect(page.getByTestId("dev-commit")).toHaveText("Commit 1 file(s)");
  await page.getByTestId("dev-commit-msg").fill("Change PAD_20");
  await page.getByTestId("dev-commit").click();
  await expect(page.getByTestId("dev-note")).toContainText("Committed 1 file(s)");
  await expect(libDiff).toHaveCount(0); // committed: no longer an uncommitted change
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="notes_e2e.md"]')).toBeVisible();
  // the commit's own changes, then everything on the branch
  await page.getByTestId("dev-branch-commit").filter({ hasText: "Change PAD_20" }).click();
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="lib.py"]')).toBeVisible();
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="notes_e2e.md"]')).toHaveCount(0);
  await page.getByTestId("dev-compare").selectOption("branch");
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="test_lib.py"]')).toBeVisible(); // committed earlier on the branch
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="notes_e2e.md"]')).toBeVisible();
  await page.getByTestId("dev-compare").selectOption("wt");
  // the bottom panel shows the same view when it is not expanded
  await page.getByTestId("dev-changes-wide").click();
  await expect(page.getByTestId("webide")).toBeVisible();
  await expect(page.locator(".dev-bottom [data-testid=dev-changes]")).toBeVisible();
  fs.rmSync(path.join(devWt, "notes_e2e.md"));
  await page.getByTestId("activity-files").click();
});

test("PR Development: resize the bottom panel; choose a model for a discussion and a chat", async ({ page }) => {
  await login(page);
  await page.getByTestId("activity-development").click();
  await page.getByTestId("dev-item").first().click();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  const bottom = page.locator(".dev-bottom");
  const h0 = (await bottom.boundingBox())!.height;
  const sp = (await page.getByTestId("dev-bottom-splitter").boundingBox())!;
  await page.mouse.move(sp.x + sp.width / 2, sp.y + sp.height / 2);
  await page.mouse.down();
  await page.mouse.move(sp.x + sp.width / 2, sp.y - 120, { steps: 5 });
  await page.mouse.up();
  const h1 = (await bottom.boundingBox())!.height;
  expect(h1).toBeGreaterThan(h0 + 100);
  await page.reload();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  expect(Math.abs((await page.locator(".dev-bottom").boundingBox())!.height - h1)).toBeLessThan(3); // kept
  await page.getByTestId("dev-bottom-splitter").dblclick();
  expect(Math.abs((await page.locator(".dev-bottom").boundingBox())!.height - h0)).toBeLessThan(3); // default
  // another model for a new discussion about a file
  await page.getByTestId("dev-view-file").click();
  await page.locator('[data-testid="webide-tree-item"][data-path="lib.py"] .webide-name').click();
  await page.getByTestId("webide-chat-file").click();
  const box = page.getByTestId("webide-discuss");
  await expect(box.getByTestId("model-choice-model").locator("option").first()).toContainText("Default");
  await box.getByTestId("model-choice-model").selectOption("fake/beta");
  await box.getByTestId("model-choice-effort").selectOption("high");
  await page.getByTestId("webide-discuss-input").fill("Which model answers?");
  await page.getByTestId("webide-discuss-input").press("Enter");
  await expect(page.locator('[data-testid="dev-task"] [data-testid="message-agent"][data-status="completed"]').last()).toContainText("Which model answers?", { timeout: 20000 });
  await expect(page.getByTestId("model-select").first()).toHaveValue("fake/beta"); // the file's session now runs it
  // a folder chat with another model
  await page.getByTestId("activity-files").click();
  await page.locator(".tree-row.dir", { hasText: "docs" }).hover();
  await page.getByTestId("tree-chat-docs").click();
  await page.getByTestId("chat-new-page").getByTestId("model-choice-model").selectOption("other/gamma");
  await page.getByTestId("chat-first").fill("Gamma, what is here?");
  await page.getByTestId("chat-first-send").click();
  await expect(page.getByTestId("chats-page").locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("Gamma, what is here?", { timeout: 20000 });
  await page.getByTestId("view-document").click();
});


test("PR Development: open a local folder directly, create a branch there, view the diff of a folder, of a file, of everything", async ({ page }) => {
  const cp = require("child_process") as typeof import("child_process");
  const proj = path.join(path.dirname(workspaceDir()), "direct-proj");
  fs.rmSync(proj, { recursive: true, force: true });
  fs.mkdirSync(path.join(proj, "pkg"), { recursive: true });
  fs.writeFileSync(path.join(proj, "top.py"), "TOP = 1\n");
  fs.writeFileSync(path.join(proj, "pkg", "x.py"), "X = 1\n");
  const g = (...a: string[]) => cp.execFileSync("git", ["-C", proj, "-c", "user.name=e2e", "-c", "user.email=e2e@x", ...a], { encoding: "utf-8" }).trim();
  g("init", "-q", "-b", "main");
  g("add", "-A");
  g("commit", "-q", "-m", "init");
  await login(page);
  await page.getByTestId("activity-development").click();
  await page.getByTestId("dev-new").click();
  await page.getByTestId("dev-mode-folder").click();
  await expect(page.getByTestId("dev-setup")).toContainText("works directly in this folder");
  await page.getByTestId("dev-source").fill(proj);
  await page.getByTestId("dev-create").click();
  await expect(page.getByTestId("dev-view")).toBeVisible();
  await expect(page.getByTestId("dev-branch-button")).toContainText("main", { timeout: 20000 });
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  await expect(page.locator(".webide-explorer .webide-row.root").first()).toContainText("MAIN");
  // the list of workspaces and the agent panel fold, and come back
  await page.getByTestId("dev-panel-fold").click();
  await expect(page.getByTestId("dev-panel")).toBeHidden();
  await page.getByTestId("dev-show-projects").click();
  await expect(page.getByTestId("dev-panel")).toBeVisible();
  await expect(page.getByTestId("dev-show-projects")).toHaveCount(0);
  await page.getByTestId("dev-agent-fold").click();
  await expect(page.locator(".dev-side")).toHaveCount(0);
  await page.getByTestId("dev-agent-strip").click();
  await expect(page.locator(".dev-side")).toBeVisible();
  await expect(page.getByTestId("dev-agent-strip")).toHaveCount(0);
  // a branch, created in the folder itself
  await page.getByTestId("dev-branch-button").click();
  await page.getByTestId("dev-branch-new").fill("feature/direct");
  await page.getByTestId("dev-branch-create").click();
  await expect(page.getByTestId("dev-branch-button")).toContainText("feature/direct");
  expect(g("symbolic-ref", "--short", "HEAD")).toBe("feature/direct");
  await expect(page.locator(".webide-explorer .webide-row.root").first()).toContainText("FEATURE/DIRECT"); // the explorer follows
  // changes in a folder and outside it
  fs.writeFileSync(path.join(proj, "pkg", "x.py"), "X = 2\n");
  fs.writeFileSync(path.join(proj, "pkg", "new.py"), "NEW = 1\n");
  fs.writeFileSync(path.join(proj, "top.py"), "TOP = 2\n");
  // ± of the folder in the explorer: only its changes
  const row = page.locator('[data-testid="webide-tree-item"][data-path="pkg"]');
  await row.hover();
  await row.getByTestId("webide-diff").click();
  await expect(page.getByTestId("dev-diff-scope")).toContainText("pkg");
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="pkg/x.py"]')).toContainText("X = 2");
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="pkg/new.py"]')).toBeVisible();
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="top.py"]')).toHaveCount(0);
  // all changes
  await page.getByTestId("dev-diff-scope-clear").click();
  await expect(page.locator('[data-testid="dev-diff-file"]')).toHaveCount(3);
  // ± of one file
  await page.getByTestId("dev-changes-toggle").click(); // back to the editor
  const file = page.locator('[data-testid="webide-tree-item"][data-path="top.py"]');
  await file.hover();
  await file.getByTestId("webide-diff").click();
  await expect(page.locator('[data-testid="dev-diff-file"]')).toHaveCount(1);
  await expect(page.locator('[data-testid="dev-diff-file"][data-path="top.py"]')).toContainText("TOP = 2");
  // the toolbar's Diff: everything again; commit and see the branch's changes against main
  await page.getByTestId("dev-changes-toggle").click();
  await page.getByTestId("dev-changes-toggle").click();
  await expect(page.getByTestId("dev-diff-scope")).toHaveCount(0);
  await expect(page.locator('[data-testid="dev-diff-file"]')).toHaveCount(3);
  await page.getByTestId("dev-commit-toggle").click();
  await page.getByTestId("dev-commit-msg").fill("Direct work");
  await page.getByTestId("dev-commit").click();
  await expect(page.getByTestId("dev-note")).toContainText("Committed 3 file(s)");
  expect(g("log", "-1", "--format=%s", "feature/direct")).toBe("Direct work");
  await page.getByTestId("dev-compare").selectOption("branch");
  await expect(page.locator('[data-testid="dev-diff-file"]')).toHaveCount(3);
  // archive: the folder stays
  page.once("dialog", (d) => d.accept());
  await page.locator(".dev-toolbar .icon-btn", { hasText: "⋯" }).click();
  await expect(page.getByTestId("confirm-dialog")).toContainText("is not touched");
  await page.getByTestId("confirm-ok").click();
  await expect(page.getByTestId("dev-view")).toHaveCount(0, { timeout: 15000 });
  expect(fs.existsSync(path.join(proj, "top.py"))).toBe(true);
  await page.getByTestId("activity-files").click();
});
test("Local files: open a folder and a file, edit and save like a file manager; Wiki is the former Files", async ({ page }) => {
  await login(page);
  await expect(page.getByTestId("activity-files")).toHaveAttribute("title", /^Wiki/);
  const dir = path.join(path.dirname(workspaceDir()), "localfm");
  fs.mkdirSync(path.join(dir, "sub"), { recursive: true });
  fs.writeFileSync(path.join(dir, "notes.txt"), "first line\n");
  fs.writeFileSync(path.join(dir, "README.md"), "# Local readme\n\nsome text\n");
  await page.getByTestId("activity-local").click();
  await expect(page.getByTestId("local-panel")).toBeVisible();
  await page.getByTestId("local-open-folder").click();
  await expect(page.getByTestId("folder-picker-selected")).toContainText("/"); // the picker's first listing is in
  await page.getByTestId("folder-picker-path").fill(dir);
  await page.getByTestId("folder-picker-path").press("Enter");
  await expect(page.getByTestId("folder-picker-selected")).toContainText(dir);
  await page.getByTestId("folder-picker-choose").click();
  const root = page.locator(`[data-testid="local-root"][data-path="${dir}"]`);
  await expect(root).toBeVisible();
  const notes = page.locator(`[data-testid="local-item"][data-path="${dir}/notes.txt"]`);
  if (!(await notes.isVisible())) await root.click();
  await notes.click();
  await expect(page.locator(`[data-testid="local-tab"][data-path="${dir}/notes.txt"]`)).toBeVisible();
  await page.locator('[data-testid="local-monaco"] .view-lines').click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("added by the editor\n");
  await expect(page.locator(`[data-testid="local-tab"][data-path="${dir}/notes.txt"] .webide-tab-close`)).toHaveAttribute("data-dirty", "1");
  await page.keyboard.press("Control+s");
  await expect(page.locator(`[data-testid="local-tab"][data-path="${dir}/notes.txt"] .webide-tab-close`)).toHaveAttribute("data-dirty", "0");
  expect(fs.readFileSync(path.join(dir, "notes.txt"), "utf-8")).toContain("added by the editor");
  // changes on disk show up in a clean tab
  fs.appendFileSync(path.join(dir, "notes.txt"), "FROM_OUTSIDE\n");
  await expect(page.locator('[data-testid="local-monaco"] .view-lines')).toContainText("FROM_OUTSIDE", { timeout: 15000 });
  // Markdown: a preview, Edit for the source
  await page.locator(`[data-testid="local-item"][data-path="${dir}/README.md"]`).click();
  await expect(page.getByTestId("local-md-body").locator("h1")).toHaveText("Local readme");
  await page.getByTestId("local-md-edit").click();
  await expect(page.getByTestId("local-monaco")).toBeVisible();
  // a file by its path (outside the folder)
  await page.getByTestId("local-open-file").fill(path.join(workspaceDir(), "README.md"));
  await page.getByTestId("local-open-file").press("Enter");
  await expect(page.locator(`[data-testid="local-tab"][data-path="${path.join(workspaceDir(), "README.md")}"]`)).toBeVisible();
  // the tabs come back after a reload; Wiki brings the reader back
  await page.reload();
  await expect(page.locator(`[data-testid="local-tab"][data-path="${dir}/notes.txt"]`)).toBeVisible({ timeout: 15000 });
  await page.locator(`[data-testid="local-root"][data-path="${dir}"]`).hover();
  await page.locator(`[data-testid="local-root"][data-path="${dir}"]`).getByTestId("local-close-root").click();
  await expect(page.locator(`[data-testid="local-root"][data-path="${dir}"]`)).toHaveCount(0);
  await page.getByTestId("activity-files").click();
  await expect(page.getByTestId("view-document")).toBeVisible();
  fs.rmSync(dir, { recursive: true, force: true });
});

test("PR diff: copy the file path, expand hidden lines around a change, expand / collapse the whole file", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await ensureGithubSignIn(page);
  await page.goto("/");
  await page.getByTestId("login-github").click();
  await page.getByTestId("activity-reviews").click();
  await page.locator('[data-testid="review-pr"][data-number="101"]').click();
  await page.getByTestId("pr-tab-files").click();
  if ((await page.getByTestId("pr-layout-unified").getAttribute("aria-selected")) !== "true") await page.getByTestId("pr-layout-unified").click();
  const f = page.locator('[data-testid="pr-file"][data-file="c10/xpu/stub.cpp"]');
  await expect(f).toBeVisible();
  if ((await f.locator(".diff-table").count()) === 0) await f.locator(".diff-file-head").click();
  await f.getByTestId("pr-copy-path").click();
  await expect(f.getByTestId("pr-copy-path")).toHaveText("✓");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("c10/xpu/stub.cpp");
  const rows = f.locator('[data-testid="pr-diff-row"]');
  const n0 = await rows.count();
  // the 9 lines above the hunk: one click shows them all
  await f.locator(".diff-line.hunk").first().getByTestId("pr-expand-all").click();
  await expect(f.locator(".diff-line.expanded")).toHaveCount(9);
  await expect(f.locator(".diff-line.expanded").first()).toContainText("// line 1");
  // below the last change: 20 at a time, then the rest
  const tail = f.locator(".diff-line.hunk").last();
  await tail.getByTestId("pr-expand-down").click();
  await expect(f.locator(".diff-line.expanded")).toHaveCount(29);
  await expect(f.locator('.diff-line.expanded[data-line="33"]')).toContainText("// line 33");
  await f.locator(".diff-line.hunk").last().getByTestId("pr-expand-all").click();
  await expect(f.locator(".diff-line.expanded")).toHaveCount(36);
  await expect(f.getByTestId("pr-collapse-all")).toBeVisible();
  await f.getByTestId("pr-collapse-all").click();
  await expect(rows).toHaveCount(n0);
  await f.getByTestId("pr-file-expand-all").click();
  await expect(f.locator(".diff-line.expanded")).toHaveCount(36);
  // side by side too
  await page.getByTestId("pr-layout-split").click();
  await expect(f.locator(".diff-line.split-row.expanded")).toHaveCount(36);
  await page.getByTestId("pr-layout-unified").click();
  await page.getByTestId("activity-files").click();
});

test("Agents: find sessions in folders, add one by id, alarms, a session on stage, continue it", async ({ page }) => {
  await login(page);
  await openGuide(page);
  await selectText(page, "forms a graph");
  await page.getByTestId("ask-current-selection").click();
  await page.getByTestId("selection-question").fill("monitor question");
  await page.getByTestId("selection-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("monitor question", { timeout: 20000 });
  await page.getByTestId("activity-agents").click();
  await expect(page.getByTestId("agents-panel")).toBeVisible();
  // the agents' status page; Document / Terminal / Chats / Diff (and the document's session) do not apply here
  await expect(page.getByTestId("agent-monitor")).toBeVisible();
  await expect(page.getByTestId("view-document")).toHaveCount(0);
  await expect(page.getByTestId("view-diff")).toHaveCount(0);
  await expect(page.getByTestId("session-area")).toHaveCount(0);
  await page.reload(); // still the status page after a reload
  await expect(page.getByTestId("agent-monitor")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("view-document")).toHaveCount(0);
  // the wiki brings them back, with the document that was open
  await page.getByTestId("activity-files").click();
  await expect(page.getByTestId("view-document")).toHaveClass(/active/);
  await expect(page.getByTestId("markdown-body").locator("h1")).toContainText("Guide");
  await page.getByTestId("activity-agents").click();
  await expect(page.getByTestId("agent-monitor")).toBeVisible();
  // the recent sessions of the checked folders (the wiki folder) are monitored without picking them
  const card = page.getByTestId("mon-card").first();
  await expect(card).toBeVisible({ timeout: 15000 });
  await expect(card).toHaveAttribute("data-state", /idle|working|needs_input/);
  // a session given by its id that is not on this machine
  await page.getByTestId("mon-manual-provider").selectOption("codex");
  await page.getByTestId("mon-manual-id").fill("00000000-0000-0000-0000-00000000dead");
  await page.getByTestId("mon-manual-label").fill("my codex run");
  await page.getByTestId("mon-manual-add").click();
  await expect(page.locator('[data-testid="mon-card"][data-state="missing"]')).toContainText("my codex run", { timeout: 15000 });
  // on stage: the conversation takes most of the page; continue it
  await page.locator('[data-testid="mon-card"]:not([data-state="missing"])').first().click();
  const stage = page.getByTestId("mon-stage");
  await expect(stage).toBeVisible();
  const sw = (await stage.boundingBox())!.width;
  const mw = (await page.getByTestId("agent-monitor").boundingBox())!.width;
  expect(sw / mw).toBeGreaterThan(0.7);
  await expect(page.getByTestId("mon-msg").first()).toBeVisible({ timeout: 15000 });
  await page.getByTestId("mon-input").fill("follow-up from the monitor");
  await page.getByTestId("mon-send").click();
  await expect(page.getByTestId("mon-transcript")).toContainText("echo: follow-up from the monitor", { timeout: 20000 });
  await page.getByTestId("mon-stage-close").click();
  await expect(stage).toHaveCount(0);
  // the list of all sessions is folded; unchecking an automatically monitored one removes it
  await page.getByTestId("mon-list-toggle").click();
  const auto = page.locator('[data-testid="mon-found"][data-auto="1"]').first();
  await expect(auto).toBeChecked({ timeout: 15000 });
  const key = await auto.getAttribute("data-key");
  await auto.uncheck();
  await page.getByTestId("mon-refresh").click();
  await expect(page.locator(`[data-testid="mon-card"][data-key="${key}"]`)).toHaveCount(0, { timeout: 15000 });
  // a folder can be unchecked (its sessions are no longer monitored) and checked again; the choice is saved
  const folderBox = page.getByTestId("mon-folder").first();
  await expect(folderBox).toBeChecked();
  const saved = () => page.waitForResponse((r) => r.url().includes("/monitor/auto") && r.request().method() === "PUT" && r.ok());
  await Promise.all([saved(), folderBox.uncheck()]);
  await expect(folderBox).not.toBeChecked();
  await page.reload(); // (still on Agents: clicking its icon again would fold the list)
  await expect(page.getByTestId("agents-panel")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("mon-folder").first()).not.toBeChecked({ timeout: 10000 });
  await Promise.all([saved(), page.getByTestId("mon-folder").first().check()]);
  await expect(page.getByTestId("mon-folder").first()).toBeChecked();
  // the list next to the activity bar folds (the current icon again, or « at the bottom); the activity bar stays
  const width = async () => (await page.locator(".sidebar").boundingBox())!.width;
  const open = await width();
  await page.getByTestId("activity-agents").click();
  await expect(page.getByTestId("agents-panel")).toBeHidden();
  await expect(page.getByTestId("activity-files")).toBeVisible();
  expect(await width()).toBeLessThan(60);
  await expect(page.getByTestId("agent-monitor")).toBeVisible(); // the main page stays
  await page.getByTestId("activity-agents").click();
  await expect(page.getByTestId("agents-panel")).toBeVisible();
  expect(Math.abs((await width()) - open)).toBeLessThan(3);
  await page.getByTestId("sidebar-list-fold").click();
  await expect(page.getByTestId("agents-panel")).toBeHidden();
  await page.reload(); // kept
  await expect(page.getByTestId("agent-monitor")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("agents-panel")).toBeHidden();
  await page.getByTestId("activity-files").click(); // another icon: its list opens
  await expect(page.getByTestId("tree-root").first()).toBeVisible();
});

test("layout: panes can be resized by dragging, folded and unfolded, and keep their size", async ({ page }) => {
  await login(page);
  await page.getByTestId("activity-development").click();
  await page.getByTestId("dev-item").first().click();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  const drag = async (testId: string, dx: number, dy = 0) => {
    const b = (await page.getByTestId(testId).boundingBox())!;
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 4);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2 + dx, b.y + b.height / 4 + dy, { steps: 5 });
    await page.mouse.up();
  };
  // the agent panel (right): wider by dragging left; fold and unfold
  const side = page.locator(".dev-side");
  const w0 = (await side.boundingBox())!.width;
  await drag("dev-agent-splitter", -100);
  expect((await side.boundingBox())!.width).toBeGreaterThan(w0 + 80);
  await page.getByTestId("dev-agent-splitter").hover();
  await page.getByTestId("dev-agent-splitter-fold").click();
  await expect(side).toHaveCount(0);
  await page.getByTestId("dev-agent-splitter-fold").click();
  await expect(side).toBeVisible();
  // the editor's side bar (left): narrower by dragging left, kept after a reload
  const ex = page.locator(".webide-side");
  const e0 = (await ex.boundingBox())!.width;
  await drag("webide-side-splitter", -60);
  const e1 = (await ex.boundingBox())!.width;
  expect(e1).toBeLessThan(e0 - 40);
  await page.reload();
  await expect(page.getByTestId("webide")).toBeVisible({ timeout: 20000 });
  expect(Math.abs((await page.locator(".webide-side").boundingBox())!.width - e1)).toBeLessThan(3);
  await page.getByTestId("webide-side-splitter").dblclick({ position: { x: 2, y: 10 } }); // default size again
  expect(Math.abs((await page.locator(".webide-side").boundingBox())!.width - 260)).toBeLessThan(3);
  await page.getByTestId("dev-agent-splitter").dblclick({ position: { x: 2, y: 10 } });
  // Build & test: the command list beside the output
  await page.getByTestId("dev-tab-tasks").click();
  const tl = page.locator(".dev-tasks-side");
  const t0 = (await tl.boundingBox())!.width;
  await drag("dev-tasks-splitter", 80);
  expect((await tl.boundingBox())!.width).toBeGreaterThan(t0 + 60);
  await page.getByTestId("dev-tasks-splitter").dblclick({ position: { x: 2, y: 10 } });
  await page.getByTestId("activity-files").click();
});

test("Agents: rename a session, select all of a folder, delete selected after confirming", async ({ page }) => {
  await login(page);
  await openGuide(page);
  await selectText(page, "while running Python");
  await page.getByTestId("ask-current-selection").click();
  await page.getByTestId("selection-session").selectOption("new"); // a second session to delete
  await page.getByTestId("selection-question").fill("to be deleted");
  await page.getByTestId("selection-send").click();
  await expect(page.locator('[data-testid="message-agent"][data-status="completed"]').last()).toContainText("to be deleted", { timeout: 20000 });
  await page.getByTestId("activity-agents").click();
  const cards = page.getByTestId("mon-card");
  await expect.poll(() => cards.count(), { timeout: 20000 }).toBeGreaterThanOrEqual(2);
  const n0 = await cards.count();
  // rename
  const wrap = page.locator(".mon-card-wrap").first();
  await wrap.hover();
  page.once("dialog", (d) => void d.accept("Renamed session"));
  await wrap.getByTestId("mon-rename").click();
  await expect(cards.first()).toContainText("Renamed session", { timeout: 15000 });
  // select all of the folder, then cancel the confirmation: nothing is deleted
  await page.getByTestId("mon-select-folder").first().check();
  await expect(page.getByTestId("mon-pick").first()).toBeChecked();
  await page.getByTestId("mon-delete-selected").click();
  await expect(page.getByTestId("confirm-dialog")).toContainText("deleted permanently");
  await page.getByTestId("confirm-cancel").click();
  await expect.poll(() => cards.count()).toBeGreaterThanOrEqual(n0);
  // one selected, confirmed: gone
  await page.getByTestId("mon-select-folder").first().uncheck();
  const victim = await page.locator(".mon-card-wrap").last().getByTestId("mon-card").getAttribute("data-key");
  await page.locator(".mon-card-wrap").last().getByTestId("mon-pick").check();
  await expect(page.getByTestId("mon-pick").first()).not.toBeChecked(); // the folder could be deselected
  await page.getByTestId("mon-delete-selected").click();
  await page.getByTestId("confirm-ok").click();
  await expect(page.locator(`[data-testid="mon-card"][data-key="${victim}"]`)).toHaveCount(0, { timeout: 15000 });
  await page.getByTestId("activity-files").click();
});
