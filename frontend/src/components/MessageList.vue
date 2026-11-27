<script setup>
import { ref, watch, nextTick } from "vue";
import { formatSize } from "../fileTransfer";
const props = defineProps({
  messages: Array,
  selfId: String,
  selfUserId: String,
  privatePeer: Object,
  downloading: Object,
  connected: Boolean,
  historyLoading: Boolean,
  hasMore: Boolean,
});
const emit = defineEmits(["download", "loadmore"]);
const list = ref(null);
function time(value) {
  return new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}
function onScroll() {
  const element = list.value;
  if (!element || props.historyLoading || !props.hasMore) return;
  if (element.scrollTop < 40) emit("loadmore");
}
watch(
  () =>
    props.messages.length ? props.messages[props.messages.length - 1] : null,
  async () => {
    const element = list.value;
    const nearBottom =
      !element ||
      element.scrollHeight - element.scrollTop - element.clientHeight < 140;
    await nextTick();
    if (nearBottom && list.value)
      list.value.scrollTop = list.value.scrollHeight;
  },
);
watch(
  () => props.messages[0]?.id,
  async (next, previous) => {
    const element = list.value;
    if (!element || !previous || next === previous) return;
    const before = element.scrollHeight;
    await nextTick();
    if (list.value) list.value.scrollTop += list.value.scrollHeight - before;
  },
);
</script>
<template>
  <div
    ref="list"
    class="messages"
    role="log"
    aria-label="聊天消息"
    aria-live="polite"
    @scroll="onScroll"
  >
    <div class="channel-intro">
      <span>{{ privatePeer ? "@" : "#" }}</span>
      <h3>
        {{
          privatePeer
            ? `与 ${privatePeer.nickname} 的私聊`
            : "欢迎来到公共聊天室"
        }}
      </h3>
      <p>
        {{
          privatePeer
            ? "只有你和对方能看到这里的消息。"
            : "这是你们本次交流的开始。"
        }}
      </p>
    </div>
    <p v-if="historyLoading" class="history-status" role="status">
      正在加载更早的消息…
    </p>
    <template
      v-for="(message, index) in messages"
      :key="message.id || `notice-${index}`"
      ><p v-if="message.type === 'notice'" class="notice">
        {{ message.text }} · {{ time(message.time) }}
      </p>
      <article
        v-else
        class="message"
        :class="{ mine: message.sender.user_id === selfUserId }"
      >
        <span class="avatar">{{ [...message.sender.nickname][0] }}</span>
        <div class="message-content">
          <div class="message-meta">
            <strong>{{ message.sender.nickname }}</strong
            ><time>{{ time(message.time) }}</time>
          </div>
          <div
            v-if="message.type === 'file'"
            class="file-card"
            :data-file-id="message.file.id"
          >
            <span class="file-icon" aria-hidden="true">▤</span>
            <div class="file-details">
              <strong class="file-name">{{ message.file.name }}</strong
              ><span>{{ formatSize(message.file.size) }} · 临时文件</span>
            </div>
            <button
              type="button"
              class="file-download"
              :aria-label="`下载 ${message.file.name}`"
              :disabled="!connected || downloading?.[message.file.id]"
              @click="$emit('download', message.file)"
            >
              {{ downloading?.[message.file.id] ? "下载中…" : "下载" }}
            </button>
          </div>
          <div v-else class="bubble">{{ message.text }}</div>
        </div>
      </article></template
    >
  </div>
</template>
