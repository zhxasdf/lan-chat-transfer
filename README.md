# LAN Chat Transfer

局域网多人聊天与文件传输课程项目。当前版本使用 **Python / FastAPI + Vue 3 / Vite**，已实现昵称进入、在线成员、公共群聊、一对一私聊、群聊与私聊文件传输、上下线提示、主动退出、断线提示及手动重新进入。

注册登录、离线消息、敏感词检测、聊天记录持久化、应用层心跳和自动重连均为后续必做功能，目前尚未实现。当前只有一个公共频道和一个私聊视图，无账号登录；HTTP文件操作使用临时连接凭证。在线状态保存在服务内存中，重启即清空。浏览器仅保留当前会话最近500条消息及系统提示，刷新或重新进入后不恢复。

## 环境与安装（Windows PowerShell）

使用Python 3.11和Node.js 24.12以上。已在Python 3.11.9、Node 24.22.1、npm 11.19.1上验证。以下命令均从仓库根目录开始执行；使用`npm.cmd`可避免PowerShell阻止`npm.ps1`。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
Set-Location frontend
npm.cmd ci --cache .npm-cache
Set-Location ..
```

后端主依赖固定在`backend/requirements.txt`，测试依赖固定在`backend/requirements-dev.txt`；完整的本机已验证依赖快照见`backend/requirements-lock.txt`。需要复现快照时，用`-r backend/requirements-lock.txt`替换上述后端安装参数。若默认Python源连接失败，可追加`--index-url https://pypi.org/simple --timeout 15`重试。

## 快速运行：构建后只启动一个服务

```powershell
Set-Location frontend
npm.cmd run build
Set-Location ..
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

本机打开 <http://127.0.0.1:8000>。同时打开三个标签页，分别输入“张三”“李四”“王五”，即可模拟多人聊天；每个标签页是独立用户。点击“在线成员”里的其他成员可发起一对一私聊，点“← 公共频道”切回群聊。使用Ctrl+C停止服务。

## 发送和下载文件

- 在公共频道或私聊中点击“＋ 发送文件”，选择一个文件后自动上传，最大 **20 MiB（20,971,520字节）**，允许空文件。一次上传一个文件，支持进度和取消；文字聊天可以继续使用。
- 上传目标固定为选择文件时所在的会话，之后切换频道不会改变目标。上传完成后通过聊天消息出现文件卡片，点击“下载”保存；卡片出现表示服务器已接收文件，不是对方已下载的回执。
- 公共频道文件允许当前在线用户下载；私聊文件只有原发送者与接收者的有效连接能下载，不能只凭文件ID访问。退出或刷新后身份改变，无法继续下载旧的私聊文件。
- 每次服务运行最多保存256 MiB文件，上传会预留一个20 MiB配额，空间不足时提示错误，取消和失败释放配额。文件保存在系统临时目录，正常停止或热重载时清理，重启后旧文件失效；强制结束进程可能留下临时文件，但重启后仍不可通过接口访问。
- 不做文件预览、断点续传、离线传输或自动重试。上传失败后可重新选择文件；取消时若卡片已经出现，表示服务端已完成发送。现阶段仍只面向可信局域网，不提供HTTPS或账号级身份保护。

先构建前端、再启动Python服务。如果首次构建时Python已经在运行，构建完成后重新启动Python，使静态资源挂载生效。不要添加多个worker：当前在线连接只在单进程内共享。

## 开发：两个终端

终端一，在仓库根目录：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

终端二，在仓库根目录：

```powershell
Set-Location frontend
npm.cmd run dev
```

开发页面打开 <http://localhost:5173>。Vite将`/api`和`/ws`代理给8000端口。后端热重载会中断聊天连接，页面提示断线后点击“重新进入”。

## 局域网访问和双机验收

1. 按“快速运行”构建并启动服务，确保监听`0.0.0.0:8000`。
2. 用`ipconfig`查看服务器当前Wi-Fi或以太网的IPv4地址，例如`192.168.1.20`。
3. 同一局域网的手机或另一台电脑打开`http://192.168.1.20:8000`，使用不同昵称聊天。
4. 如果本机可访问、其他设备不行，检查是否同网、路由器是否启用访客网络/设备隔离，以及Windows防火墙是否允许实际Python进程通过当前网络类型使用8000端口。不要关闭整个防火墙。

这里的IP只是例子，不能直接照抄。用户已反馈手机与电脑在个人热点下聊天成功，并于2026年11月21日确认完成双设备群聊、私聊文件互传及下载内容核对。校园Wi-Fi访问超时，具体原因未最终确认。详细验证范围及剩余事项见[交接说明](交接说明.md)。

## 测试

在仓库根目录运行后端测试：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
.\.venv\Scripts\python.exe -m pip check
```

浏览器测试首次需要安装Chromium，在仓库根目录执行：

```powershell
Set-Location frontend
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD/.browser-cache"
npm.cmd exec -- playwright install chromium
npm.cmd run build
npm.cmd run test:browser
Set-Location ..
```

测试会自行启动并停止`127.0.0.1:8765`上的专用后端，无需手动启动服务器；该端口需空闲。脚本打开三个标签页，检查群聊、私聊、未读提示、文件上传下载及字节一致性、第三人下载拒绝、切换会话、超限和取消，以及重名、退出、文本安全、窄屏和服务重启。测试不自动保存截图，文件在测试专用临时目录中生成并清理。该测试使用仓库根目录的`.venv`和已构建的前端。

## 阅读代码与交接

- `backend/main.py`：应用入口、WebSocket路由、房间和连接管理。
- `backend/files.py`：临时文件存储、容量管理、上传下载与连接凭证校验。
- `frontend/src/useChat.js`：连接与消息状态；`App.vue`和`components/`：页面与组件。
- [交接说明](交接说明.md)：已完成与未完成的工作、测试与待验收事项、必要通信约定、扩展入口和接手流程，与README并列放在项目根目录。

两人按完整功能轮流负责前后端，另一人运行验证并评审PR；各自使用真实账号提交。首轮群聊和第二轮私聊分别经PR #1、#2合并。本轮文件传输已提交并推送至`feature/file-transfer`，已创建[PR #3](https://github.com/zhxasdf/lan-chat-transfer/pull/3)，待评审和合并；计划由fish-salt-jpg评审，需在PR的Reviewers中正式请求对方评审。自动测试或文档中的分工不代表已发出评审请求或评审已通过。
