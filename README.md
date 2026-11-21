# LAN Chat Transfer

局域网多人聊天与文件传输课程项目。当前版本使用 **Python / FastAPI + Vue 3 / Vite**，已实现昵称进入、在线成员、公共群聊、一对一私聊、上下线提示、主动退出、断线提示及手动重新进入。

文件传输、聊天记录持久化、心跳和自动重连尚未实现。当前只有一个公共频道和一个私聊视图，无账号验证；在线状态保存在服务内存中，重启即清空。浏览器仅保留当前会话最近500条消息及系统提示，私聊消息同样只存在本次连接中，刷新或重新进入后不恢复。

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
4. 如果本机可访问、其他设备不行，检查是否同网、路由器是否启用访客网络/设备隔离，以及Windows防火墙是否允许该Python程序在专用网络使用8000端口。不要关闭整个防火墙。

这里的IP只是例子，不能直接照抄。本机多标签页和手机尺寸模拟已经验证；**两台真实设备互发消息仍待现场验收**，详见[交接说明中的验证与待验收事项](交接说明.md)。

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

测试会自行启动并停止`127.0.0.1:8765`上的专用后端，无需手动启动服务器；该端口需空闲。脚本打开三个标签页，检查群聊广播、一对一私聊可见性、私聊未读提示、重名、退出、文本安全、多行输入、窄屏布局，以及服务中断和恢复，不自动保存截图。该测试使用仓库根目录的`.venv`和已构建的前端。

## 阅读代码与交接

- `backend/main.py`：应用入口、WebSocket路由、房间和连接管理。
- `frontend/src/useChat.js`：连接与消息状态；`App.vue`和`components/`：页面与组件。
- [交接说明](交接说明.md)：已完成与未完成的工作、测试与待验收事项、必要通信约定、扩展入口和接手流程，与README并列放在项目根目录。

两人按完整功能轮流负责前后端，另一人运行验证并评审PR；各自使用真实账号提交。本轮私聊功能由 fish-salt-jpg 在 `feature/private-chat` 分支实现，仍需组员审阅、合并与双人环境验收，不能将自动测试或代理编写代码视为已完成双人协作评审。
