<script setup>
import { ref } from "vue";
import { useChat } from "./useChat";
import MessageList from "./components/MessageList.vue";
import UserList from "./components/UserList.vue";

const { status, user, users, messages, error, join, disconnect, send } =
  useChat();
const nickname = ref(""),
  draft = ref(""),
  showUsers = ref(false);
const labels = {
  idle: "未连接",
  connecting: "连接中",
  connected: "已连接",
  disconnected: "连接已断开",
};
function submit() {
  if (send(draft.value)) draft.value = "";
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
      <span class="version">局域网聊天室 · 01</span>
    </header>
    <section v-if="!user" class="entry">
      <div class="entry-copy">
        <span class="eyebrow">LOCAL NETWORK / REAL CONNECTION</span>
        <h1>同一个网络，<br />同一个聊天室。</h1>
        <p>选一个昵称，和身边的人聊起来。<br />无需注册，打开网页即可加入。</p>
        <div class="feature-tags">
          <span>实时群聊</span><span>在线成员</span><span>多端访问</span>
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
        ><UserList :users="users" :self-id="user.id" />
        <div class="sidebar-note">
          消息仅在当前会话展示。<br />请友善交流，尊重彼此。
        </div>
      </aside>
      <div class="conversation">
        <div class="conversation-header">
          <div>
            <span class="eyebrow">PUBLIC CHANNEL</span>
            <h2>一起聊聊</h2>
          </div>
          <div class="session">
            <span class="connection" :class="status"
              ><i></i>{{ labels[status] }}</span
            ><span class="self-name">{{ user.nickname }}</span
            ><button class="text-button" @click="leave">退出</button>
          </div>
        </div>
        <MessageList :messages="messages" :self-id="user.id" />
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
            placeholder="说点什么，让交流开始…"
            :disabled="status !== 'connected'"
            @keydown="keydown"
          ></textarea>
          <div class="composer-bottom">
            <span>Enter 发送 · Shift + Enter 换行</span
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
