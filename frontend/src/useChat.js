import { ref, onBeforeUnmount } from "vue";

export function useChat() {
  const status = ref("idle"),
    user = ref(null),
    users = ref([]),
    messages = ref([]),
    error = ref(""),
    activePeer = ref(null),
    unread = ref({});
  let socket = null;
  let timer = null;
  function clearState() {
    users.value = [];
    status.value = "idle";
    user.value = null;
    messages.value = [];
    activePeer.value = null;
    unread.value = {};
  }
  function disconnect() {
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
      else if (
        event.type === "chat" ||
        event.type === "notice" ||
        event.type === "private"
      ) {
        messages.value.push(event);
        if (messages.value.length > 500) messages.value.shift();
        if (event.type === "private") {
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
  onBeforeUnmount(disconnect);
  return {
    status,
    user,
    users,
    messages,
    error,
    activePeer,
    unread,
    join,
    disconnect,
    send,
    sendPrivate,
    openPrivate,
    closePrivate,
  };
}
