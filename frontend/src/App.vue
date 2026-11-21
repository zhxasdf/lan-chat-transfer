<script setup>
import { ref, computed } from "vue";
import { useChat } from "./useChat";
import MessageList from "./components/MessageList.vue";
import UserList from "./components/UserList.vue";

const {
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
} = useChat();
const nickname = ref(""),
  draft = ref(""),
  showUsers = ref(false);
const labels = {
  idle: "未连接",
  connecting: "连接中",
  connected: "已连接",
  disconnected: "连接已断开",
};
const visibleMessages = computed(() => {
  if (!user.value) return [];
  return messages.value.filter((message) => {
    if (!activePeer.value) return message.type !== "private";
    if (message.type !== "private") return false;
    const peer =
      message.sender.id === user.value.id
        ? message.recipient
        : message.sender;
    return peer.id === activePeer.value.id;
  });
});
const peerOnline = computed(
  () =>
    !activePeer.value ||
    users.value.some((person) => person.id === activePeer.value.id),
);
function submit() {
  const accepted = activePeer.value
    ? sendPrivate(draft.value)
    : send(draft.value);
  if (accepted) draft.value = "";
}
function keydown(event) {
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    submit();
  }
}
function selectPerson(person) {
  if (!person || person.id === user.value.id) return;
  openPrivate(person);
  showUsers.value = false;
}
function leave() {
  disconnect();
  draft.value = "";
}
</script>

<template>
  <main class="shell">
    <header class="brand">
      <span class="brand-icon">L</span>
      <div>
        <strong>LAN Chat</strong
        ><span class="brand-caption">让沟通发生在身边</span>
      </div>
      <span class="version">局域网聊天室 · 02</span>
    </header>
    <section v-if="!user" class="entry">
      <div class="entry-copy">
        <span class="eyebrow">LOCAL NETWORK / REAL CONNECTION</span>
        <h1>同一个网络，<br />同一个聊天室。</h1>
        <p>选一个昵称，和身边的人聊起来。<br />无需注册，打开网页即可加入。</p>
        <div class="feature-tags">
          <span>实时群聊</span><span>一对一私聊</span><span>在线成员</span>
        </div>
      </div>
      <form class="join-card" @submit.prevent="join(nickname)">
        <span class="eyebrow">加入公共频道</span>
        <h2>你好，怎么称呼？</h2>
        <label for="nickname">你的昵称</label
        ><input
          id="nickname"
          v-model="nickname"
          placeholder="输入 1～20 个字符"
          autocomplete="off"
          :disabled="status === 'connecting'"
          autofocus
        />
        <p class="hint">昵称仅用于本次聊天，请勿填写敏感信息。</p>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <button class="primary" :disabled="status === 'connecting'">
          {{ status === "connecting" ? "正在连接…" : "进入聊天室 →" }}
        </button>
        <p class="card-foot">同一局域网内，分享地址即可邀请朋友</p>
      </form>
    </section>
    <section v-else class="workspace">
      <aside class="sidebar" :class="{ expanded: showUsers }">
        <div class="room-title">
          <span class="room-mark">#</span>
          <div>
            <h2>公共聊天室</h2>
            <p>每一次连接，都从这里开始</p>
          </div>
        </div>
        <button class="members-toggle" @click="showUsers = !showUsers">
          在线成员 · {{ users.length }}
          {{ showUsers ? "收起" : "展开" }}</button
        ><UserList
          :users="users"
          :self-id="user.id"
          :active-id="activePeer ? activePeer.id : null"
          :unread="unread"
          @select="selectPerson"
        />
        <div class="sidebar-note">
          点击在线成员可发起私聊。<br />消息仅在当前会话展示。
        </div>
      </aside>
      <div class="conversation">
        <div class="conversation-header">
          <div class="conversation-title">
            <button
              v-if="activePeer"
              class="text-button back-button"
              @click="closePrivate"
            >
              ← 公共频道
            </button>
            <span class="eyebrow">{{
              activePeer ? "PRIVATE CHAT" : "PUBLIC CHANNEL"
            }}</span>
            <h2>
              {{ activePeer ? `与 ${activePeer.nickname} 私聊` : "一起聊聊" }}
              <span v-if="activePeer && !peerOnline" class="offline-hint"
                >对方已离线</span
              >
            </h2>
          </div>
          <div class="session">
            <span class="connection" :class="status"
              ><i></i>{{ labels[status] }}</span
            ><span class="self-name">{{ user.nickname }}</span
            ><button class="text-button" @click="leave">退出</button>
          </div>
        </div>
        <MessageList
          :messages="visibleMessages"
          :self-id="user.id"
          :private-peer="activePeer"
        />
        <div
          v-if="status === 'disconnected'"
          class="disconnect-banner"
          role="status"
        >
          连接已断开，重新进入后可继续聊天。<button @click="join(nickname)">
            重新进入
          </button>
        </div>
        <form class="composer" @submit.prevent="submit">
          <p v-if="error" class="error" role="alert">{{ error }}</p>
          <label class="sr-only" for="message">消息内容</label
          ><textarea
            id="message"
            v-model="draft"
            :placeholder="
              activePeer
                ? `私聊 ${activePeer.nickname}，只有对方能看到…`
                : '说点什么，让交流开始…'
            "
            :disabled="status !== 'connected'"
            @keydown="keydown"
          ></textarea>
          <div class="composer-bottom">
            <span>{{
              activePeer
                ? "Enter 发送 · 仅对方可见"
                : "Enter 发送 · Shift + Enter 换行"
            }}</span
            ><button
              class="primary"
              :disabled="status !== 'connected' || !draft.trim()"
            >
              发送消息 ↗
            </button>
          </div>
        </form>
      </div>
    </section>
    <footer class="page-footer">
      LAN CHAT TRANSFER <span>简单连接，自在交流。</span>
    </footer>
  </main>
</template>
