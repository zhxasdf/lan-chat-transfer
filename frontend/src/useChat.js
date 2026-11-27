import { ref, watch, nextTick, onBeforeUnmount } from "vue";
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
    conversations = ref([]),
    hasNewer = ref(false),
    info = ref(""),
    upload = ref(null),
    downloading = ref({}),
    deleting = ref({}),
    history = ref({}),
    historyLoading = ref(false),
    hasMoreHistory = ref(true);
  let socket = null;
  let timer = null;
  let token = null;
  let generation = 0;
  let restoring = 0;
  let pendingUpload = null;
  const downloads = new Set();
  const fileStates = new Map();
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
    deleting.value = {};
  }
  function clearState() {
    fileStates.clear();
    users.value = [];
    status.value = "idle";
    user.value = null;
    messages.value = [];
    activePeer.value = null;
    unread.value = {};
    conversations.value = [];
    info.value = "";
    hasNewer.value = false;
    history.value = {};
    historyLoading.value = false;
    hasMoreHistory.value = true;
  }
  function disconnect() {
    generation++;
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
  function historyKey() {
    return activePeer.value ? `private:${activePeer.value.userId}` : "public";
  }
  function historyState(key) {
    if (!history.value[key])
      history.value[key] = {
        cursor: null,
        hasMore: true,
        ready: false,
        loading: false,
        hasNewer: false,
        forward: null,
        forwardSeq: 0,
        ackSeq: 0,
        acknowledging: false,
      };
    return history.value[key];
  }
  function syncHistoryState() {
    hasMoreHistory.value = historyState(historyKey()).hasMore;
    hasNewer.value = historyState(historyKey()).hasNewer;
    historyLoading.value = historyState(historyKey()).loading;
  }
  async function fetchHistory(direction = "older") {
    if (!token || status.value !== "connected") return;
    const key = historyKey();
    const selected = activePeer.value ? { ...activePeer.value } : null;
    const requestGeneration = generation;
    const state = historyState(key);
    if (
      state.loading ||
      (state.ready && !(direction === "newer" ? state.hasNewer : state.hasMore))
    )
      return;
    const initial = !state.ready;
    state.loading = true;
    syncHistoryState();
    try {
      const params = new URLSearchParams({
        kind: selected ? "private" : "public",
        limit: "50",
      });
      if (selected) params.set("peer", selected.userId);
      if (selected && initial) params.set("unread", "true");
      else if (direction === "newer" && state.forward)
        params.set("after", state.forward);
      else if (state.cursor) params.set("before", state.cursor);
      const response = await fetch(`/api/history?${params}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error("聊天记录加载失败，请重试");
      const body = await response.json();
      if (generation !== requestGeneration) return;
      const incoming = (Array.isArray(body.messages) ? body.messages : []).map(
        (message) =>
          message.file && fileStates.has(message.file.id)
            ? { ...message, file: fileStates.get(message.file.id) }
            : message,
      );
      const seen = new Set(messages.value.map((message) => message.id));
      const fresh = incoming.filter((message) => !seen.has(message.id));
      if (fresh.length) messages.value = [...messages.value, ...fresh];
      if (initial || direction === "older") {
        if (incoming.length) state.cursor = incoming[0].id;
        state.hasMore = Boolean(body.has_more);
      }
      if (selected && (initial || direction === "newer")) {
        state.forward = body.next_after;
        state.forwardSeq = incoming.at(-1)?.seq || state.forwardSeq;
        state.hasNewer = Boolean(body.has_newer);
      }
      state.ready = true;
      syncHistoryState();
    } catch {
      if (generation === requestGeneration)
        error.value = "聊天记录加载失败，请重试";
    } finally {
      state.loading = false;
      if (generation === requestGeneration) {
        syncHistoryState();
        await nextTick();
        acknowledge();
      }
    }
  }
  async function acknowledge() {
    const selected = activePeer.value;
    if (
      !selected ||
      document.visibilityState !== "visible" ||
      status.value !== "connected"
    )
      return;
    const state = historyState(historyKey());
    if (!state.ready || state.loading || state.acknowledging) return;
    const candidates = messages.value.filter(
      (event) =>
        event.recipient &&
        peerOf(event)?.id === selected.id &&
        (!state.hasNewer || event.seq <= state.forwardSeq),
    );
    const last = candidates.reduce(
      (a, b) => (!a || b.seq > a.seq ? b : a),
      null,
    );
    if (!last || last.seq <= state.ackSeq) return;
    const version = generation;
    state.acknowledging = true;
    try {
      const response = await fetch(
        `/api/conversations/${encodeURIComponent(selected.id)}/read`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ through: last.id }),
        },
      );
      if (response.ok && version === generation) state.ackSeq = last.seq;
    } catch {
      /* A later visit or visibility change retries the acknowledgement. */
    } finally {
      state.acknowledging = false;
      if (version === generation && state.ackSeq === last.seq)
        nextTick(acknowledge);
    }
  }
  watch(
    () => [messages.value.length, activePeer.value?.id],
    async () => {
      await nextTick();
      acknowledge();
    },
    { flush: "post" },
  );
  async function lookupUser(username) {
    const version = generation;
    error.value = "";
    try {
      const response = await fetch(
        `/api/users/lookup?username=${encodeURIComponent(username.trim())}`,
        {
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      const body = await readJson(response);
      if (version !== generation) return false;
      if (!response.ok) {
        error.value = detail(body, "查找失败");
        return false;
      }
      openPrivate(body.user);
      return true;
    } catch {
      if (version === generation) error.value = "查找失败，请重试";
      return false;
    }
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
        history.value = {};
        syncHistoryState();
        fetchHistory();
        current.send(JSON.stringify({ type: "subscribe_inbox" }));
      } else if (event.type === "users") users.value = event.users;
      else if (event.type === "conversations") {
        conversations.value = event.items;
        unread.value = Object.fromEntries(
          event.items.map((item) => [item.peer.id, item.unread]),
        );
      } else if (event.type === "file_state") {
        fileStates.set(event.file.id, event.file);
        messages.value = messages.value.map((message) =>
          message.file?.id === event.file.id
            ? { ...message, file: event.file }
            : message,
        );
      } else if (
        event.type === "chat" ||
        event.type === "notice" ||
        event.type === "private" ||
        event.type === "file"
      ) {
        const duplicate =
          event.id && messages.value.some((message) => message.id === event.id);
        if (!duplicate) {
          if (event.file && fileStates.has(event.file.id))
            event.file = fileStates.get(event.file.id);
          messages.value.push(event);
        }
        if (
          (event.type === "private" || event.type === "file") &&
          event.sender.id === user.value?.id
        )
          info.value = event.queued ? "对方离线，消息已保存" : "";
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
    current.onclose = ({ code }) => {
      if (socket !== current) return;
      if (code === 4001) {
        disconnect();
        clearSession();
        return;
      }
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
    const attempt = ++restoring;
    error.value = "";
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const body = await readJson(response);
    if (attempt !== restoring) return false;
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
    restoring++;
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
    const attempt = ++restoring;
    const saved = localStorage.getItem(TOKEN_KEY);
    disconnect();
    token = saved;
    account.value = null;
    if (!saved) return;
    try {
      const response = await fetch("/api/auth/me", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error("expired");
      const body = await response.json();
      if (attempt !== restoring || localStorage.getItem(TOKEN_KEY) !== saved)
        return;
      account.value = body.user;
      connect();
    } catch {
      if (attempt !== restoring || localStorage.getItem(TOKEN_KEY) !== saved)
        return;
      saveToken(null);
      account.value = null;
    }
  }
  function openPrivate(person) {
    if (!user.value || !person || person.id === user.value.id) return;
    activePeer.value = {
      id: person.id,
      userId: person.user_id,
      nickname: person.nickname,
    };
    info.value = "";
    syncHistoryState();
    if (!historyState(historyKey()).ready) fetchHistory();
    else nextTick(acknowledge);
  }
  function closePrivate() {
    activePeer.value = null;
    syncHistoryState();
    if (!historyState(historyKey()).ready) fetchHistory();
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
    info.value = "";
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
  async function removeFile(file) {
    if (!writable() || !token || deleting.value[file.id]) return;
    if (
      !window.confirm(
        `删除“${file.name}”？文件内容将被移除，聊天卡片仍会保留。`,
      )
    )
      return;
    const version = generation;
    deleting.value = { ...deleting.value, [file.id]: true };
    error.value = "";
    try {
      const response = await fetch(
        `/api/files/${encodeURIComponent(file.id)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        const body = await readJson(response);
        throw new Error(detail(body, "删除失败，请重试"));
      }
      if (version === generation)
        messages.value = messages.value.map((message) =>
          message.file?.id === file.id
            ? { ...message, file: { ...message.file, status: "deleted" } }
            : message,
        );
    } catch (cause) {
      if (version === generation) error.value = cause.message;
    } finally {
      if (version === generation) {
        const remaining = { ...deleting.value };
        delete remaining[file.id];
        deleting.value = remaining;
      }
    }
  }
  function syncSession(event) {
    if (event.storageArea !== localStorage) return;
    if (event.key !== TOKEN_KEY && event.key !== null) return;
    if (localStorage.getItem(TOKEN_KEY) !== token) restore();
  }
  window.addEventListener("storage", syncSession);
  document.addEventListener("visibilitychange", acknowledge);
  onBeforeUnmount(() => {
    restoring++;
    window.removeEventListener("storage", syncSession);
    document.removeEventListener("visibilitychange", acknowledge);
    disconnect();
  });
  return {
    status,
    account,
    user,
    users,
    messages,
    error,
    activePeer,
    unread,
    conversations,
    lookupUser,
    hasNewer,
    info,
    upload,
    downloading,
    deleting,
    removeFile,
    historyLoading,
    hasMoreHistory,
    fetchHistory,
    loadMoreHistory: () => fetchHistory("older"),
    loadNewer: () => fetchHistory("newer"),
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
