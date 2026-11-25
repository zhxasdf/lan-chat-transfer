import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import net from "node:net";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";

const root = fileURLToPath(new URL("../../", import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH ||= path.join(
  root,
  "frontend/.browser-cache",
);
const { chromium } = await import("playwright");
const python =
  process.platform === "win32"
    ? path.join(root, ".venv/Scripts/python.exe")
    : path.join(root, ".venv/bin/python");
const url = "http://127.0.0.1:8765";
let server, browser;
let downloadDirectory;
const errors = [];
const identities = new Map();
const digest = (value) => createHash("sha256").update(value).digest("hex");
async function until(check, label) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`等待超时: ${label}`);
}
async function start() {
  // Never accidentally test against, or terminate, someone else's server.
  await new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(8765, "127.0.0.1", () => probe.close(resolve));
  });
  server = spawn(
    python,
    [
      "-m",
      "uvicorn",
      "backend.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      "8765",
    ],
    {
      cwd: root,
      windowsHide: true,
      stdio: "ignore",
      env: {
        ...process.env,
        TMP: downloadDirectory,
        TEMP: downloadDirectory,
        TMPDIR: downloadDirectory,
      },
    },
  );
  server.on("error", (error) => errors.push(error.message));
  await until(async () => {
    try {
      return (await fetch(`${url}/api/health`)).ok;
    } catch {
      return false;
    }
  }, "服务启动");
}
async function stop() {
  if (
    !server ||
    server.exitCode !== null ||
    server.signalCode !== null ||
    !server.pid
  )
    return;
  const exited = new Promise((resolve) => server.once("exit", resolve));
  server.kill();
  await exited;
}
async function enter(page, nickname) {
  await page.goto(url);
  await page.getByLabel("你的昵称").fill(nickname);
  await page.getByRole("button", { name: "进入聊天室" }).click();
  await page.getByRole("button", { name: "退出", exact: true }).waitFor();
}
try {
  // Keep files left by the deliberately killed test server inside our own directory.
  downloadDirectory = await mkdtemp(path.join(tmpdir(), "lan-chat-test-"));
  await start();
  // 使用完整 Chromium 的无头模式，避免额外依赖 headless shell 下载。
  browser = await chromium.launch({ channel: "chromium" });
  const context = await browser.newContext({
    viewport: { width: 1366, height: 900 },
  });
  context.on("page", (page) => {
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("websocket", (socket) =>
      socket.on("framereceived", ({ payload }) => {
        const event = JSON.parse(String(payload));
        if (event.type === "welcome") identities.set(page, event);
      }),
    );
  });
  const a = await context.newPage(),
    b = await context.newPage(),
    c = await context.newPage();
  await a.goto(url);
  await enter(a, "张三");
  await enter(b, "李四");
  await enter(c, "王五");
  for (const page of [a, b, c])
    await until(
      async () => (await page.locator(".user-list li").count()) === 3,
      "三人在线",
    );
  await a.getByLabel("消息内容").fill("大家好！这是我们的第一版群聊 🙂");
  await a.getByLabel("消息内容").press("Enter");
  for (const page of [a, b, c])
    await until(
      async () => (await page.locator(".bubble").count()) === 1,
      "广播只出现一次",
    );
  await b.getByLabel("消息内容").fill("收到！在线列表和消息同步正常。");
  await b.getByRole("button", { name: "发送消息" }).click();
  await until(
    async () => (await a.locator(".bubble").count()) === 2,
    "第二条消息",
  );
  await c
    .getByLabel("消息内容")
    .fill("<script>window.testInjected=true</script>\n多行消息");
  await c.getByLabel("消息内容").press("Shift+Enter");
  assert.equal(await c.locator(".bubble").count(), 2);
  await c.getByLabel("消息内容").press("Enter");
  await until(
    async () => (await a.locator(".bubble").count()) === 3,
    "多行消息",
  );
  assert.equal(await a.evaluate(() => window.testInjected), undefined);
  // 私聊：张三点李四发起，只有张三和李四能看到。
  await a.getByRole("button", { name: "与 李四 私聊" }).click();
  await until(
    async () =>
      (await a.getByRole("heading", { name: "与 李四 私聊" }).count()) === 1,
    "进入私聊视图",
  );
  assert.equal(await a.locator(".bubble").count(), 0);
  await a.getByLabel("消息内容").fill("只给李四看的私聊");
  await a.getByLabel("消息内容").press("Enter");
  await until(
    async () => (await a.locator(".bubble").count()) === 1,
    "私聊已发出",
  );
  await until(
    async () => (await b.locator(".unread-badge").count()) === 1,
    "李四收到未读提示",
  );
  assert.equal(await c.locator(".bubble").count(), 3);
  await b.getByRole("button", { name: "与 张三 私聊" }).click();
  await until(
    async () => (await b.locator(".bubble").count()) === 1,
    "李四收到私聊",
  );
  assert.match(await b.locator(".bubble").innerText(), /只给李四看的私聊/);
  assert.equal(await c.locator(".bubble").count(), 3);
  await b.getByRole("button", { name: "公共频道" }).click();
  await until(
    async () => (await b.locator(".bubble").count()) === 3,
    "切回群聊",
  );
  // Real HTTP upload, WebSocket file notification and browser download.
  await a.getByRole("button", { name: "公共频道" }).click();
  const publicBytes = Buffer.from("群聊文件内容\nhello file 🙂\n");
  await b
    .getByLabel("选择文件")
    .setInputFiles({
      name: "课程资料.txt",
      mimeType: "text/plain",
      buffer: publicBytes,
    });
  for (const page of [a, b, c])
    await until(
      async () => (await page.locator(".file-card").count()) === 1,
      "群聊文件卡片",
    );
  const publicCard = c.locator(".file-card").first();
  const publicId = await publicCard.getAttribute("data-file-id");
  const publicDownloadEvent = c.waitForEvent("download");
  await publicCard.getByRole("button", { name: "下载 课程资料.txt" }).click();
  const publicDownload = await publicDownloadEvent;
  assert.equal(publicDownload.suggestedFilename(), "课程资料.txt");
  const publicPath = path.join(downloadDirectory, "public.txt");
  await publicDownload.saveAs(publicPath);
  assert.equal(digest(await readFile(publicPath)), digest(publicBytes));

  await a.getByRole("button", { name: "与 李四 私聊" }).click();
  // Delay only the HTTP request: switching views must not retarget the upload.
  let finishUpload, uploadStarted;
  const ready = new Promise((resolve) => {
    uploadStarted = resolve;
  });
  const release = new Promise((resolve) => {
    finishUpload = resolve;
  });
  const privateBytes = Buffer.from("只给李四的文件\x00\x01");
  await a.route(
    "**/api/files?**",
    async (route) => {
      uploadStarted();
      await release;
      await route.continue();
    },
    { times: 1 },
  );
  await a
    .getByLabel("选择文件")
    .setInputFiles({
      name: "私聊资料.bin",
      mimeType: "application/octet-stream",
      buffer: privateBytes,
    });
  await ready;
  await a.getByRole("button", { name: "公共频道" }).click();
  finishUpload();
  await until(
    async () => (await a.locator(".upload-status").count()) === 0,
    "上传完成",
  );
  await until(
    async () => (await b.locator(".unread-badge").count()) === 1,
    "私聊文件未读提示",
  );
  assert.equal(await a.locator(".file-card").count(), 1);
  assert.equal(await c.locator(".file-card").count(), 1);
  await a.getByRole("button", { name: "与 李四 私聊" }).click();
  await b.getByRole("button", { name: "与 张三 私聊" }).click();
  for (const page of [a, b])
    await until(
      async () => (await page.locator(".file-card").count()) === 1,
      "私聊文件卡片",
    );
  const privateCard = b.locator(".file-card");
  const privateId = await privateCard.getAttribute("data-file-id");
  const privateDownloadEvent = b.waitForEvent("download");
  await privateCard.getByRole("button", { name: "下载 私聊资料.bin" }).click();
  const privatePath = path.join(downloadDirectory, "private.bin");
  await (await privateDownloadEvent).saveAs(privatePath);
  assert.equal(digest(await readFile(privatePath)), digest(privateBytes));
  const unauthorized = await context.request.get(
    `${url}/api/files/${privateId}`,
    { headers: { Authorization: `Bearer ${identities.get(c).token}` } },
  );
  assert.equal(unauthorized.status(), 403);
  assert.equal(
    (await context.request.get(`${url}/api/files/${publicId}`)).status(),
    401,
  );

  // Oversized selection is rejected before an HTTP transfer is started.
  await a
    .getByLabel("选择文件")
    .setInputFiles({
      name: "too-large.bin",
      mimeType: "application/octet-stream",
      buffer: Buffer.alloc(20 * 1024 * 1024 + 1),
    });
  await until(
    async () => (await a.getByRole("alert").innerText()).includes("20 MiB"),
    "超限提示",
  );
  assert.equal(await a.locator(".upload-status").count(), 0);

  // Cancel a request held before it reaches the backend; no file message is emitted.
  let heldRoute, notifyCancel;
  const cancelReady = new Promise((resolve) => {
    notifyCancel = resolve;
  });
  await a.route(
    "**/api/files?**",
    (route) => {
      heldRoute = route;
      notifyCancel();
    },
    { times: 1 },
  );
  await a
    .getByLabel("选择文件")
    .setInputFiles({
      name: "cancelled.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("cancel me"),
    });
  await cancelReady;
  await a.getByRole("button", { name: "取消上传" }).click();
  await heldRoute.abort().catch(() => {});
  await until(
    async () => (await a.locator(".upload-status").count()) === 0,
    "取消上传",
  );
  assert.equal(await a.locator(".file-card").count(), 1);
  await c.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await c.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await c.getByRole("button", { name: "在线成员" }).click();
  await c.close();
  await until(
    async () => (await a.locator(".user-list li").count()) === 2,
    "关闭页面清理",
  );
  const duplicate = await context.newPage();
  await duplicate.goto(url);
  await duplicate.getByLabel("你的昵称").fill("张三");
  await duplicate.getByRole("button", { name: "进入聊天室" }).click();
  await duplicate.getByRole("alert").waitFor();
  assert.match(await duplicate.getByRole("alert").innerText(), /昵称已被使用/);
  await duplicate.close();
  await b.getByRole("button", { name: "退出", exact: true }).click();
  await until(
    async () => (await a.locator(".user-list li").count()) === 1,
    "主动退出",
  );
  await stop();
  await until(
    async () => (await a.locator(".connection").innerText()) === "连接已断开",
    "服务中断提示",
  );
  assert.equal(await a.getByLabel("消息内容").isDisabled(), true);
  await start();
  await a.getByRole("button", { name: "重新进入" }).click();
  await until(
    async () => (await a.locator(".connection").innerText()) === "已连接",
    "手动重新进入",
  );
  assert.equal(await a.locator(".bubble").count(), 0);
  const expired = await context.request.get(`${url}/api/files/${publicId}`, {
    headers: { Authorization: `Bearer ${identities.get(a).token}` },
  });
  assert.equal(expired.status(), 404);
  assert.deepEqual(errors, []);
  console.log(
    "PASS: 群聊/私聊、文件上传下载与内容校验、第三人下载拒绝、上传切换会话、超限与取消、未读提示、退出清理、文本安全、窄屏、服务重启及文件失效。",
  );
} finally {
  await browser?.close();
  await stop();
  if (downloadDirectory) {
    const resolved = path.resolve(downloadDirectory);
    if (
      path.dirname(resolved) !== path.resolve(tmpdir()) ||
      !path.basename(resolved).startsWith("lan-chat-test-")
    ) {
      throw new Error("拒绝清理临时目录之外的路径");
    }
    await rm(resolved, { recursive: true, force: true });
  }
}
