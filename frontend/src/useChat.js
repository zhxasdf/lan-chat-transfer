import { ref, onBeforeUnmount } from "vue";

export function useChat() {
  const status = ref("idle"),
    user = ref(null),
    users = ref([]),
    messages = ref([]),
    error = ref("");
  let socket = null;
  let timer = null;
  function disconnect() {
    clearTimeout(timer);
    const previous = socket;
    socket = null;
    previous?.close();
    users.value = [];
    status.value = "idle";
    user.value = null;
    messages.value = [];
    error.value = "";
  }
  function join(nickname) {
    disconnect();
    const name = nickname.trim();
    if ([...name].length < 1 || [...name].length > 20) {
      error.value = "昵称须为1～20个字符";
      return;
    }
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
        current.send(JSON.stringify({ type: "join", nickname: name }));
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
      else if (event.type === "chat" || event.type === "notice") {
        messages.value.push(event);
        if (messages.value.length > 500) messages.value.shift();
      } else if (event.type === "error") {
        error.value = event.message;
        if (!user.value) {
          clearTimeout(timer);
          current.close();
          status.value = "disconnected";
        }
      }
    };
    current.onerror = () => {
      if (socket === current)
        error.value = "无法连接服务器，请检查服务是否启动";
    };
    current.onclose = () => {
      if (socket !== current) return;
      clearTimeout(timer);
      status.value = "disconnected";
      users.value = [];
    };
  }
  function send(text) {
    error.value = "";
    const body = text.trim();
    if ([...body].length < 1 || [...body].length > 2000) {
      error.value = "消息须为1～2000个字符";
      return false;
    }
    if (status.value !== "connected" || socket?.readyState !== WebSocket.OPEN) {
      error.value = "连接已断开，请重新进入";
      return false;
    }
    socket.send(JSON.stringify({ type: "chat", text: body }));
    return true;
  }
  onBeforeUnmount(disconnect);
  return { status, user, users, messages, error, join, disconnect, send };
}
