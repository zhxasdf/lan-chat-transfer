import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import net from "node:net";

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
const errors = [];
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
    { cwd: root, windowsHide: true, stdio: "ignore" },
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
  await start();
  // 使用完整 Chromium 的无头模式，避免额外依赖 headless shell 下载。
  browser = await chromium.launch({ channel: "chromium" });
  const context = await browser.newContext({
    viewport: { width: 1366, height: 900 },
  });
  context.on("page", (page) =>
    page.on("pageerror", (error) => errors.push(error.message)),
  );
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
  assert.deepEqual(errors, []);
  console.log(
    "PASS: 三标签页群聊、一对一私聊可见性与未读提示、重复昵称、退出清理、文本安全、多行输入、移动端布局、服务中断和重新进入。",
  );
} finally {
  await browser?.close();
  await stop();
}
