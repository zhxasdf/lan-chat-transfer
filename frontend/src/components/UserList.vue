<script setup>
defineProps({ users: Array, selfId: String, activeId: String, unread: Object });
const emit = defineEmits(["select"]);
</script>
<template>
  <div class="user-panel">
    <h3>
      在线成员 <span>{{ users.length }}</span>
    </h3>
    <ul class="user-list">
      <li
        v-for="person in users"
        :key="person.id"
        :class="{ active: person.id === activeId }"
      >
        <button
          class="member-button"
          :disabled="person.id === selfId"
          :aria-label="
            person.id === selfId
              ? `${person.nickname}（你）`
              : `与 ${person.nickname} 私聊`
          "
          @click="emit('select', person)"
        >
          <span class="avatar">{{ [...person.nickname][0] }}</span
          ><span class="member-name">{{ person.nickname }}</span
          ><small v-if="person.id === selfId">你</small
          ><span v-if="unread[person.id]" class="unread-badge">{{
            unread[person.id] > 99 ? "99+" : unread[person.id]
          }}</span
          ><i class="online-dot"></i>
        </button>
      </li>
    </ul>
  </div>
</template>
