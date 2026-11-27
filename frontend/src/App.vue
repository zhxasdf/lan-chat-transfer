<script setup>
import { ref, computed } from "vue";
import { useChat } from "./useChat";
import MessageList from "./components/MessageList.vue";
import UserList from "./components/UserList.vue";
import { formatSize } from "./fileTransfer";

const {
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
  loadNewer,
  info,
  upload,
  downloading,
  deleting,
  removeFile,
  historyLoading,
  hasMoreHistory,
  loadMoreHistory,
  login,
  register,
  logout,
  restore,
  connect,
  send,
  sendPrivate,
  openPrivate,
  closePrivate,
  sendFile,
  cancelFileUpload,
  saveFile,
} = useChat();
const username = ref(""),
  password = ref(""),
  authMode = ref("login"),
  busy = ref(false),
  draft = ref(""),
  showUsers = ref(false);
const fileInput = ref(null);
const lookupName = ref("");
const searching = ref(false);
async function startPrivate() {
  if (searching.value || !lookupName.value.trim()) return;
  searching.value = true;
  try {
    if (await lookupUser(lookupName.value)) {
      lookupName.value = "";
      showUsers.value = false;
    }
  } finally {
    searching.value = false;
  }
}
const labels = {
  idle: "未连接",
  connecting: "连接中",
  connected: "已连接",
  disconnected: "连接已断开",
};
restore();
function toggleAuth() {
  authMode.value = authMode.value === "login" ? "register" : "login";
  error.value = "";
}
async function submitAuth() {
  if (busy.value) return;
  busy.value = true;
  try {
    if (authMode.value === "login") await login(username.value, password.value);
    else await register(username.value, password.value);
  } finally {
    busy.value = false;
  }
}
function selectFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (file) sendFile(file);
}
const visibleMessages = computed(() => {
  if (!user.value) return [];
  return messages.value
    .filter((message) => {
      if (!activePeer.value) return !message.recipient;
      if (!message.recipient) return false;
      const peer =
        message.sender.user_id === user.value.user_id
          ? message.recipient
          : message.sender;
      return peer.user_id === activePeer.value.userId;
    })
    .sort((a, b) => (a.seq || 0) - (b.seq || 0));
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
async function leave() {
  draft.value = "";
  await logout();
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
      <span class="version">局域网聊天室 · 04</span>
    </header>
    <section v-if="!account" class="entry">
      <div class="entry-copy">
        <span class="eyebrow">LOCAL NETWORK / REAL CONNECTION</span>
        <h1>同一个网络，<br />同一个聊天室。</h1>
        <p>注册一个账号，聊天记录会跟着你。<br />打开网页即可加入。</p>
        <div class="feature-tags">
          <span>账号登录</span><span>群聊与私聊</span><span>文件传输</span>
        </div>
      </div>
      <form class="join-card" @submit.prevent="submitAuth">
        <span class="eyebrow">{{
          authMode === "login" ? "登录账号" : "注册账号"
        }}</span>
        <h2>{{ authMode === "login" ? "欢迎回来" : "创建你的账号" }}</h2>
        <label for="username">用户名</label
        ><input
          id="username"
          v-model="username"
          placeholder="3～20 个字符"
          autocomplete="username"
          :disabled="busy"
          autofocus
        />
        <label for="password">密码</label
        ><input
          id="password"
          v-model="password"
          type="password"
          :placeholder="authMode === 'login' ? '输入密码' : '8～64 个字符'"
          :autocomplete="
            authMode === 'login' ? 'current-password' : 'new-password'
          "
          :disabled="busy"
        />
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <button class="primary" :disabled="busy">
          {{
            busy ? "请稍候…" : authMode === "login" ? "登录 →" : "注册并登录 →"
          }}
        </button>
        <button type="button" class="text-button auth-alt" @click="toggleAuth">
          {{ authMode === "login" ? "还没有账号？注册" : "已有账号？直接登录" }}
        </button>
        <p class="card-foot">账号只用于本课程演示，请勿使用真实密码</p>
      </form>
    </section>
    <section v-else-if="!user" class="entry">
      <div class="join-card connecting-card">
        <span class="eyebrow">CONNECTING</span>
        <h2>正在连接聊天室…</h2>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <button class="primary" @click="connect">重新连接</button>
        <button type="button" class="text-button auth-alt" @click="logout">
          退出登录
        </button>
      </div>
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
        <div class="user-panel inbox-panel">
          <form class="lookup-form" @submit.prevent="startPrivate">
            <label for="lookup-user">发起私聊</label>
            <input
              id="lookup-user"
              v-model="lookupName"
              placeholder="输入完整用户名"
              maxlength="20"
              :disabled="status !== 'connected' || searching"
            />
            <button
              class="text-button"
              :disabled="
                !lookupName.trim() || searching || status !== 'connected'
              "
            >
              {{ searching ? "查找中…" : "查找账号" }}
            </button>
          </form>
          <h3>最近私聊</h3>
          <p v-if="!conversations.length" class="empty-inbox">
            还没有私聊，可查找账号开始。
          </p>
          <ul class="recent-list">
            <li v-for="item in conversations" :key="item.peer.id">
              <button
                :class="{ active: activePeer?.id === item.peer.id }"
                :aria-label="`打开与 ${item.peer.nickname} 的会话`"
                @click="selectPerson(item.peer)"
              >
                <span class="recent-name"
                  >{{ item.peer.nickname }}
                  <small>{{
                    users.some((person) => person.id === item.peer.id)
                      ? "在线"
                      : "离线"
                  }}</small></span
                >
                <span v-if="item.unread" class="unread-badge">{{
                  item.unread > 99 ? "99+" : item.unread
                }}</span>
                <span class="recent-preview">{{ item.last_text }}</span>
              </button>
            </li>
          </ul>
        </div>
        <div class="sidebar-note">
          可给离线账号发送文字。<br />聊天记录和未读状态会保留。
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
          :self-user-id="user.user_id"
          :private-peer="activePeer"
          :downloading="downloading"
          :deleting="deleting"
          :connected="status === 'connected'"
          :history-loading="historyLoading"
          :has-more="hasMoreHistory"
          @download="saveFile"
          @delete="removeFile"
          @loadmore="loadMoreHistory"
        />
        <button
          v-if="hasNewer"
          class="load-newer"
          :disabled="historyLoading"
          @click="loadNewer"
        >
          {{ historyLoading ? "正在加载…" : "加载后续消息" }}
        </button>
        <div
          v-if="status === 'disconnected'"
          class="disconnect-banner"
          role="status"
        >
          连接已断开，重新进入后可继续聊天。<button @click="connect">
            重新进入
          </button>
        </div>
        <form class="composer" @submit.prevent="submit">
          <p v-if="error" class="error" role="alert">{{ error }}</p>
          <p v-if="info" class="send-info" role="status">{{ info }}</p>
          <div v-if="upload" class="upload-status" role="status">
            <span class="upload-caption"
              >{{ upload.name }} → {{ upload.target }}</span
            >
            <progress
              :value="upload.progress"
              max="100"
              aria-label="上传进度"
            ></progress>
            <span>{{
              upload.progress === 100 ? "处理中…" : `${upload.progress}%`
            }}</span>
            <button type="button" class="text-button" @click="cancelFileUpload">
              取消上传
            </button>
          </div>
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
            <div class="file-actions">
              <input
                ref="fileInput"
                type="file"
                class="sr-only"
                aria-label="选择文件"
                @change="selectFile"
              />
              <button
                type="button"
                class="file-select"
                :disabled="status !== 'connected' || !!upload"
                @click="fileInput.click()"
              >
                ＋ 发送文件
              </button>
              <span class="file-limit"
                >≤ {{ formatSize(20 * 1024 * 1024) }}</span
              >
            </div>
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
