<template>
  <!--
    Brain tab content (#60). The Brain tab is a normal in-page panel — brain
    settings + a launch button — NOT an auto-jump to the full-page orb. Settings
    are a placeholder for now; the button opens the dedicated orb route.
  -->
  <div class="max-w-3xl">
    <div class="flex items-start gap-4">
      <div class="flex-shrink-0 w-14 h-14 rounded-full flex items-center justify-center bg-state-autonomous-50 dark:bg-state-autonomous-900/30 border border-state-autonomous-200 dark:border-state-autonomous-700 text-state-autonomous-500 dark:text-state-autonomous-400">
        <svg class="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" stroke-width="1.6" />
          <path stroke-width="1.4" stroke-linecap="round" d="M3 12h18" opacity="0.7" />
          <path stroke-width="1.4" stroke-linecap="round" d="M12 3c3.2 2.4 3.2 15.6 0 18M12 3c-3.2 2.4-3.2 15.6 0 18" opacity="0.7" />
        </svg>
      </div>
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2">
          <h3 class="text-lg font-semibold text-gray-900 dark:text-white">Brain Orb</h3>
          <span class="px-1.5 py-0.5 text-[10px] font-bold rounded bg-state-autonomous-100 dark:bg-state-autonomous-900/40 text-state-autonomous-700 dark:text-state-autonomous-400 leading-none">BETA</span>
        </div>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">
          The Self-Rendering Mind — a live 3D knowledge-graph view of {{ name }}'s memory,
          with a client-held voice tile to explore it by talking.
        </p>
      </div>
    </div>

    <!-- Settings (placeholder until brain settings exist) -->
    <div class="mt-6 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 p-4">
      <div class="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">Settings</div>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        No brain settings yet — options will appear here as they're added.
      </p>
    </div>

    <!-- Launch -->
    <div class="mt-6">
      <button
        @click="openBrain"
        :disabled="!running"
        class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
        :class="running
          ? 'bg-state-autonomous-500 hover:bg-state-autonomous-600 text-white shadow-sm'
          : 'bg-gray-200 dark:bg-gray-700 text-gray-400 dark:text-gray-500 cursor-not-allowed'"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" stroke-width="1.6" />
          <path stroke-width="1.4" stroke-linecap="round" d="M3 12h18" opacity="0.8" />
          <path stroke-width="1.4" stroke-linecap="round" d="M12 3c3.2 2.4 3.2 15.6 0 18M12 3c-3.2 2.4-3.2 15.6 0 18" opacity="0.8" />
        </svg>
        Open Brain Orb
      </button>
      <p v-if="!running" class="mt-2 text-xs text-gray-400 dark:text-gray-500">
        Start the agent to open its Brain Orb.
      </p>
    </div>
  </div>
</template>

<script setup>
import { useRouter } from 'vue-router'

const props = defineProps({
  name: { type: String, required: true },
  running: { type: Boolean, default: false },
})

const router = useRouter()
function openBrain() {
  router.push({ name: 'AgentBrainOrb', params: { name: props.name } })
}
</script>
