<script setup>
import { ref, watch, nextTick } from "vue";
const props = defineProps({ messages: Array, selfId: String, privatePeer: Object });
const list = ref(null);
function time(value) {
  return new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
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
</script>
<template>
  <div
    ref="list"
    class="messages"
    role="log"
    aria-label="聊天消息"
    aria-live="polite"
  >
    <div class="channel-intro">
      <span>{{ privatePeer ? "@" : "#" }}</span>
      <h3>
        {{
          privatePeer ? `与 ${privatePeer.nickname} 的私聊` : "欢迎来到公共聊天室"
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
    <template
      v-for="(message, index) in messages"
      :key="message.id || `notice-${index}`"
      ><p v-if="message.type === 'notice'" class="notice">
        {{ message.text }} · {{ time(message.time) }}
      </p>
      <article
        v-else
        class="message"
        :class="{ mine: message.sender.id === selfId }"
      >
        <span class="avatar">{{ [...message.sender.nickname][0] }}</span>
        <div class="message-content">
          <div class="message-meta">
            <strong>{{ message.sender.nickname }}</strong
            ><time>{{ time(message.time) }}</time>
          </div>
          <div class="bubble">{{ message.text }}</div>
        </div>
      </article></template
    >
  </div>
</template>
