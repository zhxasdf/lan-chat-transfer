import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import net from "node:net";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";

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
const PASSWORD = "password123";
let server, browser;
let stoppedOwnServer = false;
let downloadDirectory;
const errors = [];
const digest = (value) => createHash("sha256").update(value).digest("hex");
async function until(check, label, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`等待超时: ${label}`);
}
async function start() {
  // Never accidentally test against, or terminate, someone else's server.
  const portDeadline = Date.now() + (stoppedOwnServer ? 10000 : 0);
  while (true) {
    try {
      await new Promise((resolve, reject) => {
        const probe = net.createServer();
        probe.once("error", reject);
        probe.listen(8765, "127.0.0.1", () => probe.close(resolve));
      });
      break;
    } catch (error) {
      if (error.code !== "EADDRINUSE" || Date.now() >= portDeadline)
        throw error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  stoppedOwnServer = false;
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
        LAN_CHAT_DB: path.join(downloadDirectory, "chat.db"),
        LAN_CHAT_FILES: path.join(downloadDirectory, "files"),
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
  stoppedOwnServer = true;
}
function tokenOf(page) {
  return page.evaluate(() => localStorage.getItem("lan-chat-token"));
}
async function register(page, username) {
  await page.goto(url);
  await page.getByRole("button", { name: /还没有账号/ }).click();
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: /^注册并登录/ }).click();
  await page.getByRole("button", { name: "退出", exact: true }).waitFor();
}
async function login(page, username) {
  await page.goto(url);
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByRole("button", { name: /^登录/ }).click();
  await page.getByRole("button", { name: "退出", exact: true }).waitFor();
}
const testRoot = path.join(root, ".test-tmp");
try {
  // Keep every test artifact inside the workspace; system TEMP is not writable here.
  await mkdir(testRoot, { recursive: true });
  downloadDirectory = await mkdtemp(path.join(testRoot, "lan-chat-test-"));
  await start();
  // 使用完整 Chromium 的无头模式，避免额外依赖 headless shell 下载。
  browser = await chromium.launch({ channel: "chromium" });
  // Separate browser contexts so each tab keeps its own login session.
  async function newPage() {
    const context = await browser.newContext({
      viewport: { width: 1366, height: 900 },
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    return page;
  }
  const a = await newPage(),
    b = await newPage(),
    c = await newPage();
  const api = a.context().request;
  await register(a, "张三三");
  await register(b, "李四四");
  await register(c, "王五五");
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
  // 私聊：张三三点李四四发起，只有双方能看到。
  await a.getByRole("button", { name: "与 李四四 私聊" }).click();
  await until(
    async () =>
      (await a.getByRole("heading", { name: "与 李四四 私聊" }).count()) === 1,
    "进入私聊视图",
  );
  assert.equal(await a.locator(".bubble").count(), 0);
  await a.getByLabel("消息内容").fill("只给李四四看的私聊");
  await a.getByLabel("消息内容").press("Enter");
  await until(
    async () => (await a.locator(".bubble").count()) === 1,
    "私聊已发出",
  );
  await until(
    async () => (await b.locator(".user-list .unread-badge").count()) === 1,
    "李四四收到未读提示",
  );
  assert.equal(await c.locator(".bubble").count(), 3);
  await b.getByRole("button", { name: "与 张三三 私聊" }).click();
  await until(
    async () => (await b.locator(".bubble").count()) === 1,
    "李四四收到私聊",
  );
  assert.match(await b.locator(".bubble").innerText(), /只给李四四看的私聊/);
  assert.equal(await c.locator(".bubble").count(), 3);
  await b.getByRole("button", { name: "公共频道" }).click();
  await until(
    async () => (await b.locator(".bubble").count()) === 3,
    "切回群聊",
  );
  // Real HTTP upload, WebSocket file notification and browser download.
  await a.getByRole("button", { name: "公共频道" }).click();
  const publicBytes = Buffer.from("群聊文件内容\nhello file 🙂\n");
  await b.getByLabel("选择文件").setInputFiles({
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

  await a.getByRole("button", { name: "与 李四四 私聊" }).click();
  // Delay only the HTTP request: switching views must not retarget the upload.
  let finishUpload, uploadStarted;
  const ready = new Promise((resolve) => {
    uploadStarted = resolve;
  });
  const release = new Promise((resolve) => {
    finishUpload = resolve;
  });
  const privateBytes = Buffer.from("只给李四四的文件\x00\x01");
  await a.route(
    "**/api/files?**",
    async (route) => {
      uploadStarted();
      await release;
      await route.continue();
    },
    { times: 1 },
  );
  await a.getByLabel("选择文件").setInputFiles({
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
    async () => (await b.locator(".user-list .unread-badge").count()) === 1,
    "私聊文件未读提示",
  );
  assert.equal(await a.locator(".file-card").count(), 1);
  assert.equal(await c.locator(".file-card").count(), 1);
  await a.getByRole("button", { name: "与 李四四 私聊" }).click();
  await b.getByRole("button", { name: "与 张三三 私聊" }).click();
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
  const unauthorized = await api.get(`${url}/api/files/${privateId}`, {
    headers: { Authorization: `Bearer ${await tokenOf(c)}` },
  });
  assert.equal(unauthorized.status(), 403);
  assert.equal((await api.get(`${url}/api/files/${publicId}`)).status(), 401);

  // Oversized selection is rejected before an HTTP transfer is started.
  await a.getByLabel("选择文件").setInputFiles({
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
  await a.getByLabel("选择文件").setInputFiles({
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
  // 刷新页面后，登录态与公共/私聊历史都应恢复。
  await a.reload();
  await a.getByRole("button", { name: "退出", exact: true }).waitFor();
  await until(
    async () => (await a.locator(".bubble").count()) === 3,
    "刷新后公共历史恢复",
  );
  assert.equal(await a.locator(".file-card").count(), 1);
  await a.getByRole("button", { name: "与 李四四 私聊" }).click();
  await until(
    async () => (await a.locator(".bubble").count()) === 1,
    "刷新后私聊历史恢复",
  );
  assert.equal(await a.locator(".file-card").count(), 1);
  await a.getByRole("button", { name: "公共频道" }).click();
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
  const duplicate = await newPage();
  await duplicate.goto(url);
  await duplicate.getByRole("button", { name: /还没有账号/ }).click();
  await duplicate.getByLabel("用户名").fill("张三三");
  await duplicate.getByLabel("密码").fill(PASSWORD);
  await duplicate.getByRole("button", { name: /^注册并登录/ }).click();
  await duplicate.getByRole("alert").waitFor();
  assert.match(
    await duplicate.getByRole("alert").innerText(),
    /用户名已被使用/,
  );
  await duplicate.close();
  await b.getByRole("button", { name: "退出", exact: true }).click();
  await b.getByLabel("用户名").waitFor();
  await until(
    async () => (await a.locator(".user-list li").count()) === 1,
    "主动退出",
  );
  await stop();
  await until(
    async () => /重连|断开/.test(await a.locator(".connection").innerText()),
    "服务中断提示",
  );
  assert.equal(await a.getByLabel("消息内容").isDisabled(), true);
  await start();
  await until(
    async () => (await a.locator(".connection").innerText()) === "已连接",
    "自动重连",
    30000,
  );
  await until(
    async () => (await a.locator(".bubble").count()) >= 3,
    "自动重连后恢复历史",
    30000,
  );
  assert.equal(await a.locator(".file-card").count(), 1);
  const persisted = await api.get(`${url}/api/files/${publicId}`, {
    headers: { Authorization: `Bearer ${await tokenOf(a)}` },
  });
  assert.equal(persisted.status(), 200);
  assert.equal(digest(await persisted.body()), digest(publicBytes));
  const privatePersisted = await api.get(`${url}/api/files/${privateId}`, {
    headers: { Authorization: `Bearer ${await tokenOf(a)}` },
  });
  assert.equal(privatePersisted.status(), 200);
  assert.equal(digest(await privatePersisted.body()), digest(privateBytes));
  // Tabs in one browser share the login, but represent only one online account.
  const sibling = await a.context().newPage();
  sibling.on("pageerror", (error) => errors.push(error.message));
  await sibling.goto(url);
  await sibling.getByRole("button", { name: "退出", exact: true }).waitFor();
  await until(
    async () => (await sibling.locator(".user-list li").count()) === 1,
    "同账号去重",
  );
  assert.equal(
    await sibling.locator(".user-list button").first().isDisabled(),
    true,
  );
  await login(b, "李四四");
  await until(
    async () => (await sibling.locator(".user-list li").count()) === 2,
    "两账号在线",
  );
  const bSibling = await b.context().newPage();
  bSibling.on("pageerror", (error) => errors.push(error.message));
  await bSibling.goto(url);
  await bSibling.getByRole("button", { name: "退出", exact: true }).waitFor();
  await sibling.getByLabel("消息内容").fill("多标签群聊同步");
  await sibling.getByRole("button", { name: /发送消息/ }).click();
  for (const page of [a, sibling, b, bSibling]) {
    await page
      .locator(".bubble")
      .filter({ hasText: "多标签群聊同步" })
      .waitFor();
    assert.equal(await page.locator(".user-list li").count(), 2);
    await page
      .getByRole("button", {
        name:
          page === a || page === sibling ? "与 李四四 私聊" : "与 张三三 私聊",
      })
      .click();
  }
  await sibling.getByLabel("消息内容").fill("多标签私聊同步");
  await sibling.getByRole("button", { name: /发送消息/ }).click();
  for (const page of [a, sibling, b, bSibling])
    await page
      .locator(".bubble")
      .filter({ hasText: "多标签私聊同步" })
      .waitFor();
  await sibling.getByLabel("选择文件").setInputFiles({
    name: "tabs-sync.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("tabs sync"),
  });
  for (const page of [a, sibling, b, bSibling])
    await page
      .locator(".file-card")
      .filter({ hasText: "tabs-sync.txt" })
      .waitFor();
  await bSibling.close();
  await a.close();
  await sibling.getByLabel("消息内容").fill("关闭原标签仍可私聊");
  await sibling.getByRole("button", { name: /发送消息/ }).click();
  await b
    .locator(".bubble")
    .filter({ hasText: "关闭原标签仍可私聊" })
    .waitFor();
  assert.equal(await b.locator(".user-list li").count(), 2);
  const another = await sibling.context().newPage();
  await another.goto(url);
  await another.getByRole("button", { name: "退出", exact: true }).waitFor();
  await sibling.getByRole("button", { name: "退出", exact: true }).click();
  await sibling.getByLabel("用户名").waitFor();
  await another.getByLabel("用户名").waitFor();
  await until(
    async () => (await b.locator(".user-list li").count()) === 1,
    "所有共享会话退出",
  );
  // Login changes also propagate to already-open tabs of the same browser.
  await login(sibling, "王五五");
  await another.getByRole("button", { name: "退出", exact: true }).waitFor();
  await until(
    async () => (await another.locator(".user-list li").count()) === 2,
    "跨标签登录同步",
  );
  assert.equal(await tokenOf(sibling), await tokenOf(another));
  // A registered offline account can receive text; unread state survives restart.
  const sender = await newPage(),
    receiver = await newPage();
  await register(sender, "离线发送者");
  await register(receiver, "离线接收者");
  await receiver.getByRole("button", { name: "退出", exact: true }).click();
  await receiver.getByLabel("用户名").waitFor();
  await until(
    async () =>
      (await sender
        .getByRole("button", { name: "与 离线接收者 私聊" })
        .count()) === 0,
    "接收者离线",
  );
  await sender.getByLabel("发起私聊").fill("离线接收者");
  await sender.getByRole("button", { name: "查找账号", exact: true }).click();
  await sender.getByRole("heading", { name: "与 离线接收者 私聊" }).waitFor();
  assert.equal(
    await sender.getByRole("button", { name: "＋ 发送文件" }).isDisabled(),
    false,
  );
  await sender.getByLabel("消息内容").fill("离线留言-0");
  await sender.getByLabel("消息内容").press("Enter");
  await sender.getByText("对方离线，消息已保存", { exact: true }).waitFor();
  await sender.locator(".bubble").filter({ hasText: "离线留言-0" }).waitFor();
  const lookup = await api.get(
    `${url}/api/users/lookup?username=${encodeURIComponent("离线接收者")}`,
    {
      headers: { Authorization: `Bearer ${await tokenOf(sender)}` },
    },
  );
  const targetId = (await lookup.json()).user.id;
  // Exercise real WebSocket delivery for a backlog larger than one history page.
  await sender.evaluate(
    ({ token, target }) =>
      new Promise((resolve, reject) => {
        const ws = new WebSocket(`ws://${location.host}/ws`);
        const deadline = setTimeout(() => {
          ws.close();
          reject(new Error("backlog timeout"));
        }, 15000);
        let index = 1;
        ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token }));
        ws.onerror = () => {
          clearTimeout(deadline);
          ws.close();
          reject(new Error("backlog socket failed"));
        };
        ws.onmessage = ({ data }) => {
          const event = JSON.parse(data);
          if (event.type === "welcome" || event.type === "private") {
            if (index === 55) {
              clearTimeout(deadline);
              ws.close();
              resolve();
            } else
              ws.send(
                JSON.stringify({
                  type: "private",
                  to: target,
                  text: `离线留言-${index++}`,
                }),
              );
          }
        };
      }),
    { token: await tokenOf(sender), target: targetId },
  );
  const offlineBytes = Buffer.from("offline file survives restart\x00🙂");
  await sender.getByLabel("选择文件").setInputFiles({
    name: "offline-persist.bin",
    mimeType: "application/octet-stream",
    buffer: offlineBytes,
  });
  const offlineCard = sender
    .locator(".file-card")
    .filter({ hasText: "offline-persist.bin" });
  await offlineCard.waitFor();
  const offlineId = await offlineCard.getAttribute("data-file-id");
  await stop();
  await start();
  await login(receiver, "离线接收者");
  await until(
    async () =>
      (await receiver.locator(".recent-list .unread-badge").innerText()) ===
      "56",
    "重启后未读保留",
  );
  await receiver.reload();
  await until(
    async () =>
      (await receiver.locator(".recent-list .unread-badge").innerText()) ===
      "56",
    "刷新不清未读",
  );
  const receiverTab = await receiver.context().newPage();
  await receiverTab.goto(url);
  await until(
    async () =>
      (await receiverTab.locator(".recent-list .unread-badge").innerText()) ===
      "56",
    "新标签未读一致",
  );
  await receiver.bringToFront();
  await receiver
    .getByRole("button", { name: "打开与 离线发送者 的会话" })
    .click();
  await until(
    async () => (await receiver.locator(".bubble").count()) === 50,
    "先显示最早50条未读",
  );
  assert.equal(
    await receiver.locator(".bubble").first().innerText(),
    "离线留言-0",
  );
  await until(
    async () =>
      (await receiverTab.locator(".recent-list .unread-badge").innerText()) ===
      "6",
    "未展示的5条文字和文件保留未读",
  );
  await receiver
    .getByRole("button", { name: "加载后续消息", exact: true })
    .click();
  await until(
    async () => (await receiver.locator(".bubble").count()) === 55,
    "加载其余离线消息",
  );
  await until(
    async () =>
      (await receiverTab.locator(".recent-list .unread-badge").count()) === 0,
    "跨标签已读同步",
  );
  const messageIds = await receiver.locator(".bubble").allTextContents();
  assert.equal(new Set(messageIds).size, 55);
  const receivedCard = receiver
    .locator(".file-card")
    .filter({ hasText: "offline-persist.bin" });
  await receivedCard.waitFor();
  const offlineDownload = receiver.waitForEvent("download");
  await receivedCard
    .getByRole("button", { name: "下载 offline-persist.bin" })
    .click();
  const offlinePath = path.join(downloadDirectory, "offline-received.bin");
  await (await offlineDownload).saveAs(offlinePath);
  assert.equal(digest(await readFile(offlinePath)), digest(offlineBytes));
  assert.equal(
    (
      await api.get(`${url}/api/files/${offlineId}`, {
        headers: { Authorization: `Bearer ${await tokenOf(b)}` },
      })
    ).status(),
    403,
  );
  assert.equal(
    (
      await api.delete(`${url}/api/files/${offlineId}`, {
        headers: { Authorization: `Bearer ${await tokenOf(receiver)}` },
      })
    ).status(),
    403,
  );
  await until(
    async () => (await sender.locator(".connection").innerText()) === "已连接",
    "离线测试自动重连",
    30000,
  );
  await sender
    .getByRole("button", { name: "打开与 离线接收者 的会话" })
    .click();
  await offlineCard.waitFor();
  sender.once("dialog", (dialog) => dialog.accept());
  await offlineCard
    .getByRole("button", { name: "删除 offline-persist.bin" })
    .click();
  await until(
    async () => (await receivedCard.innerText()).includes("文件已删除"),
    "私聊删除状态同步",
  );
  assert.equal(
    await receivedCard
      .getByRole("button", { name: "下载 offline-persist.bin" })
      .isDisabled(),
    true,
  );
  assert.equal(
    (
      await api.get(`${url}/api/files/${offlineId}`, {
        headers: { Authorization: `Bearer ${await tokenOf(receiver)}` },
      })
    ).status(),
    404,
  );
  await receiver.reload();
  await receiver.getByRole("button", { name: "退出", exact: true }).waitFor();
  await until(
    async () => (await receiver.locator(".recent-list button").count()) === 1,
    "最近会话恢复",
  );
  assert.equal(await receiver.locator(".recent-list .unread-badge").count(), 0);
  await receiver
    .getByRole("button", { name: "打开与 离线发送者 的会话" })
    .click();
  await until(
    async () => (await receivedCard.innerText()).includes("文件已删除"),
    "刷新保留删除状态",
  );
  assert.deepEqual(errors, []);
  console.log(
    "PASS: 原有聊天、账号和多标签页验证；离线文字及文件未读分页；公私文件重启后内容一致、离线文件上线下载、第三人拒绝、发送者删除及状态同步；服务中断后自动重连并恢复历史。",
  );
} finally {
  await browser?.close();
  await stop();
  if (downloadDirectory) {
    const resolved = path.resolve(downloadDirectory);
    if (
      path.dirname(resolved) !== path.resolve(testRoot) ||
      !path.basename(resolved).startsWith("lan-chat-test-")
    ) {
      throw new Error("拒绝清理临时目录之外的路径");
    }
    await rm(resolved, { recursive: true, force: true });
  }
}
