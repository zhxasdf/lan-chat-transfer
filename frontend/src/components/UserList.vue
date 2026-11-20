<script setup>
defineProps({ users: Array, selfId: String });
</script>
<template>
  <div class="user-panel">
    <h3>
      在线成员 <span>{{ users.length }}</span>
    </h3>
    <ul class="user-list">
      <li v-for="person in users" :key="person.id">
        <span class="avatar">{{ [...person.nickname][0] }}</span
        ><span class="member-name">{{ person.nickname }}</span
        ><small v-if="person.id === selfId">你</small><i class="online-dot"></i>
      </li>
    </ul>
  </div>
</template>
