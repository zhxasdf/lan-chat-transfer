import { ref, onBeforeUnmount } from "vue";
import { MAX_FILE_SIZE, uploadFile, downloadFile } from "./fileTransfer";

const TOKEN_KEY = "lan-chat-token";

export function useChat() {
  const status = ref("idle"),
    account = ref(null),
    user = ref(null),
    users = ref([]),
    messages = ref([]),
    error = ref(""),
    activePeer = ref(null),
    unread = ref({}),
    upload = ref(null),
    downloading = ref({});
  let socket = null;
  let timer = null;
  let token = null;
  let pendingUpload = null;
  const downloads = new Set();
  function saveToken(value) {
    token = value;
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  }
  function clearSession() {
    saveToken(null);
    account.value = null;
    user.value = null;
  }
  function cancelUpload() {
    const pending = pendingUpload;
    pendingUpload = null;
    upload.value = null;
    pending?.cancel();
  }
  function stopTransfers() {
    cancelUpload();
    for (const controller of downloads) controller.abort();
    downloads.clear();
    downloading.value = {};
  }
  function clearState() {
    users.value = [];
    status.value = "idle";
    user.value = null;
    messages.value = [];
    activePeer.value = null;
    unread.value = {};
  }
  function disconnect() {
    stopTransfers();
    clearTimeout(timer);
    const previous = socket;
    socket = null;
    previous?.close();
    clearState();
    error.value = "";
  }
  function peerOf(event) {
    if (!user.value) return null;
    return event.sender.id === user.value.id ? event.recipient : event.sender;
  }
  function connect() {
    if (!token) return;
    disconnect();
    status.value = "connecting";
    const current = new WebSocket(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`,
    );
    socket = current;
    timer = setTimeout(() => {
      if (socket === current && status.value === "connecting") {
        error.value = "连接超时，请检查服务器后重试";
        current.close();
        status.value = "disconnected";
      }
    }, 10000);
    current.onopen = () => {
      if (socket === current)
        current.send(JSON.stringify({ type: "auth", token }));
    };
    current.onmessage = ({ data }) => {
      if (socket !== current) return;
      let event;
      try {
        event = JSON.parse(data);
      } catch {
        return;
      }
      if (event.type === "welcome") {
        clearTimeout(timer);
        user.value = event.user;
        status.value = "connected";
      } else if (event.type === "users") users.value = event.users;
      else if (
        event.type === "chat" ||
        event.type === "notice" ||
        event.type === "private" ||
        event.type === "file"
      ) {
        messages.value.push(event);
        if (messages.value.length > 500) messages.value.shift();
        if (event.recipient && event.sender.id !== user.value?.id) {
          const peer = peerOf(event);
          if (peer && peer.id !== activePeer.value?.id)
            unread.value = {
              ...unread.value,
              [peer.id]: (unread.value[peer.id] || 0) + 1,
            };
        }
      } else if (event.type === "error") {
        error.value = event.message;
        if (!user.value) {
          clearTimeout(timer);
          current.close();
          status.value = "disconnected";
          if (String(event.message).includes("登录")) clearSession();
        }
      }
    };
    current.onerror = () => {
      if (socket === current)
        error.value = "无法连接服务器，请检查服务是否启动";
    };
    current.onclose = () => {
      if (socket !== current) return;
      stopTransfers();
      clearTimeout(timer);
      status.value = "disconnected";
      users.value = [];
    };
  }
  async function readJson(response) {
    try {
      return await response.json();
    } catch {
      return {};
    }
  }
  function detail(body, fallback) {
    return typeof body.detail === "string" ? body.detail : fallback;
  }
  async function login(username, password) {
    error.value = "";
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const body = await readJson(response);
    if (!response.ok) {
      error.value = detail(body, "登录失败，请稍后重试");
      return false;
    }
    saveToken(body.token);
    account.value = body.user;
    connect();
    return true;
  }
  async function register(username, password) {
    error.value = "";
    const response = await fetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const body = await readJson(response);
    if (!response.ok) {
      error.value = detail(body, "注册失败，请稍后重试");
      return false;
    }
    return login(username, password);
  }
  async function logout() {
    const current = token;
    disconnect();
    clearSession();
    status.value = "idle";
    if (current) {
      try {
        await fetch("/api/auth/logout", {
          method: "POST",
          headers: { Authorization: `Bearer ${current}` },
        });
      } catch {
        /* 本地已退出，服务端会话由过期时间清理 */
      }
    }
  }
  async function restore() {
    const saved = localStorage.getItem(TOKEN_KEY);
    if (!saved) return;
    token = saved;
    try {
      const response = await fetch("/api/auth/me", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error("expired");
      const body = await response.json();
      account.value = body.user;
      connect();
    } catch {
      saveToken(null);
      account.value = null;
    }
  }
  function openPrivate(person) {
    if (!user.value || !person || person.id === user.value.id) return;
    activePeer.value = { id: person.id, nickname: person.nickname };
    unread.value = { ...unread.value, [person.id]: 0 };
  }
  function closePrivate() {
    activePeer.value = null;
  }
  function writable() {
    if (status.value !== "connected" || socket?.readyState !== WebSocket.OPEN) {
      error.value = "连接已断开，请重新进入";
      return false;
    }
    return true;
  }
  function validate(text) {
    error.value = "";
    const body = text.trim();
    if ([...body].length < 1 || [...body].length > 2000) {
      error.value = "消息须为1～2000个字符";
      return null;
    }
    return body;
  }
  function send(text) {
    const body = validate(text);
    if (body === null) return false;
    if (!writable()) return false;
    socket.send(JSON.stringify({ type: "chat", text: body }));
    return true;
  }
  function sendPrivate(text) {
    if (!activePeer.value) {
      error.value = "请先选择私聊对象";
      return false;
    }
    const body = validate(text);
    if (body === null) return false;
    if (!writable()) return false;
    socket.send(
      JSON.stringify({
        type: "private",
        to: activePeer.value.id,
        text: body,
      }),
    );
    return true;
  }
  async function sendFile(file) {
    error.value = "";
    if (!file || !writable()) return;
    if (pendingUpload) {
      error.value = "请等待当前上传完成";
      return;
    }
    if (file.size > MAX_FILE_SIZE) {
      error.value = "文件超过20 MiB限制";
      return;
    }
    if (!token) {
      error.value = "登录凭证缺失，请重新登录";
      return;
    }
    const target = activePeer.value ? { ...activePeer.value } : null;
    if (target && !users.value.some((person) => person.id === target.id)) {
      error.value = "对方不在线，无法发送文件";
      return;
    }
    const state = {
      name: file.name,
      target: target?.nickname || "公共频道",
      progress: 0,
    };
    upload.value = state;
    const transfer = uploadFile(file, token, target, (progress) => {
      if (pendingUpload === transfer) upload.value = { ...state, progress };
    });
    pendingUpload = transfer;
    try {
      await transfer.done;
    } catch (cause) {
      if (pendingUpload === transfer) error.value = cause.message;
    } finally {
      if (pendingUpload === transfer) {
        pendingUpload = null;
        upload.value = null;
      }
    }
  }
  function cancelFileUpload() {
    cancelUpload();
    error.value = "上传已取消；若文件卡片已出现，表示服务器已完成发送";
  }
  async function saveFile(file) {
    error.value = "";
    if (!writable() || !token || downloading.value[file.id]) return;
    const currentToken = token;
    const controller = new AbortController();
    downloads.add(controller);
    downloading.value = { ...downloading.value, [file.id]: true };
    try {
      await downloadFile(file, currentToken, controller.signal);
    } catch (cause) {
      if (!controller.signal.aborted && token === currentToken)
        error.value = cause.message;
    } finally {
      downloads.delete(controller);
      if (token === currentToken) {
        const remaining = { ...downloading.value };
        delete remaining[file.id];
        downloading.value = remaining;
      }
    }
  }
  onBeforeUnmount(disconnect);
  return {
    status,
    account,
    user,
    users,
    messages,
    error,
    activePeer,
    unread,
    upload,
    downloading,
    login,
    register,
    logout,
    restore,
    connect,
    join: connect,
    disconnect,
    send,
    sendPrivate,
    openPrivate,
    closePrivate,
    sendFile,
    cancelFileUpload,
    saveFile,
  };
}
